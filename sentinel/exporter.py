"""
H1: Delta snapshot builder.

Serialises Sentinel's PostgreSQL conclusions into a signed JSON payload
suitable for pushing to SC//Foresight-Central.

A "delta" snapshot includes only rows created/updated after a given
``last_sync_at`` timestamp.  A full snapshot (``last_sync_at=None``) includes
everything currently active in the recommendations schema.

The payload is HMAC-SHA256 signed using the viewer bearer token so that the
receiving end can verify it was not tampered with in transit.
"""

import hashlib
import hmac
import json
import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional
from uuid import UUID

import db
import config as cfg_module

log = logging.getLogger(__name__)

_SENTINEL_VERSION = '1.4'


# ---------------------------------------------------------------------------
# serialisation helpers
# ---------------------------------------------------------------------------

def _serialise(obj):
    """
    JSON default serialiser for types that json.dumps cannot handle natively.

    Converts:
    - ``datetime`` / ``date`` → ISO 8601 string  (datetime checked first —
      it is a subclass of date, so order matters)
    - ``UUID`` → string
    - ``Decimal`` → float
    - Anything else → str (last resort)
    """
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, UUID):
        return str(obj)
    if isinstance(obj, Decimal):
        return float(obj)
    return str(obj)


def _row_to_dict(row, description) -> dict:
    """Convert a psycopg2 row to a JSON-serialisable dict."""
    cols = [d.name for d in description]
    result = {}
    for col, value in zip(cols, row):
        if isinstance(value, datetime):         # datetime before date — it is a subclass
            result[col] = value.isoformat()
        elif isinstance(value, date):
            result[col] = value.isoformat()
        elif isinstance(value, UUID):
            result[col] = str(value)
        elif isinstance(value, Decimal):
            result[col] = float(value)
        else:
            result[col] = value
    return result


def _rows_to_dicts(rows, description) -> list:
    """Convert a list of psycopg2 rows to a list of JSON-serialisable dicts."""
    return [_row_to_dict(row, description) for row in rows]


# ---------------------------------------------------------------------------
# snapshot builder
# ---------------------------------------------------------------------------

