"""
writer.py — PostgreSQL writer for Sentinel conclusions.

Writes ForecastResult and RightSizingResult objects to the recommendations
schema and emits a corresponding audit event for each row written.

Design:
- Each write is a single transaction per result type per cycle.
- Exhaustion estimates are derived from forecast results in the same call.
- The retention cleanup function is called at the start of each write cycle.
- Audit records are always written even if the recommendations write fails
  (separate transaction).
"""

import json
import logging
from datetime import date, datetime, timezone

import psycopg2.extras

import config as cfg_module
import db
from analysis.cluster_health import ClusterHealthResult
from analysis.forecaster     import ForecastResult
from analysis.resize_advisor import ResizeAdvice
from analysis.rightsizing    import RightSizingResult

log = logging.getLogger(__name__)


def _dt(value) -> str | None:
    """Serialise date/datetime to ISO string for JSON payloads."""
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _uuid_or_none(value) -> str | None:
    """Return None for empty/missing UUID strings; PostgreSQL uuid type rejects ''."""
    if not value:
        return None
    return value


def _sanitize_json(obj):
    """
    Recursively replace float NaN/Inf with None so json.dumps produces valid JSON.
    PostgreSQL's json/jsonb types reject the bare NaN token that Python emits by default.
    """
    import math
    if isinstance(obj, dict):
        return {k: _sanitize_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_json(v) for v in obj]
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    return obj


