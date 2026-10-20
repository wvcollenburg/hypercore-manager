"""
api.py — Foresight REST API (F1–F4, F6).

Runs as a Flask application on a dedicated port (default 8081), started as
a daemon thread alongside the health endpoint.

Authentication (F4):
    Bearer token — set SENTINEL_API_TOKEN in the environment.
    If the env var is empty, authentication is disabled (dev mode).

Endpoints:
    F1  GET /api/v1/forecasts          — latest forecast snapshots
    F2  GET /api/v1/rightsizing        — latest right-sizing results
        GET /api/v1/resize             — concrete resize recommendations (C3-C6)
    F3  GET /api/v1/cluster-health     — cluster health score + breakdown (D1-D4,D6)
        GET /api/v1/exhaustion         — resource exhaustion alerts (B6)
    F6  GET /api/v1/summary            — single-call overview of all dimensions
    C7  GET /api/v1/outcomes           — right-sizing outcome history
    D5  GET /api/v1/vms                — VM list with available profile data (for what-if)
        POST /api/v1/whatif            — what-if scenario simulation

Common query parameters (all endpoints):
    limit  — max rows to return (1–1000, default 100)
    offset — skip N rows for pagination (default 0)

Filter parameters (where documented per endpoint):
    /forecasts:   ?dimension=cpu&horizon=30
    /rightsizing: ?direction=over_provisioned&severity=WARNING

All responses are JSON.  Timestamps are ISO-8601 UTC strings.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime, timezone
from functools import wraps

import psycopg2.extras
from flask import Flask, jsonify, request

import config as cfg_module
import db
from influx_client import InfluxReader

log = logging.getLogger(__name__)

_api_app = Flask('sentinel.api')


# ---------------------------------------------------------------------------
# F4 — authentication
# ---------------------------------------------------------------------------

def _require_token(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = _api_app.config.get('API_TOKEN', '')
        if not token:
            return f(*args, **kwargs)   # dev mode — no token configured
        auth = request.headers.get('Authorization', '')
        if not auth.startswith('Bearer ') or auth[7:] != token:
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _cfg() -> cfg_module.Config:
    """Config of the cluster this request is about: ?cluster=<uuid> when it names a running
    engine, otherwise the first engine. The dict is owned and updated by sentinel.py."""
    cfgs: dict = _api_app.config.get('SENTINEL_CFGS') or {}
    wanted = (request.args.get('cluster') or '').strip()
    if wanted and wanted in cfgs:
        return cfgs[wanted]
    if cfgs:
        return next(iter(cfgs.values()))
    return _api_app.config['SENTINEL_CFG']   # no cluster registered yet: base config, no data


def _pagination() -> tuple[int, int]:
    try:
        limit  = max(1, min(int(request.args.get('limit',  100)), 1000))
        offset = max(0, int(request.args.get('offset', 0)))
    except (ValueError, TypeError):
        limit, offset = 100, 0
    return limit, offset


def _rows_to_dicts(rows, description) -> list[dict]:
    """Convert psycopg2 rows to JSON-serialisable dicts."""
    cols   = [d.name for d in description]
    result = []
    for row in rows:
        d = {}
        for k, v in zip(cols, row):
            if isinstance(v, (date, datetime)):
                d[k] = v.isoformat()
            else:
                d[k] = v
        result.append(d)
    return result


# ---------------------------------------------------------------------------
# F1 — forecasts
# ---------------------------------------------------------------------------

@_api_app.route('/api/v1/forecasts')
@_require_token
def get_forecasts():
    """
    Latest forecast snapshots.
    Optional filters: ?dimension=cpu  ?horizon=30  ?severity=WARNING
    """
    cfg           = _cfg()
    limit, offset = _pagination()
    dimension     = request.args.get('dimension')
    horizon       = request.args.get('horizon')
    severity      = request.args.get('severity')

    try:
        with db.connect(cfg) as conn:
            with conn.cursor() as cur:
                where  = ['cluster_id = %s']
                params: list = [cfg.cluster_id]
                if dimension:
                    where.append('resource_dimension = %s'); params.append(dimension)
                if horizon:
                    where.append('forecast_horizon_days = %s'); params.append(int(horizon))
                if severity:
                    where.append('severity = %s'); params.append(severity)
                params += [limit, offset]

                cur.execute(
                    f'''
                    SELECT id, cluster_id, node_uuid, entity_label, resource_dimension,
                           forecast_horizon_days, current_usage, current_capacity,
                           forecast_value, confidence_score, driving_metric,
                           exhaustion_date, severity, model_metadata, snapshot_at
                    FROM recommendations.forecast_snapshots
                    WHERE {" AND ".join(where)}
                      AND expires_at > NOW()
                    ORDER BY snapshot_at DESC
                    LIMIT %s OFFSET %s
                    ''',
                    params,
                )
                rows = _rows_to_dicts(cur.fetchall(), cur.description)
        return jsonify(rows)
    except Exception as e:
        log.exception('GET /api/v1/forecasts failed')
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# F2 — right-sizing + resize recommendations
# ---------------------------------------------------------------------------

@_api_app.route('/api/v1/rightsizing')
@_require_token
def get_rightsizing():
    """
    Latest right-sizing results (C1, C2).
    Optional filters: ?direction=over_provisioned  ?severity=WARNING
    """
    cfg           = _cfg()
    limit, offset = _pagination()
    direction     = request.args.get('direction')
    severity      = request.args.get('severity')

    try:
        with db.connect(cfg) as conn:
            with conn.cursor() as cur:
                where  = ['cluster_id = %s']
                params: list = [cfg.cluster_id]
                if direction:
                    where.append('sizing_direction = %s'); params.append(direction)
                if severity:
                    where.append('severity = %s'); params.append(severity)
                params += [limit, offset]

                cur.execute(
                    f'''
                    SELECT id, cluster_id, vm_uuid, vm_name, node_uuid,
                           sizing_direction, severity, analysis_window_days,
                           cpu_signal_score, cpu_avg_usage_pct, cpu_p95_usage_pct,
                           latency_signal_score, spike_signal_score,
                           composite_score, signal_detail, current_vcpu, detected_at
                    FROM recommendations.right_sizing
                    WHERE {" AND ".join(where)}
                      AND expires_at > NOW()
                    ORDER BY composite_score DESC, detected_at DESC
                    LIMIT %s OFFSET %s
                    ''',
                    params,
                )
                rows = _rows_to_dicts(cur.fetchall(), cur.description)
        return jsonify(rows)
    except Exception as e:
        log.exception('GET /api/v1/rightsizing failed')
        return jsonify({'error': str(e)}), 500


@_api_app.route('/api/v1/resize')
@_require_token
def get_resize_recommendations():
    """Concrete resize recommendations (C3-C6)."""
    cfg           = _cfg()
    limit, offset = _pagination()
    direction     = request.args.get('direction')
    risk          = request.args.get('risk')

    try:
        with db.connect(cfg) as conn:
            with conn.cursor() as cur:
                where  = ['cluster_id = %s']
                params: list = [cfg.cluster_id]
                if direction:
                    where.append('sizing_direction = %s'); params.append(direction)
                if risk:
                    where.append('risk_level = %s'); params.append(risk)
                params += [limit, offset]

                cur.execute(
                    f'''
                    SELECT id, cluster_id, vm_uuid, vm_name, node_uuid,
                           sizing_direction, suggested_cpu_change_pct, target_cpu_avg_pct,
                           confidence_score, capacity_reclaim_cpu_pct,
                           risk_level, risk_detail, severity,
                           current_vcpu, recommended_vcpu, detected_at
                    FROM recommendations.resize_recommendations
                    WHERE {" AND ".join(where)}
                      AND expires_at > NOW()
                    ORDER BY confidence_score DESC, detected_at DESC
                    LIMIT %s OFFSET %s
                    ''',
                    params,
                )
                rows = _rows_to_dicts(cur.fetchall(), cur.description)
        return jsonify(rows)
    except Exception as e:
        log.exception('GET /api/v1/resize failed')
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# F3 — cluster health + resource exhaustion
# ---------------------------------------------------------------------------

@_api_app.route('/api/v1/cluster-health')
@_require_token
def get_cluster_health():
    """Latest cluster health scores (D1-D4, D6)."""
    cfg           = _cfg()
    limit, offset = _pagination()

    try:
        with db.connect(cfg) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    '''
                    SELECT id, cluster_id, health_score, bottleneck_resource,
                           growth_rates, efficiency_index, component_scores,
                           cluster_count, severity, calculated_at
                    FROM recommendations.cluster_health_scores
                    WHERE cluster_id = %s
                      AND expires_at > NOW()
                    ORDER BY calculated_at DESC
                    LIMIT %s OFFSET %s
                    ''',
                    [cfg.cluster_id, limit, offset],
                )
                rows = _rows_to_dicts(cur.fetchall(), cur.description)
        return jsonify(rows)
    except Exception as e:
        log.exception('GET /api/v1/cluster-health failed')
        return jsonify({'error': str(e)}), 500


@_api_app.route('/api/v1/exhaustion')
@_require_token
def get_exhaustion():
    """Resource exhaustion alerts, ordered most-urgent first."""
    cfg           = _cfg()
    limit, offset = _pagination()
    severity      = request.args.get('severity')

    try:
        with db.connect(cfg) as conn:
            with conn.cursor() as cur:
                where  = ['cluster_id = %s', 'expires_at > NOW()']
                params: list = [cfg.cluster_id]
                if severity:
                    where.append('severity = %s'); params.append(severity)
                params += [limit, offset]

                cur.execute(
                    f'''
                    SELECT id, cluster_id, node_uuid, resource_dimension,
                           days_until_exhaustion, exhaustion_date,
                           current_usage_pct, growth_rate_per_day, severity, calculated_at
                    FROM recommendations.resource_exhaustion
                    WHERE {" AND ".join(where)}
                    ORDER BY
                        CASE severity
                            WHEN 'EMERGENCY' THEN 1
                            WHEN 'CRITICAL'  THEN 2
                            WHEN 'WARNING'   THEN 3
                            ELSE 4
                        END,
                        days_until_exhaustion ASC NULLS LAST
                    LIMIT %s OFFSET %s
                    ''',
                    params,
                )
                rows = _rows_to_dicts(cur.fetchall(), cur.description)
        return jsonify(rows)
    except Exception as e:
        log.exception('GET /api/v1/exhaustion failed')
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# F6 — summary
# ---------------------------------------------------------------------------

@_api_app.route('/api/v1/summary')
@_require_token
def get_summary():
    """Single-call overview: health, sizing counts, worst exhaustion, forecast count."""
    cfg = _cfg()

    try:
        with db.connect(cfg) as conn:
            with conn.cursor() as cur:
                # latest cluster health score
                cur.execute(
                    '''
                    SELECT health_score, bottleneck_resource, efficiency_index,
                           severity, calculated_at
                    FROM recommendations.cluster_health_scores
                    WHERE cluster_id = %s AND expires_at > NOW()
                    ORDER BY calculated_at DESC LIMIT 1
                    ''',
                    [cfg.cluster_id],
                )
                health_row = cur.fetchone()

                # right-sizing counts by direction
                cur.execute(
                    '''
                    SELECT sizing_direction, COUNT(*) AS cnt
                    FROM recommendations.right_sizing
                    WHERE cluster_id = %s AND expires_at > NOW()
                    GROUP BY sizing_direction
                    ''',
                    [cfg.cluster_id],
                )
                sizing = {row[0]: row[1] for row in cur.fetchall()}

                # worst active exhaustion alert
                cur.execute(
                    '''
                    SELECT resource_dimension, days_until_exhaustion, severity
                    FROM recommendations.resource_exhaustion
                    WHERE cluster_id = %s AND expires_at > NOW()
                    ORDER BY
                        CASE severity
                            WHEN 'EMERGENCY' THEN 1 WHEN 'CRITICAL' THEN 2
                            WHEN 'WARNING'   THEN 3 ELSE 4
                        END,
                        days_until_exhaustion ASC NULLS LAST
                    LIMIT 1
                    ''',
                    [cfg.cluster_id],
                )
                exhaustion_row = cur.fetchone()

                # active forecast count
                cur.execute(
                    '''SELECT COUNT(*) FROM recommendations.forecast_snapshots
                       WHERE cluster_id = %s AND expires_at > NOW()''',
                    [cfg.cluster_id],
                )
                forecast_count = cur.fetchone()[0]

                # resize advice counts by risk level
                cur.execute(
                    '''
                    SELECT risk_level, COUNT(*) AS cnt
                    FROM recommendations.resize_recommendations
                    WHERE cluster_id = %s AND expires_at > NOW()
                    GROUP BY risk_level
                    ''',
                    [cfg.cluster_id],
                )
                resize_risk = {row[0]: row[1] for row in cur.fetchall()}

        return jsonify({
            'cluster_id': cfg.cluster_id,
            'cluster_health': {
                'score':      health_row[0] if health_row else None,
                'bottleneck': health_row[1] if health_row else None,
                'efficiency': health_row[2] if health_row else None,
                'severity':   health_row[3] if health_row else None,
                'as_of':      health_row[4].isoformat() if health_row and health_row[4] else None,
            },
            'right_sizing': {
                'over_provisioned':  sizing.get('over_provisioned',  0),
                'under_provisioned': sizing.get('under_provisioned', 0),
            },
            'resize_risk': resize_risk,
            'worst_exhaustion': {
                'dimension': exhaustion_row[0],
                'days':      exhaustion_row[1],
                'severity':  exhaustion_row[2],
            } if exhaustion_row else None,
            'active_forecasts': forecast_count,
        })
    except Exception as e:
        log.exception('GET /api/v1/summary failed')
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# C7 — right-sizing outcomes
# ---------------------------------------------------------------------------

@_api_app.route('/api/v1/outcomes')
@_require_token
def get_outcomes():
    """
    Right-sizing outcome history (C7).

    Returns audit.recommendation_events rows for event_type = 'right_sizing'
    that have been resolved (acted_on_at IS NOT NULL) unless ?pending=true is
    passed, in which case unresolved records are returned instead.

    Optional filters:
        ?outcome=resized_up|resized_down|no_action
        ?pending=true   (return unresolved records instead)
        ?vm_uuid=<uuid>
    """
    cfg           = _cfg()
    limit, offset = _pagination()
    outcome_filter = request.args.get('outcome')
    pending_only   = request.args.get('pending', '').lower() in ('true', '1', 'yes')
    vm_uuid_filter = request.args.get('vm_uuid')

    try:
        with db.connect(cfg) as conn:
            with conn.cursor() as cur:
                where  = ['cluster_id = %s', "event_type = 'right_sizing'"]
                params: list = [cfg.cluster_id]

                if pending_only:
                    where.append('acted_on_at IS NULL')
                else:
                    where.append('acted_on_at IS NOT NULL')
                    if outcome_filter:
                        where.append('action_outcome = %s')
                        params.append(outcome_filter)

                if vm_uuid_filter:
                    where.append("payload_snapshot->>'vm_uuid' = %s")
                    params.append(vm_uuid_filter)

                params += [limit, offset]

                cur.execute(
                    f'''
                    SELECT id, cluster_id, source_id, source_table,
                           severity, emitted_at, payload_snapshot,
                           acted_on_at, action_outcome, post_action_snapshot
                    FROM audit.recommendation_events
                    WHERE {" AND ".join(where)}
                    ORDER BY emitted_at DESC
                    LIMIT %s OFFSET %s
                    ''',
                    params,
                )
                rows = _rows_to_dicts(cur.fetchall(), cur.description)
        return jsonify(rows)
    except Exception as e:
        log.exception('GET /api/v1/outcomes failed')
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# D5 — VM list + what-if scenario modelling
# ---------------------------------------------------------------------------

@_api_app.route('/api/v1/vms')
@_require_token
def get_vms():
    """
    List of VMs with available profile data for add_vms_like what-if simulation.

    Each entry reports which data sources are available:
      has_cpu_data     — VM appears in right_sizing (utilisation %)
      has_network_data — VM has a network forecast row (bytes)
    """
    cfg = _cfg()

    try:
        with db.connect(cfg) as conn:
            with conn.cursor() as cur:
                # VMs with CPU utilisation data from right_sizing
                cur.execute(
                    '''
                    SELECT vm_name, MAX(cpu_avg_usage_pct) AS cpu_avg_pct
                    FROM recommendations.right_sizing
                    WHERE cluster_id = %s AND expires_at > NOW()
                    GROUP BY vm_name
                    ORDER BY vm_name
                    ''',
                    [cfg.cluster_id],
                )
                rs_rows = {r[0]: float(r[1]) if r[1] is not None else None
                           for r in cur.fetchall()}

                # VMs with network forecast data
                cur.execute(
                    '''
                    SELECT entity_label, MAX(current_usage) AS net_bytes
                    FROM recommendations.forecast_snapshots
                    WHERE cluster_id = %s AND expires_at > NOW()
                      AND resource_dimension = 'network'
                      AND entity_label IS NOT NULL
                      AND entity_label NOT LIKE 'Node %%'
                    GROUP BY entity_label
                    ORDER BY entity_label
                    ''',
                    [cfg.cluster_id],
                )
                net_rows = {r[0]: float(r[1]) if r[1] is not None else None
                            for r in cur.fetchall()}

        all_names = sorted(set(rs_rows) | set(net_rows))
        vms = [
            {
                'vm_name':          name,
                'has_cpu_data':     name in rs_rows,
                'has_network_data': name in net_rows,
                'cpu_avg_usage_pct': rs_rows.get(name),
                'network_bytes':     net_rows.get(name),
            }
            for name in all_names
        ]
        return jsonify(vms)

    except Exception as e:
        log.exception('GET /api/v1/vms failed')
        return jsonify({'error': str(e)}), 500


def _fetch_source_vm_profile(cfg, vm_name: str, conn) -> tuple[dict | None, str | None]:
    """
    Fetch the resource profile for a named VM from PostgreSQL.

    Returns (profile_dict, None) on success, or (None, error_message) if the
    VM cannot be found in either right_sizing or network forecast data.

    profile_dict keys:
      vm_name          — str
      vm_cpu_avg_pct   — float | None  (VM's own CPU utilisation %)
      cpu_fraction     — float | None  (VM's share of sum of all VM CPU util %)
      network_bytes    — float | None  (VM's current_usage from network forecast)
      network_fraction — float | None  (VM's share of total cluster network)
    """
    cluster_id = cfg.cluster_id

    with conn.cursor() as cur:
        # VM's latest CPU utilisation and UUID from right_sizing
        cur.execute(
            '''
            SELECT vm_uuid, cpu_avg_usage_pct
            FROM recommendations.right_sizing
            WHERE cluster_id = %s AND vm_name = %s AND expires_at > NOW()
            ORDER BY detected_at DESC LIMIT 1
            ''',
            (cluster_id, vm_name),
        )
        rs_row = cur.fetchone()
        vm_uuid        = str(rs_row[0]) if rs_row else None
        vm_cpu_avg_pct = float(rs_row[1]) if rs_row and rs_row[1] is not None else None

        # sum of all VMs' CPU utilisation (denominator for fraction)
        total_cpu_pct = None
        if vm_cpu_avg_pct is not None:
            cur.execute(
                '''
                SELECT SUM(cpu_avg_usage_pct)
                FROM recommendations.right_sizing
                WHERE cluster_id = %s AND expires_at > NOW()
                ''',
                (cluster_id,),
            )
            row = cur.fetchone()
            total_cpu_pct = float(row[0]) if row and row[0] else None

        # VM's latest network current_usage
        cur.execute(
            '''
            SELECT current_usage
            FROM recommendations.forecast_snapshots
            WHERE cluster_id = %s AND entity_label = %s
              AND resource_dimension = 'network' AND expires_at > NOW()
            ORDER BY snapshot_at DESC LIMIT 1
            ''',
            (cluster_id, vm_name),
        )
        net_row = cur.fetchone()
        vm_network_bytes = float(net_row[0]) if net_row and net_row[0] is not None else None

        # total cluster network from the most recent snapshot window
        total_network_bytes = None
        if vm_network_bytes is not None:
            cur.execute(
                '''
                SELECT SUM(current_usage)
                FROM recommendations.forecast_snapshots
                WHERE cluster_id = %s AND resource_dimension = 'network'
                  AND expires_at > NOW()
                  AND snapshot_at >= (
                      SELECT MAX(snapshot_at) - INTERVAL '1 hour'
                      FROM recommendations.forecast_snapshots
                      WHERE cluster_id = %s
                        AND resource_dimension = 'network'
                        AND expires_at > NOW()
                  )
                ''',
                (cluster_id, cluster_id),
            )
            tot = cur.fetchone()
            total_network_bytes = float(tot[0]) if tot and tot[0] else None

    if vm_cpu_avg_pct is None and vm_network_bytes is None:
        return None, (
            f'No profile data found for VM "{vm_name}". '
            'It does not appear in right-sizing results or network forecasts.'
        )

    cpu_fraction = (
        vm_cpu_avg_pct / total_cpu_pct
        if vm_cpu_avg_pct is not None and total_cpu_pct and total_cpu_pct > 0
        else None
    )
    network_fraction = (
        vm_network_bytes / total_network_bytes
        if vm_network_bytes is not None and total_network_bytes and total_network_bytes > 0
        else None
    )

    # fetch the VM's allocated RAM from InfluxDB vm_inventory (mem_bytes field)
    # this is needed so the what-if RAM dimension uses the source VM's actual
    # allocation rather than a cluster average
    mem_bytes_allocated: int | None = None
    if vm_uuid:
        try:
            reader = InfluxReader(cfg)
            mem_map = reader.query_vm_mem_assigned()
            raw = mem_map.get(vm_uuid)
            if raw:
                mem_bytes_allocated = int(raw)
        except Exception:
            log.warning(
                'Could not fetch VM RAM allocation for what-if; '
                'RAM contribution will fall back to cluster average',
                extra={'vm_name': vm_name},
            )

    return {
        'vm_name':            vm_name,
        'vm_cpu_avg_pct':     vm_cpu_avg_pct,
        'cpu_fraction':       cpu_fraction,
        'network_bytes':      vm_network_bytes,
        'network_fraction':   network_fraction,
        'mem_bytes_allocated': mem_bytes_allocated,
    }, None


@_api_app.route('/api/v1/whatif', methods=['POST'])
@_require_token
def post_whatif():
    """
    D5: What-if scenario simulation.

    Request body (JSON):
    {
        "scenario": "add_nodes" | "add_vms" | "add_vms_like",
        "count": <int>,                        // number of nodes or VMs to add
        // add_nodes only (all optional — defaults to cluster average):
        "node_cpu_headroom_pct": <float>,      // assumed avg CPU % free per new node
        "node_storage_bytes":    <int>,        // assumed storage bytes per new node
        "node_ram_bytes":        <int>,        // assumed RAM bytes per new node
        // add_vms_like only (required):
        "source_vm_name":        <str>         // template VM whose profile to replicate
    }
    """
    from dataclasses import asdict
    from analysis.whatif import WhatIfRequest, run_whatif

    cfg = _cfg()
    body = request.get_json(silent=True) or {}

    scenario = body.get('scenario')
    if scenario not in ('add_nodes', 'add_vms', 'add_vms_like'):
        return jsonify({'error': 'scenario must be "add_nodes", "add_vms", or "add_vms_like"'}), 400
    try:
        count = int(body.get('count', 0))
        if count < 1:
            raise ValueError
    except (ValueError, TypeError):
        return jsonify({'error': 'count must be a positive integer'}), 400

    source_vm_name = body.get('source_vm_name', '').strip() if scenario == 'add_vms_like' else None
    if scenario == 'add_vms_like' and not source_vm_name:
        return jsonify({'error': 'source_vm_name is required for add_vms_like'}), 400

    req = WhatIfRequest(
        scenario=scenario,
        count=count,
        node_cpu_headroom_pct=body.get('node_cpu_headroom_pct'),
        node_storage_bytes=body.get('node_storage_bytes'),
        node_ram_bytes=body.get('node_ram_bytes'),
        source_vm_name=source_vm_name,
    )

    try:
        with db.connect(cfg) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # latest cluster health row
                cur.execute(
                    '''
                    SELECT health_score, bottleneck_resource, efficiency_index,
                           severity, growth_rates
                    FROM recommendations.cluster_health_scores
                    WHERE cluster_id = %s AND expires_at > NOW()
                    ORDER BY calculated_at DESC LIMIT 1
                    ''',
                    [cfg.cluster_id],
                )
                health_row = dict(cur.fetchone() or {})

                # latest forecast snapshots (30d horizon — for growth rates)
                cur.execute(
                    '''
                    SELECT resource_dimension, forecast_horizon_days,
                           current_usage, current_capacity, model_metadata
                    FROM recommendations.forecast_snapshots
                    WHERE cluster_id = %s AND expires_at > NOW()
                      AND forecast_horizon_days = 30
                    ORDER BY snapshot_at DESC
                    ''',
                    [cfg.cluster_id],
                )
                forecast_rows = [dict(r) for r in cur.fetchall()]

                # latest resource exhaustion rows
                cur.execute(
                    '''
                    SELECT resource_dimension, exhaustion_date,
                           days_until_exhaustion, severity
                    FROM recommendations.resource_exhaustion
                    WHERE cluster_id = %s AND expires_at > NOW()
                    ORDER BY calculated_at DESC
                    ''',
                    [cfg.cluster_id],
                )
                exhaustion_rows = [dict(r) for r in cur.fetchall()]

                # node count: distinct node_uuids with a cpu forecast
                cur.execute(
                    '''
                    SELECT COUNT(DISTINCT node_uuid)
                    FROM recommendations.forecast_snapshots
                    WHERE cluster_id = %s AND expires_at > NOW()
                      AND resource_dimension = 'cpu'
                    ''',
                    [cfg.cluster_id],
                )
                node_count = (cur.fetchone() or {}).get('count') or 1

                # VM count: distinct entities with a network forecast
                cur.execute(
                    '''
                    SELECT COUNT(DISTINCT entity_label)
                    FROM recommendations.forecast_snapshots
                    WHERE cluster_id = %s AND expires_at > NOW()
                      AND resource_dimension = 'network'
                    ''',
                    [cfg.cluster_id],
                )
                vm_count = (cur.fetchone() or {}).get('count') or 0

            # fetch source VM profile for add_vms_like
            source_vm_profile = None
            if scenario == 'add_vms_like':
                source_vm_profile, err = _fetch_source_vm_profile(cfg, source_vm_name, conn)
                if err:
                    return jsonify({'error': err}), 404

        pg_state = {
            'health_row':        health_row,
            'forecast_rows':     forecast_rows,
            'exhaustion_rows':   exhaustion_rows,
            'node_count':        node_count,
            'vm_count':          vm_count,
            'source_vm_profile': source_vm_profile,
        }

        result = run_whatif(req, pg_state)
        return jsonify(asdict(result))

    except Exception as e:
        log.exception('POST /api/v1/whatif failed')
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# T2 — live tunables (phase 3.5)
# ---------------------------------------------------------------------------

@_api_app.route('/api/v1/tunables', methods=['GET'])
@_require_token
def get_tunables():
    """Return all Sentinel live-tunable settings with metadata."""
    import tunables as _tunables
    return jsonify(_tunables.get_with_meta(_cfg().cluster_id))


@_api_app.route('/api/v1/tunables', methods=['PUT'])
@_require_token
def put_tunables():
    """
    Update one or more Sentinel live-tunable settings.

    Request body: { "SENTINEL_KEY": value, ... }
    Unknown keys are rejected. Type coercion is applied then range-validated.
    Returns the full updated tunables list on success.
    """
    import tunables as _tunables

    body = request.get_json(silent=True) or {}
    errors = {}
    validated = {}

    for key, raw_val in body.items():
        if key not in _tunables.TUNABLES:
            errors[key] = 'Unknown tunable'
            continue
        defn = _tunables.TUNABLES[key]
        try:
            val = _tunables._cast(raw_val, defn.type_hint)
            # enum constraint
            if defn.type_hint == 'enum' and defn.allowed_values and val not in defn.allowed_values:
                raise ValueError(f'Must be one of: {", ".join(defn.allowed_values)}')
            # range checks for numeric keys
            if defn.type_hint in ('float', 'int') and defn.unit == '%':
                if not (0 <= val <= 100):
                    raise ValueError('Must be between 0 and 100')
            if key == 'SENTINEL_ANALYSIS_WINDOW_DAYS' and val < 1:
                raise ValueError('Must be >= 1')
            if key == 'SENTINEL_RECURRING_SPIKE_MIN_OCCURRENCES' and val < 1:
                raise ValueError('Must be >= 1')
            if key == 'SENTINEL_RETENTION_MONTHS' and val < 1:
                raise ValueError('Must be >= 1')
            if key == 'SENTINEL_OUTCOME_CHECK_DELAY_HOURS' and val < 0:
                raise ValueError('Must be >= 0')
            validated[key] = val
        except (ValueError, TypeError) as e:
            errors[key] = str(e)

    if errors:
        return jsonify({'error': 'Validation failed', 'details': errors}), 400

    cid = _cfg().cluster_id
    _tunables.save_many(validated, cid)
    return jsonify(_tunables.get_with_meta(cid))


# ---------------------------------------------------------------------------
# H3 — sharing preferences (GET/PUT)
# ---------------------------------------------------------------------------

@_api_app.route('/api/v1/sharing', methods=['GET'])
@_require_token
def get_sharing():
    """
    H3: Return the current sharing configuration.

    Returns all sharing_prefs fields except bearer_token and anon_hmac_key
    (secrets are never returned).
    """
    cfg = _cfg()
    try:
        with db.connect(cfg) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    '''
                    SELECT sharing_enabled, viewer_url,
                           last_sync_at, last_sync_status, last_sync_error,
                           anon_vm_names, anon_node_uuids, anon_cluster_name
                    FROM config.sharing_prefs
                    WHERE cluster_id = %s
                    LIMIT 1
                    ''',
                    [cfg.cluster_id],
                )
                row = cur.fetchone()
    except Exception as e:
        log.exception('GET /api/v1/sharing failed')
        return jsonify({'error': str(e)}), 500

    if not row:
        return jsonify({
            'sharing_enabled':  False,
            'viewer_url':       None,
            'last_sync_at':     None,
            'last_sync_status': None,
            'last_sync_error':  None,
            'anon_vm_names':    False,
            'anon_node_uuids':  False,
            'anon_cluster_name': False,
        })

    sharing_enabled, viewer_url, last_sync_at, last_sync_status, last_sync_error, \
        anon_vm_names, anon_node_uuids, anon_cluster_name = row

    from datetime import date
    return jsonify({
        'sharing_enabled':  bool(sharing_enabled),
        'viewer_url':       viewer_url,
        'last_sync_at':     last_sync_at.isoformat() if isinstance(last_sync_at, (date, datetime)) else last_sync_at,
        'last_sync_status': last_sync_status,
        'last_sync_error':  last_sync_error,
        'anon_vm_names':    bool(anon_vm_names),
        'anon_node_uuids':  bool(anon_node_uuids),
        'anon_cluster_name': bool(anon_cluster_name),
    })


@_api_app.route('/api/v1/sharing', methods=['PUT'])
@_require_token
def put_sharing():
    """
    H3: Update sharing configuration.

    Accepts a JSON body with any subset of the writable fields.
    bearer_token is accepted but never returned.
    Returns the same shape as GET on success.
    """
    cfg  = _cfg()
    body = request.get_json(silent=True) or {}

    # validate viewer_url if provided
    viewer_url = body.get('viewer_url', ...)
    if viewer_url is not ... and viewer_url and not str(viewer_url).startswith('https://'):
        return jsonify({'error': 'viewer_url must start with https://'}), 400

    allowed = {
        'sharing_enabled',
        'viewer_url',
        'bearer_token',
        'anon_vm_names',
        'anon_node_uuids',
        'anon_cluster_name',
    }
    updates = {k: v for k, v in body.items() if k in allowed}
    if not updates:
        return jsonify({'error': 'No valid fields provided'}), 400

    # build SET clause
    set_parts = []
    params = []
    for k, v in updates.items():
        set_parts.append(f'{k} = %s')
        params.append(v)
    set_parts.append('updated_at = NOW()')
    params.append(cfg.cluster_id)

    try:
        with db.connect(cfg) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE config.sharing_prefs SET {', '.join(set_parts)} WHERE cluster_id = %s",
                    params,
                )
    except Exception as e:
        log.exception('PUT /api/v1/sharing failed')
        return jsonify({'error': str(e)}), 500

    # return the same shape as GET (re-query to get current state)
    try:
        with db.connect(cfg) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    '''
                    SELECT sharing_enabled, viewer_url,
                           last_sync_at, last_sync_status, last_sync_error,
                           anon_vm_names, anon_node_uuids, anon_cluster_name
                    FROM config.sharing_prefs
                    WHERE cluster_id = %s
                    LIMIT 1
                    ''',
                    [cfg.cluster_id],
                )
                row = cur.fetchone()
    except Exception as e:
        log.exception('PUT /api/v1/sharing: re-read failed')
        return jsonify({'error': str(e)}), 500

    sharing_enabled, viewer_url, last_sync_at, last_sync_status, last_sync_error, \
        anon_vm_names, anon_node_uuids, anon_cluster_name = row

    from datetime import date
    return jsonify({
        'sharing_enabled':  bool(sharing_enabled),
        'viewer_url':       viewer_url,
        'last_sync_at':     last_sync_at.isoformat() if isinstance(last_sync_at, (date, datetime)) else last_sync_at,
        'last_sync_status': last_sync_status,
        'last_sync_error':  last_sync_error,
        'anon_vm_names':    bool(anon_vm_names),
        'anon_node_uuids':  bool(anon_node_uuids),
        'anon_cluster_name': bool(anon_cluster_name),
    })


# ---------------------------------------------------------------------------
# H1/H2 — export snapshot + test-push
# ---------------------------------------------------------------------------

@_api_app.route('/api/v1/export/snapshot')
@_require_token
def get_export_snapshot():
    """
    H1: Return the current full snapshot as JSON for local inspection.

    Builds a full (non-delta) snapshot from the current PostgreSQL state,
    optionally anonymises it according to the customer's sharing_prefs, and
    returns the result.  The payload is *not* signed — this endpoint is for
    debugging and local verification only.

    The raw (pre-anonymisation) snapshot is what is returned; anonymisation is
    applied on the copy that would be transmitted, not on the response here.
    """
    import anonymiser as _anonymiser
    import exporter as _exporter

    cfg = _cfg()
    try:
        snapshot = _exporter.build_snapshot(cfg)
        anon_cfg = _anonymiser.load_anon_config(cfg)
        if anon_cfg.enabled:
            snapshot = _anonymiser.anonymise_snapshot(snapshot, anon_cfg)
        return jsonify(snapshot)
    except Exception as e:
        log.exception('GET /api/v1/export/snapshot failed')
        return jsonify({'error': str(e)}), 500


@_api_app.route('/api/v1/trend/run', methods=['POST'])
@_require_token
def post_trend_run():
    """
    Trigger an immediate analysis cycle.

    Signals the analysis loop to wake from its trend-interval sleep and run
    the next forecast pass right away.  Useful after a config change or
    forecaster code update so operators don't have to wait out the full
    trend_interval_seconds (default 1 hour).

    Returns 503 if the trigger callable was not wired up at startup.
    """
    trigger = _api_app.config.get('TRIGGER_ANALYSIS')
    if trigger is None:
        return jsonify({
            'success': False,
            'error':   'Trigger not configured — analysis loop is not running.',
        }), 503
    try:
        trigger(_cfg().cluster_id)
    except Exception as e:
        log.exception('POST /api/v1/trend/run: trigger failed')
        return jsonify({'success': False, 'error': str(e)}), 500
    log.info('Analysis cycle requested via API')
    return jsonify({'success': True, 'message': 'Analysis cycle triggered'})


@_api_app.route('/api/v1/export/test-push', methods=['POST'])
@_require_token
def post_export_test_push():
    """
    H1: Build, sign, and push a full snapshot to the configured viewer_url.

    This is a manual test trigger.  The scheduled push loop (H5) calls the
    same underlying logic on a timer; this endpoint lets operators verify that
    connectivity and credentials are correct without waiting for the scheduler.

    Returns::

        { "success": bool, "status_code": int | null, "error": str | null }
    """
    import anonymiser as _anonymiser
    import exporter as _exporter

    cfg = _cfg()

    # load sharing prefs to get viewer_url and bearer_token
    try:
        with db.connect(cfg) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    '''
                    SELECT sharing_enabled, viewer_url, bearer_token
                    FROM config.sharing_prefs
                    WHERE cluster_id = %s
                    LIMIT 1
                    ''',
                    [cfg.cluster_id],
                )
                row = cur.fetchone()
    except Exception as e:
        log.exception('POST /api/v1/export/test-push: failed to read sharing_prefs')
        return jsonify({'success': False, 'status_code': None, 'error': str(e)}), 500

    if not row:
        return jsonify({
            'success': False,
            'status_code': None,
            'error': 'No sharing_prefs row found; run migration 015.',
        }), 500

    sharing_enabled, viewer_url, bearer_token = row

    if not sharing_enabled:
        return jsonify({
            'success': False,
            'status_code': None,
            'error': 'Sharing is disabled in config.sharing_prefs.',
        }), 400

    if not viewer_url:
        return jsonify({
            'success': False,
            'status_code': None,
            'error': 'viewer_url is not configured in config.sharing_prefs.',
        }), 400

    if not bearer_token:
        return jsonify({
            'success': False,
            'status_code': None,
            'error': 'bearer_token is not configured in config.sharing_prefs.',
        }), 400

    try:
        snapshot = _exporter.build_snapshot(cfg)
        anon_cfg = _anonymiser.load_anon_config(cfg)
        if anon_cfg.enabled:
            snapshot = _anonymiser.anonymise_snapshot(snapshot, anon_cfg)
        signed   = _exporter.sign_snapshot(snapshot, bearer_token)
    except Exception as e:
        log.exception('POST /api/v1/export/test-push: snapshot build/sign failed')
        return jsonify({'success': False, 'status_code': None, 'error': str(e)}), 500

    # transmit to the viewer
    try:
        import urllib.request
        import urllib.error
        payload_bytes = json.dumps(signed, default=str).encode()
        push_req = urllib.request.Request(
            viewer_url.rstrip('/') + '/api/v1/sync',
            data=payload_bytes,
            headers={
                'Content-Type':  'application/json',
                'Authorization': f'Bearer {bearer_token}',
            },
            method='POST',
        )
        with urllib.request.urlopen(push_req, timeout=30) as resp:
            status_code = resp.getcode()
        log.info('export test-push succeeded', extra={'status_code': status_code, 'viewer_url': viewer_url})
        return jsonify({'success': True, 'status_code': status_code, 'error': None})
    except urllib.error.HTTPError as http_err:
        log.warning(
            'export test-push HTTP error',
            extra={'status_code': http_err.code, 'viewer_url': viewer_url},
        )
        return jsonify({'success': False, 'status_code': http_err.code, 'error': str(http_err)})
    except Exception as e:
        log.exception('POST /api/v1/export/test-push: transmission failed')
        return jsonify({'success': False, 'status_code': None, 'error': str(e)}), 500


# ---------------------------------------------------------------------------
# H4 — .hcb encrypted file export
# ---------------------------------------------------------------------------

@_api_app.route('/api/v1/export/hcb')
@_require_token
def get_export_hcb():
    """
    H4: Build and return an AES-256-GCM encrypted .hcb snapshot file.

    Designed for air-gapped / offline transfer — no viewer registration required.
    The caller supplies a passphrase via the ?passphrase= query parameter.  The
    same passphrase must be entered when importing the file into Foresight-Central.

    Anonymisation is applied according to config.sharing_prefs toggles if a prefs
    row exists; it is skipped silently if the table is not yet migrated.
    """
    import anonymiser as _anonymiser
    import exporter as _exporter
    import hcb as _hcb
    from flask import make_response

    passphrase = request.args.get('passphrase', '').strip()
    if not passphrase:
        return jsonify({
            'error': 'passphrase_required',
            'message': 'Supply a passphrase via ?passphrase=<value>. '
                       'You will need it to import the file into Foresight-Central.',
        }), 400

    cfg = _cfg()

    # anonymisation is best-effort — silently skip if sharing_prefs table absent
    anon_cfg = _anonymiser.load_anon_config(cfg)

    try:
        snapshot = _exporter.build_snapshot(cfg)
        if anon_cfg.enabled:
            snapshot = _anonymiser.anonymise_snapshot(snapshot, anon_cfg)
        # sign with the passphrase so the viewer can verify integrity on import
        signed = _exporter.sign_snapshot(snapshot, passphrase)
    except Exception as e:
        log.exception('GET /api/v1/export/hcb: snapshot build/sign failed')
        return jsonify({'error': 'snapshot_error', 'message': str(e)}), 500

    try:
        file_bytes = _hcb.encrypt(json.dumps(signed, default=str), passphrase)
    except Exception as e:
        log.exception('GET /api/v1/export/hcb: encryption failed')
        return jsonify({'error': 'encryption_error', 'message': str(e)}), 500

    timestamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    filename  = f'sentinel-export-{timestamp}.hcb'

    response = make_response(file_bytes)
    response.headers['Content-Type']        = 'application/octet-stream'
    response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


# ---------------------------------------------------------------------------
# startup
# ---------------------------------------------------------------------------

def start(cfg: cfg_module.Config, trigger_analysis_fn=None, cfgs: dict = None) -> None:
    """
    Start the REST API server in a daemon thread.

    cfg is the base (environment) config: port and token. cfgs is the live
    {cluster_id: Config} dict maintained by sentinel.py's supervisor; requests pick
    their cluster with ?cluster=<uuid>. trigger_analysis_fn(cluster_id) signals that
    cluster's analysis loop to run on its next iteration (POST /api/v1/trend/run).
    """
    _api_app.config['SENTINEL_CFG']     = cfg
    _api_app.config['SENTINEL_CFGS']    = cfgs if cfgs is not None else {}
    _api_app.config['API_TOKEN']        = cfg.api_token
    _api_app.config['TRIGGER_ANALYSIS'] = trigger_analysis_fn

    def _run() -> None:
        _api_app.run(
            host='0.0.0.0',
            port=cfg.api_port,
            debug=False,
            use_reloader=False,
        )

    t = threading.Thread(target=_run, name='sentinel-api', daemon=True)
    t.start()
    log.info('Sentinel REST API started', extra={'port': cfg.api_port})