def build_snapshot(cfg: cfg_module.Config, last_sync_at: Optional[datetime] = None) -> dict:
    """
    Build a delta (or full) snapshot of all current Sentinel conclusions.

    If *last_sync_at* is provided only rows created after that timestamp are
    included (delta mode).  Pass ``None`` for a full snapshot.

    Returns a dict with the following top-level structure::

        {
            "schema_version": "1.0",
            "generated_at":   "<ISO8601>",
            "cluster_id":     "<cfg.cluster_id>",
            "is_delta":       bool,
            "since":          "<ISO8601 or null>",
            "metadata": {
                "cluster_name":      str,
                "sentinel_version":  "1.4",
                "node_count":        int,
                "vm_count":          int,
            },
            "forecasts":             [...],
            "rightsizing":           [...],
            "resize_recommendations":[...],
            "cluster_health":        [...],
            "exhaustion":            [...],
        }

    All sections return empty lists if the underlying table is empty or an
    error occurs — the snapshot is never partial.
    """
    is_delta = last_sync_at is not None
    since_str = last_sync_at.isoformat() if last_sync_at else None
    cluster_id = cfg.cluster_id

    forecasts            = []
    rightsizing          = []
    resize_recommendations = []
    cluster_health       = []
    exhaustion           = []
    node_count           = 0
    vm_count             = 0

    try:
        with db.connect(cfg) as conn:
            with conn.cursor() as cur:

                # ── forecast snapshots ────────────────────────────────────
                params: list = [cluster_id]
                delta_clause = ''
                if is_delta:
                    delta_clause = 'AND created_at > %s'
                    params.append(last_sync_at)

                # forecast_snapshots uses snapshot_at (not created_at) as its
                # insertion timestamp; we treat snapshot_at as the delta anchor
                fs_params: list = [cluster_id]
                fs_delta = ''
                if is_delta:
                    fs_delta = 'AND snapshot_at > %s'
                    fs_params.append(last_sync_at)

                cur.execute(
                    f'''
                    SELECT id, snapshot_at, resource_dimension, entity_label,
                           forecast_horizon_days, confidence_score, driving_metric,
                           exhaustion_date, severity, model_metadata,
                           current_usage, current_capacity, forecast_value,
                           node_uuid, cluster_id
                    FROM recommendations.forecast_snapshots
                    WHERE cluster_id = %s
                      AND expires_at > NOW()
                      {fs_delta}
                    ORDER BY snapshot_at DESC
                    ''',
                    fs_params,
                )
                forecasts = _rows_to_dicts(cur.fetchall(), cur.description)

                # ── right-sizing ──────────────────────────────────────────
                rs_params: list = [cluster_id]
                rs_delta = ''
                if is_delta:
                    rs_delta = 'AND detected_at > %s'
                    rs_params.append(last_sync_at)

                cur.execute(
                    f'''
                    SELECT id, detected_at, vm_uuid, vm_name, node_uuid,
                           sizing_direction, severity, composite_score,
                           current_vcpu,
                           cpu_avg_usage_pct, cpu_p95_usage_pct,
                           analysis_window_days, cluster_id
                    FROM recommendations.right_sizing
                    WHERE cluster_id = %s
                      AND expires_at > NOW()
                      {rs_delta}
                    ORDER BY detected_at DESC
                    ''',
                    rs_params,
                )
                rightsizing = _rows_to_dicts(cur.fetchall(), cur.description)

                # ── resize recommendations ────────────────────────────────
                rr_params: list = [cluster_id]
                rr_delta = ''
                if is_delta:
                    rr_delta = 'AND detected_at > %s'
                    rr_params.append(last_sync_at)

                cur.execute(
                    f'''
                    SELECT id, detected_at, vm_uuid, vm_name, node_uuid,
                           current_vcpu, recommended_vcpu,
                           suggested_cpu_change_pct, confidence_score,
                           capacity_reclaim_cpu_pct,
                           risk_level, severity, cluster_id
                    FROM recommendations.resize_recommendations
                    WHERE cluster_id = %s
                      AND expires_at > NOW()
                      {rr_delta}
                    ORDER BY detected_at DESC
                    ''',
                    rr_params,
                )
                resize_recommendations = _rows_to_dicts(cur.fetchall(), cur.description)

                # ── cluster health scores ─────────────────────────────────
                ch_params: list = [cluster_id]
                ch_delta = ''
                if is_delta:
                    ch_delta = 'AND calculated_at > %s'
                    ch_params.append(last_sync_at)

                cur.execute(
                    f'''
                    SELECT id, calculated_at,
                           health_score AS overall_score,
                           bottleneck_resource,
                           severity, cluster_id,
                           growth_rates, component_scores
                    FROM recommendations.cluster_health_scores
                    WHERE cluster_id = %s
                      AND expires_at > NOW()
                      {ch_delta}
                    ORDER BY calculated_at DESC
                    ''',
                    ch_params,
                )
                cluster_health = _rows_to_dicts(cur.fetchall(), cur.description)

                # extract per-dimension health scores and growth rates from their
                # JSONB columns into the flat fields the viewer expects
                # component_scores keys: cpu, ram, storage_capacity, network
                # growth_rates keys:     cpu, ram, storage_capacity, network
                for row in cluster_health:
                    cs = row.get('component_scores') or {}
                    gr = row.get('growth_rates') or {}
                    row['cpu_headroom_pct']     = cs.get('cpu')
                    row['ram_headroom_pct']     = cs.get('ram')
                    row['storage_headroom_pct'] = cs.get('storage_capacity')
                    row['network_headroom_pct'] = cs.get('network')
                    row['cpu_growth_rate']      = gr.get('cpu')
                    row['ram_growth_rate']      = gr.get('ram')
                    row['storage_growth_rate']  = gr.get('storage_capacity')

                # ── exhaustion (derived from forecast_snapshots) ──────────
                # most recent row per (resource_dimension, entity_label) where
                # exhaustion_days (days_until_exhaustion proxy) is positive
                ex_params: list = [cluster_id]
                ex_delta = ''
                if is_delta:
                    ex_delta = 'AND snapshot_at > %s'
                    ex_params.append(last_sync_at)

                cur.execute(
                    f'''
                    SELECT DISTINCT ON (resource_dimension, entity_label)
                           resource_dimension, entity_label,
                           exhaustion_date,
                           (exhaustion_date - CURRENT_DATE) AS exhaustion_days,
                           severity, confidence_score AS confidence,
                           cluster_id
                    FROM recommendations.forecast_snapshots
                    WHERE cluster_id = %s
                      AND expires_at > NOW()
                      AND exhaustion_date IS NOT NULL
                      AND exhaustion_date > CURRENT_DATE
                      {ex_delta}
                    ORDER BY resource_dimension, entity_label, snapshot_at DESC
                    ''',
                    ex_params,
                )
                exhaustion = _rows_to_dicts(cur.fetchall(), cur.description)

                # ── metadata counts ───────────────────────────────────────
                cur.execute(
                    '''
                    SELECT COUNT(DISTINCT node_uuid), COUNT(DISTINCT vm_uuid)
                    FROM recommendations.right_sizing
                    WHERE cluster_id = %s AND expires_at > NOW()
                    ''',
                    [cluster_id],
                )
                count_row = cur.fetchone()
                if count_row:
                    node_count = int(count_row[0] or 0)
                    vm_count   = int(count_row[1] or 0)

    except Exception:
        log.exception('build_snapshot: failed to query PostgreSQL')

    return {
        'schema_version': '1.0',
        'generated_at':   datetime.now(timezone.utc).isoformat(),
        'cluster_id':     cluster_id,
        'is_delta':       is_delta,
        'since':          since_str,
        'metadata': {
            'cluster_name':     cfg.cluster_id,   # cluster name not in Config; fall back to ID
            'sentinel_version': _SENTINEL_VERSION,
            'node_count':       node_count,
            'vm_count':         vm_count,
        },
        'forecasts':              forecasts,
        'rightsizing':            rightsizing,
        'resize_recommendations': resize_recommendations,
        'cluster_health':         cluster_health,
        'exhaustion':             exhaustion,
    }