class Writer:
    def __init__(self, cfg: cfg_module.Config) -> None:
        self._cfg = cfg

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def run_retention_cleanup(self) -> None:
        """Call the PostgreSQL cleanup function to purge expired rows."""
        try:
            with db.connect(self._cfg) as conn:
                with conn.cursor() as cur:
                    cur.execute('SELECT * FROM public.cleanup_expired_recommendations()')
                    rows = cur.fetchall()
            for table, count in rows:
                if count > 0:
                    log.info('Retention cleanup', extra={'table': table, 'deleted': count})
        except Exception:
            log.exception('Retention cleanup failed')

    # ------------------------------------------------------------------
    # Forecasts
    # ------------------------------------------------------------------

    def write_forecasts(self, forecasts: list[ForecastResult]) -> None:
        if not forecasts:
            return

        self.run_retention_cleanup()

        inserted_ids: list[tuple[int, ForecastResult]] = []

        try:
            with db.connect(self._cfg) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        'DELETE FROM recommendations.forecast_snapshots WHERE cluster_id = %s',
                        [forecasts[0].cluster_id],
                    )
                    for f in forecasts:
                        cur.execute(
                            '''
                            INSERT INTO recommendations.forecast_snapshots
                                (cluster_id, node_uuid, entity_label, resource_dimension,
                                 forecast_horizon_days, current_usage, current_capacity,
                                 forecast_value, confidence_score, driving_metric,
                                 exhaustion_date, severity, model_metadata)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            RETURNING id
                            ''',
                            (
                                f.cluster_id,
                                _uuid_or_none(f.node_uuid),
                                f.entity_label,
                                f.resource_dimension,
                                f.forecast_horizon_days,
                                f.current_usage,
                                f.current_capacity,
                                f.forecast_value,
                                f.confidence_score,
                                f.driving_metric,
                                f.exhaustion_date,
                                f.severity,
                                json.dumps(_sanitize_json(f.model_metadata)),
                            ),
                        )
                        row_id = cur.fetchone()[0]
                        inserted_ids.append((row_id, f))

            log.info('Forecast snapshots written', extra={'count': len(inserted_ids)})
        except Exception:
            log.exception('Failed to write forecast snapshots')
            return

        # Write audit events (separate transaction — audit must not be lost
        # if the recommendations write somehow partially failed)
        self._emit_audit_events(inserted_ids, 'forecast_snapshot', 'recommendations.forecast_snapshots')

        # Derive and write resource exhaustion records from these forecasts
        self._write_exhaustion(forecasts)

    # ------------------------------------------------------------------
    # Right-sizing
    # ------------------------------------------------------------------

    def write_rightsizing(self, results: list[RightSizingResult]) -> None:
        if not results:
            return

        inserted_ids: list[tuple[int, RightSizingResult]] = []

        try:
            with db.connect(self._cfg) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        'DELETE FROM recommendations.right_sizing WHERE cluster_id = %s',
                        [results[0].cluster_id],
                    )
                    for r in results:
                        cur.execute(
                            '''
                            INSERT INTO recommendations.right_sizing
                                (cluster_id, vm_uuid, vm_name, node_uuid,
                                 sizing_direction, severity, analysis_window_days,
                                 cpu_signal_score, cpu_avg_usage_pct, cpu_p95_usage_pct,
                                 latency_signal_score, spike_signal_score,
                                 composite_score, signal_detail, current_vcpu)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            RETURNING id
                            ''',
                            (
                                r.cluster_id,
                                _uuid_or_none(r.vm_uuid),
                                r.vm_name,
                                _uuid_or_none(r.node_uuid),
                                r.sizing_direction,
                                r.severity,
                                r.analysis_window_days,
                                r.cpu_signal_score,
                                r.cpu_avg_usage_pct,
                                r.cpu_p95_usage_pct,
                                r.latency_signal_score,
                                r.spike_signal_score,
                                r.composite_score,
                                json.dumps(r.signal_detail),
                                r.current_vcpu,
                            ),
                        )
                        row_id = cur.fetchone()[0]
                        inserted_ids.append((row_id, r))

            log.info('Right-sizing results written', extra={'count': len(inserted_ids)})
        except Exception:
            log.exception('Failed to write right-sizing results')
            return

        self._emit_audit_events(inserted_ids, 'right_sizing', 'recommendations.right_sizing')

    # ------------------------------------------------------------------
    # Resource exhaustion (derived from forecasts)
    # ------------------------------------------------------------------

    def _write_exhaustion(self, forecasts: list[ForecastResult]) -> None:
        """
        Extract exhaustion-relevant forecasts and write to resource_exhaustion table.
        Only forecasts with an exhaustion_date or CRITICAL/EMERGENCY severity are written.
        Deduplicated to the most severe forecast per (cluster, node, dimension).
        """
        # Pick one record per (cluster_id, node_uuid, dimension) — most severe
        best: dict[tuple, ForecastResult] = {}
        severity_order = {'EMERGENCY': 5, 'CRITICAL': 4, 'WARNING': 3, 'ATTENTION': 2, 'INFO': 1}

        for f in forecasts:
            key = (f.cluster_id, f.node_uuid, f.resource_dimension)
            existing = best.get(key)
            if existing is None or severity_order[f.severity] > severity_order[existing.severity]:
                best[key] = f

        rows = [f for f in best.values() if f.exhaustion_date is not None or f.severity in ('CRITICAL', 'EMERGENCY')]
        if not rows:
            return

        inserted_ids = []
        try:
            with db.connect(self._cfg) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        'DELETE FROM recommendations.resource_exhaustion WHERE cluster_id = %s',
                        [rows[0].cluster_id],
                    )
                    for f in rows:
                        days = (f.exhaustion_date - date.today()).days if f.exhaustion_date else None
                        growth = f.model_metadata.get('slope')
                        cur.execute(
                            '''
                            INSERT INTO recommendations.resource_exhaustion
                                (cluster_id, node_uuid, resource_dimension,
                                 days_until_exhaustion, exhaustion_date,
                                 current_usage_pct, growth_rate_per_day, severity)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                            RETURNING id
                            ''',
                            (
                                f.cluster_id,
                                f.node_uuid,
                                f.resource_dimension,
                                days,
                                f.exhaustion_date,
                                f.current_usage,
                                growth,
                                f.severity,
                            ),
                        )
                        row_id = cur.fetchone()[0]
                        inserted_ids.append((row_id, f))

            log.info('Exhaustion records written', extra={'count': len(inserted_ids)})
        except Exception:
            log.exception('Failed to write exhaustion records')
            return

        self._emit_audit_events(inserted_ids, 'resource_exhaustion', 'recommendations.resource_exhaustion')

    # ------------------------------------------------------------------
    # Resize recommendations (C3-C6)
    # ------------------------------------------------------------------

    def write_resize_advice(self, advice: list[ResizeAdvice]) -> None:
        if not advice:
            return

        inserted_ids: list[tuple[int, ResizeAdvice]] = []

        try:
            with db.connect(self._cfg) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        'DELETE FROM recommendations.resize_recommendations WHERE cluster_id = %s',
                        [advice[0].cluster_id],
                    )
                    for a in advice:
                        cur.execute(
                            '''
                            INSERT INTO recommendations.resize_recommendations
                                (cluster_id, vm_uuid, vm_name, node_uuid,
                                 sizing_direction, suggested_cpu_change_pct,
                                 target_cpu_avg_pct, confidence_score,
                                 capacity_reclaim_cpu_pct, risk_level, risk_detail,
                                 severity, current_vcpu, recommended_vcpu)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            RETURNING id
                            ''',
                            (
                                a.cluster_id,
                                _uuid_or_none(a.vm_uuid),
                                a.vm_name,
                                _uuid_or_none(a.node_uuid),
                                a.sizing_direction,
                                a.suggested_cpu_change_pct,
                                a.target_cpu_avg_pct,
                                a.confidence_score,
                                a.capacity_reclaim_cpu_pct,
                                a.risk_level,
                                json.dumps(a.risk_detail),
                                a.severity,
                                a.current_vcpu,
                                a.recommended_vcpu,
                            ),
                        )
                        row_id = cur.fetchone()[0]
                        inserted_ids.append((row_id, a))

            log.info('Resize advice written', extra={'count': len(inserted_ids)})
        except Exception:
            log.exception('Failed to write resize advice')
            return

        self._emit_audit_events(
            inserted_ids, 'resize_recommendation', 'recommendations.resize_recommendations'
        )

    # ------------------------------------------------------------------
    # Cluster health (D1-D4, D6)
    # ------------------------------------------------------------------

    def write_cluster_health(self, health: ClusterHealthResult) -> None:
        inserted_ids: list[tuple[int, ClusterHealthResult]] = []

        try:
            with db.connect(self._cfg) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        '''
                        INSERT INTO recommendations.cluster_health_scores
                            (cluster_id, health_score, bottleneck_resource,
                             growth_rates, efficiency_index, component_scores,
                             cluster_count, severity)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING id
                        ''',
                        (
                            health.cluster_id,
                            health.health_score,
                            health.bottleneck_resource,
                            json.dumps(health.growth_rates),
                            health.efficiency_index,
                            json.dumps(health.component_scores),
                            health.cluster_count,
                            health.severity,
                        ),
                    )
                    row_id = cur.fetchone()[0]
                    inserted_ids.append((row_id, health))

            log.info(
                'Cluster health written',
                extra={'health_score': health.health_score, 'severity': health.severity},
            )
        except Exception:
            log.exception('Failed to write cluster health')
            return

        self._emit_audit_events(
            inserted_ids, 'cluster_health', 'recommendations.cluster_health_scores'
        )

    # ------------------------------------------------------------------
    # Audit
    # ------------------------------------------------------------------

    def _emit_audit_events(
        self,
        inserted: list[tuple[int, object]],
        event_type: str,
        source_table: str,
    ) -> None:
        """Write one audit event per inserted recommendation row."""
        try:
            with db.connect(self._cfg) as conn:
                with conn.cursor() as cur:
                    for row_id, obj in inserted:
                        severity = getattr(obj, 'severity', 'INFO')
                        cluster_id = getattr(obj, 'cluster_id', self._cfg.cluster_id)

                        # Build a JSON snapshot of the object for the audit record
                        payload = _sanitize_json({
                            k: _dt(v) if isinstance(v, (date, datetime)) else v
                            for k, v in obj.__dict__.items()
                        })

                        cur.execute(
                            '''
                            INSERT INTO audit.recommendation_events
                                (cluster_id, event_type, source_id, source_table,
                                 severity, payload_snapshot)
                            VALUES (%s, %s, %s, %s, %s, %s)
                            ''',
                            (
                                cluster_id,
                                event_type,
                                row_id,
                                source_table,
                                severity,
                                json.dumps(payload, default=str),
                            ),
                        )
            log.debug('Audit events emitted', extra={'event_type': event_type, 'count': len(inserted)})
        except Exception:
            log.exception('Failed to write audit events (event_type=%s)', event_type)
