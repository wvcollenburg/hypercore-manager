"""
writer.py — PostgreSQL writer for Sentinel conclusions.

Writes ForecastResult and RightSizingResult objects to the recommendations
schema and emits a corresponding audit event for each row written.

Design:
- Each write is a single transaction per result type per cycle.
- Exhaustion estimates are derived from forecast results in the same call.
- The retention cleanup function is called at the start of each write cycle.
- Audit events are written in a separate transaction after the recommendations
  write, and only for what changed: a recommendation whose key (event type,
  entity, dimension, horizon) was not in the previous cycle's rows, or whose
  severity or direction differs from them. INFO forecast snapshots are not
  audited. Re-emitting every row every hour grew audit.* by millions of rows a
  year, made the outcome tracker check each VM 24 times a day and the webhook
  repeat an unchanged CRITICAL hourly.
- prune_audit() removes audit rows older than retention_months.
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

# event_type -> (columns that identify the recommendation, columns whose change is news); the
# names are the same on the result objects and in the recommendations.* tables
_AUDIT_IDENTITY = {
    'forecast_snapshot':     (('node_uuid', 'entity_label', 'resource_dimension', 'forecast_horizon_days'), ('severity',)),
    'right_sizing':          (('vm_uuid', 'sizing_direction'), ('severity',)),
    'resize_recommendation': (('vm_uuid', 'sizing_direction'), ('severity',)),
    'resource_exhaustion':   (('node_uuid', 'resource_dimension'), ('severity',)),
    'cluster_health':        ((), ('severity',)),
}
_AUDIT_TABLE = {
    'forecast_snapshot':     'recommendations.forecast_snapshots',
    'right_sizing':          'recommendations.right_sizing',
    'resize_recommendation': 'recommendations.resize_recommendations',
    'resource_exhaustion':   'recommendations.resource_exhaustion',
    'cluster_health':        'recommendations.cluster_health_scores',
}
_AUDIT_PRUNE_BATCH = 20000


def _norm(value):
    """A key/state value as text, so a UUID from the database equals the one on the result."""
    if value is None or value == '':
        return None
    return str(value)


def _identity(event_type: str, obj) -> tuple[tuple, tuple]:
    keys, state = _AUDIT_IDENTITY[event_type]
    get = (lambda f: obj.get(f)) if isinstance(obj, dict) else (lambda f: getattr(obj, f, None))
    return tuple(_norm(get(f)) for f in keys), tuple(_norm(get(f)) for f in state)


def _previous_state(cur, event_type: str, cluster_id: str) -> dict:
    """{key: state} of the rows the previous cycle left in the recommendations table. Read in
    the write's own transaction, before its DELETE (cluster health is append-only: its latest
    row)."""
    keys, state = _AUDIT_IDENTITY[event_type]
    cols = keys + state
    sql = f'SELECT {", ".join(cols)} FROM {_AUDIT_TABLE[event_type]} WHERE cluster_id = %s'
    if event_type == 'cluster_health':
        sql += ' ORDER BY id DESC LIMIT 1'
    cur.execute(sql, [cluster_id])
    prev = {}
    for row in cur.fetchall():
        key, st = _identity(event_type, dict(zip(cols, row)))
        prev[key] = st
    return prev


def _usage_pct(f) -> float | None:
    """A forecast's current usage as a percentage, for resource_exhaustion.current_usage_pct:
    CPU is a percentage already; RAM and other byte dimensions are taken against the forecast's
    capacity, or NULL when there is none. Storing the raw value put RAM bytes in a percent column
    (the report printed '320000000000.0%')."""
    if f.current_usage is None:
        return None
    if f.resource_dimension == 'cpu':
        return f.current_usage
    if f.current_capacity:
        return round(f.current_usage / f.current_capacity * 100.0, 2)
    return None


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
    # cleanup
    # ------------------------------------------------------------------

    def prune_stale_vms(self, active_vms: dict[str, str]) -> int:
        """
        Delete forecast and right-sizing rows for VMs no longer active in InfluxDB.

        active_vms: {vm_uuid: vm_name} — VMs confirmed active in the recent window.

        Skips entirely when active_vms is empty to guard against a false-positive
        purge caused by an InfluxDB outage.  Returns total deleted row count.

        For forecast_snapshots, only rows whose entity_label was previously a known
        VM name (i.e. present in right_sizing) are targeted, so node-level network
        forecasts sharing the same resource_dimension are left untouched.
        """
        if not active_vms:
            log.warning('prune_stale_vms: active VM set is empty — skipping to avoid false purge')
            return 0

        # vm_uuid columns are UUID; psycopg2 sends a Python list as text[], so cast explicitly
        active_uuids = list(active_vms.keys())
        cluster_id   = self._cfg.cluster_id
        total        = 0
        stale_names: list[str] = []

        try:
            with db.connect(self._cfg) as conn:
                with conn.cursor() as cur:
                    # collect stale VM names from right_sizing before deleting —
                    # used to safely target only VM-keyed rows in forecast_snapshots
                    cur.execute(
                        '''SELECT DISTINCT vm_name FROM recommendations.right_sizing
                           WHERE cluster_id = %s AND NOT (vm_uuid = ANY(%s::uuid[]))''',
                        (cluster_id, active_uuids),
                    )
                    stale_names = [row[0] for row in cur.fetchall()]

                    cur.execute(
                        '''DELETE FROM recommendations.right_sizing
                           WHERE cluster_id = %s AND NOT (vm_uuid = ANY(%s::uuid[]))''',
                        (cluster_id, active_uuids),
                    )
                    total += cur.rowcount

                    cur.execute(
                        '''DELETE FROM recommendations.resize_recommendations
                           WHERE cluster_id = %s AND NOT (vm_uuid = ANY(%s::uuid[]))''',
                        (cluster_id, active_uuids),
                    )
                    total += cur.rowcount

                    if stale_names:
                        cur.execute(
                            '''DELETE FROM recommendations.forecast_snapshots
                               WHERE cluster_id = %s
                                 AND resource_dimension = 'network'
                                 AND entity_label = ANY(%s)''',
                            (cluster_id, stale_names),
                        )
                        total += cur.rowcount

            if total:
                log.info(
                    'Pruned stale VM records',
                    extra={'deleted': total, 'stale_vms': stale_names},
                )
        except Exception:
            log.exception('prune_stale_vms failed')

        return total

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

    def prune_audit(self, retention_months: int) -> int:
        """Delete this cluster's audit events (and their webhook delivery rows) older than
        retention_months, in batches. Events someone acted on (audit.action_log) are kept.
        Returns the number of events deleted."""
        months = max(1, int(retention_months or 12))
        total = 0
        try:
            while True:
                with db.connect(self._cfg) as conn:
                    with conn.cursor() as cur:
                        # one statement: the delivery rows that reference the events go first
                        # (foreign keys are checked at the end of the statement)
                        cur.execute(
                            '''
                            WITH old AS (
                                SELECT re.id FROM audit.recommendation_events re
                                 WHERE re.cluster_id = %s
                                   AND re.emitted_at < NOW() - make_interval(months => %s)
                                   AND NOT EXISTS (SELECT 1 FROM audit.action_log a
                                                    WHERE a.recommendation_event_id = re.id)
                                 LIMIT %s
                            ), deliveries AS (
                                DELETE FROM audit.webhook_deliveries
                                 WHERE recommendation_event_id IN (SELECT id FROM old)
                            )
                            DELETE FROM audit.recommendation_events WHERE id IN (SELECT id FROM old)
                            ''',
                            [self._cfg.cluster_id, months, _AUDIT_PRUNE_BATCH],
                        )
                        n = cur.rowcount
                total += max(0, n)
                if n < _AUDIT_PRUNE_BATCH:
                    break
            if total:
                log.info('Audit retention cleanup', extra={'deleted': total, 'retention_months': months})
        except Exception:
            log.exception('Audit retention cleanup failed')
        return total

    # ------------------------------------------------------------------
    # forecasts
    # ------------------------------------------------------------------

    def write_forecasts(self, forecasts: list[ForecastResult]) -> None:
        if not forecasts:
            return

        self.run_retention_cleanup()

        inserted_ids: list[tuple[int, ForecastResult]] = []

        try:
            with db.connect(self._cfg) as conn:
                with conn.cursor() as cur:
                    previous = _previous_state(cur, 'forecast_snapshot', forecasts[0].cluster_id)
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

        # write audit events (separate transaction — audit must not be lost
        # if the recommendations write somehow partially failed)
        self._emit_audit_events(inserted_ids, 'forecast_snapshot', 'recommendations.forecast_snapshots', previous)

        # derive and write resource exhaustion records from these forecasts
        self._write_exhaustion(forecasts)

    # ------------------------------------------------------------------
    # right-sizing
    # ------------------------------------------------------------------

    def write_rightsizing(self, results: list[RightSizingResult]) -> None:
        if not results:
            return

        inserted_ids: list[tuple[int, RightSizingResult]] = []

        try:
            with db.connect(self._cfg) as conn:
                with conn.cursor() as cur:
                    previous = _previous_state(cur, 'right_sizing', results[0].cluster_id)
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

        self._emit_audit_events(inserted_ids, 'right_sizing', 'recommendations.right_sizing', previous)

    # ------------------------------------------------------------------
    # resource exhaustion (derived from forecasts)
    # ------------------------------------------------------------------

    def _write_exhaustion(self, forecasts: list[ForecastResult]) -> None:
        """
        Extract exhaustion-relevant forecasts and write to resource_exhaustion table.
        Only forecasts with an exhaustion_date or CRITICAL/EMERGENCY severity are written.
        Deduplicated to the most severe forecast per (cluster, node, dimension).

        The cluster_id's existing rows are always deleted first — even when the
        current cycle produces no new rows — so stale exhaustion alerts from a
        previous cycle don't linger after the underlying trend has resolved.
        """
        if not forecasts:
            return

        # pick one record per (cluster_id, node_uuid, dimension) — most severe
        best: dict[tuple, ForecastResult] = {}
        severity_order = {'EMERGENCY': 5, 'CRITICAL': 4, 'WARNING': 3, 'ATTENTION': 2, 'INFO': 1}

        for f in forecasts:
            key = (f.cluster_id, f.node_uuid, f.resource_dimension)
            existing = best.get(key)
            if existing is None or severity_order[f.severity] > severity_order[existing.severity]:
                best[key] = f

        rows = [f for f in best.values() if f.exhaustion_date is not None or f.severity in ('CRITICAL', 'EMERGENCY')]
        cluster_id = forecasts[0].cluster_id

        inserted_ids = []
        try:
            with db.connect(self._cfg) as conn:
                with conn.cursor() as cur:
                    previous = _previous_state(cur, 'resource_exhaustion', cluster_id)
                    cur.execute(
                        'DELETE FROM recommendations.resource_exhaustion WHERE cluster_id = %s',
                        [cluster_id],
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
                                _usage_pct(f),
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

        self._emit_audit_events(inserted_ids, 'resource_exhaustion', 'recommendations.resource_exhaustion', previous)

    # ------------------------------------------------------------------
    # resize recommendations (C3-C6)
    # ------------------------------------------------------------------

    def write_resize_advice(self, advice: list[ResizeAdvice]) -> None:
        if not advice:
            return

        inserted_ids: list[tuple[int, ResizeAdvice]] = []

        try:
            with db.connect(self._cfg) as conn:
                with conn.cursor() as cur:
                    previous = _previous_state(cur, 'resize_recommendation', advice[0].cluster_id)
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
            inserted_ids, 'resize_recommendation', 'recommendations.resize_recommendations', previous
        )

    # ------------------------------------------------------------------
    # cluster health (D1-D4, D6)
    # ------------------------------------------------------------------

    def write_cluster_health(self, health: ClusterHealthResult) -> None:
        inserted_ids: list[tuple[int, ClusterHealthResult]] = []

        try:
            with db.connect(self._cfg) as conn:
                with conn.cursor() as cur:
                    previous = _previous_state(cur, 'cluster_health', health.cluster_id)
                    cur.execute(
                        '''
                        INSERT INTO recommendations.cluster_health_scores
                            (cluster_id, health_score, bottleneck_resource,
                             growth_rates, efficiency_index, component_scores,
                             component_reasons, cluster_count, severity)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING id
                        ''',
                        (
                            health.cluster_id,
                            health.health_score,
                            health.bottleneck_resource,
                            json.dumps(health.growth_rates),
                            health.efficiency_index,
                            json.dumps(health.component_scores),
                            json.dumps(health.component_reasons),
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
            inserted_ids, 'cluster_health', 'recommendations.cluster_health_scores', previous
        )

    # ------------------------------------------------------------------
    # audit
    # ------------------------------------------------------------------

    def _emit_audit_events(
        self,
        inserted: list[tuple[int, object]],
        event_type: str,
        source_table: str,
        previous: dict | None = None,
    ) -> None:
        """Write an audit event for each inserted recommendation row that is news: its key was
        not among the previous cycle's rows (`previous`, {key: state}) or its severity or
        direction changed. INFO forecast snapshots are never audited."""
        changed = []
        for row_id, obj in inserted:
            if event_type == 'forecast_snapshot' and getattr(obj, 'severity', 'INFO') == 'INFO':
                continue
            key, state = _identity(event_type, obj)
            if previous is not None and previous.get(key) == state:
                continue
            changed.append((row_id, obj))
        inserted = changed
        if not inserted:
            return
        try:
            with db.connect(self._cfg) as conn:
                with conn.cursor() as cur:
                    for row_id, obj in inserted:
                        severity = getattr(obj, 'severity', 'INFO')
                        cluster_id = getattr(obj, 'cluster_id', self._cfg.cluster_id)

                        # build a JSON snapshot of the object for the audit record
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