# ---------------------------------------------------------------------------
# signing and verification
# ---------------------------------------------------------------------------

def sign_snapshot(snapshot: dict, bearer_token: str) -> dict:
    """
    Add an HMAC-SHA256 signature to the snapshot dict.

    The canonical form for signing is ``json.dumps`` with sorted keys and no
    extra whitespace applied to the snapshot body *without* the ``"signature"``
    key.  The resulting signature is stored as::

        "signature": "sha256=<lowercase hex digest>"

    Returns a **new** dict (the original is not mutated).
    """
    body = {k: v for k, v in snapshot.items() if k != 'signature'}
    canonical = json.dumps(body, sort_keys=True, separators=(',', ':'), default=_serialise)
    digest = hmac.new(
        bearer_token.encode(),
        canonical.encode(),
        hashlib.sha256,
    ).hexdigest()
    return {**body, 'signature': f'sha256={digest}'}


def verify_snapshot(signed_snapshot: dict, bearer_token: str) -> bool:
    """
    Verify the HMAC-SHA256 signature on a snapshot.

    Removes the ``"signature"`` key, recomputes the digest over the remaining
    body, and performs a constant-time comparison.  Returns ``True`` if valid,
    ``False`` otherwise.
    """
    expected_sig = signed_snapshot.get('signature', '')
    if not expected_sig.startswith('sha256='):
        return False

    body = {k: v for k, v in signed_snapshot.items() if k != 'signature'}
    canonical = json.dumps(body, sort_keys=True, separators=(',', ':'), default=_serialise)
    expected_digest = expected_sig[7:]   # strip "sha256=" prefix
    actual_digest = hmac.new(
        bearer_token.encode(),
        canonical.encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected_digest, actual_digest)
