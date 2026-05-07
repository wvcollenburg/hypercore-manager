"""
tunables.py — Live-tunable configuration store for Sentinel.

Mirrors the interface of dashboard/config_db.py but uses SENTINEL_PG_*
connection variables and manages Sentinel-specific keys only.

Live-reload keys are re-read from PostgreSQL at the top of each analysis
cycle — no container restart required to pick up changes made via the UI.

Seeds from env vars on first start; database wins after that (same pattern
as config_db.py).
"""

import logging
import os
import threading
from collections import namedtuple
from dataclasses import replace as _dc_replace
from typing import Any

import psycopg2

logger = logging.getLogger(__name__)

_lock = threading.Lock()

DEFAULT_CLUSTER_ID = os.getenv('SC_CLUSTER_ID', '00000000-0000-0000-0000-000000000001')

TunableDef = namedtuple('TunableDef', [
    'env_var', 'default', 'type_hint', 'live_reload',
    'cfg_field', 'label', 'unit', 'description',
])

# Registry: DB key → TunableDef
# All keys use live_reload=True — all are re-read before each analysis cycle.
TUNABLES: dict = {
    # ── Right-sizing thresholds ─────────────────────────────────────────────
    'SENTINEL_CPU_OVER_PROVISIONED_THRESHOLD': TunableDef(
        env_var='SENTINEL_CPU_OVER_PROVISIONED_THRESHOLD',
        default=20.0, type_hint='float', live_reload=True,
        cfg_field='cpu_over_provisioned_threshold',
        label='Over-Prov CPU Threshold', unit='%',
        description='VMs using less than this % of allocated CPU are flagged as over-provisioned (C1).',
    ),
    'SENTINEL_CPU_UNDER_PROVISIONED_AVG_THRESHOLD': TunableDef(
        env_var='SENTINEL_CPU_UNDER_PROVISIONED_AVG_THRESHOLD',
        default=80.0, type_hint='float', live_reload=True,
        cfg_field='cpu_under_provisioned_avg_threshold',
        label='Under-Prov CPU Avg', unit='%',
        description='VMs with average CPU above this % are flagged as under-provisioned (C2 Signal 1).',
    ),
    'SENTINEL_CPU_UNDER_PROVISIONED_P95_THRESHOLD': TunableDef(
        env_var='SENTINEL_CPU_UNDER_PROVISIONED_P95_THRESHOLD',
        default=90.0, type_hint='float', live_reload=True,
        cfg_field='cpu_under_provisioned_p95_threshold',
        label='Under-Prov CPU P95', unit='%',
        description='VMs with P95 CPU above this % are flagged as under-provisioned via burst pattern (C2 Signal 1).',
    ),
    'SENTINEL_ANALYSIS_WINDOW_DAYS': TunableDef(
        env_var='SENTINEL_ANALYSIS_WINDOW_DAYS',
        default=14, type_hint='int', live_reload=True,
        cfg_field='analysis_window_days',
        label='Analysis Window', unit='days',
        description='Days of history used for right-sizing and spike detection. Minimum 7.',
    ),
    'SENTINEL_RECURRING_SPIKE_MIN_OCCURRENCES': TunableDef(
        env_var='SENTINEL_RECURRING_SPIKE_MIN_OCCURRENCES',
        default=3, type_hint='int', live_reload=True,
        cfg_field='recurring_spike_min_occurrences',
        label='Min Spike Occurrences', unit='',
        description='Minimum recurring CPU spikes at the same time slot before flagging a pattern (C2 Signal 3).',
    ),
    # ── Forecast & analysis ─────────────────────────────────────────────────
    'SENTINEL_ANOMALY_FILTER_ENABLED': TunableDef(
        env_var='SENTINEL_ANOMALY_FILTER_ENABLED',
        default=True, type_hint='bool', live_reload=True,
        cfg_field='anomaly_filter_enabled',
        label='Anomaly Filter', unit='',
        description='Apply IQR-fence outlier removal to time series before fitting the forecast model (B9).',
    ),
    'SENTINEL_FORECAST_30D_ENABLED': TunableDef(
        env_var='SENTINEL_FORECAST_30D_ENABLED',
        default=True, type_hint='bool', live_reload=True,
        cfg_field='forecast_30d_enabled',
        label='30d Forecast', unit='',
        description='Enable 30-day short-term capacity forecast (B1).',
    ),
    'SENTINEL_FORECAST_90D_ENABLED': TunableDef(
        env_var='SENTINEL_FORECAST_90D_ENABLED',
        default=True, type_hint='bool', live_reload=True,
        cfg_field='forecast_90d_enabled',
        label='90d Forecast', unit='',
        description='Enable 90-day quarterly capacity forecast (B2).',
    ),
    'SENTINEL_FORECAST_180D_ENABLED': TunableDef(
        env_var='SENTINEL_FORECAST_180D_ENABLED',
        default=True, type_hint='bool', live_reload=True,
        cfg_field='forecast_180d_enabled',
        label='180d Forecast', unit='',
        description='Enable 6-month strategic capacity forecast (B3).',
    ),
    'SENTINEL_FORECAST_365D_ENABLED': TunableDef(
        env_var='SENTINEL_FORECAST_365D_ENABLED',
        default=True, type_hint='bool', live_reload=True,
        cfg_field='forecast_365d_enabled',
        label='365d Forecast', unit='',
        description='Enable 12-month strategic capacity forecast (B4).',
    ),
    'SENTINEL_FORECAST_MIN_R2_FOR_EXHAUSTION': TunableDef(
        env_var='SENTINEL_FORECAST_MIN_R2_FOR_EXHAUSTION',
        default=0.5, type_hint='float', live_reload=True,
        cfg_field='forecast_min_r2_for_exhaustion',
        label='Min r² for Exhaustion', unit='',
        description='Minimum r² required before a positive slope produces an exhaustion date. Suppresses spurious alerts on near-flat series; raise toward 1.0 to require a stronger trend, lower toward 0.0 to disable the gate.',
    ),
    # ── Maintenance ─────────────────────────────────────────────────────────
    'SENTINEL_RETENTION_MONTHS': TunableDef(
        env_var='SENTINEL_RETENTION_MONTHS',
        default=12, type_hint='int', live_reload=True,
        cfg_field='retention_months',
        label='Retention', unit='months',
        description='How long to keep forecast snapshots and right-sizing records before automated cleanup.',
    ),
    'SENTINEL_OUTCOME_TRACKER_ENABLED': TunableDef(
        env_var='SENTINEL_OUTCOME_TRACKER_ENABLED',
        default=True, type_hint='bool', live_reload=True,
        cfg_field='outcome_tracker_enabled',
        label='Outcome Tracker', unit='',
        description='Enable detection of VM spec changes after a right-sizing recommendation is issued (C7).',
    ),
    'SENTINEL_OUTCOME_CHECK_DELAY_HOURS': TunableDef(
        env_var='SENTINEL_OUTCOME_CHECK_DELAY_HOURS',
        default=48, type_hint='int', live_reload=True,
        cfg_field='outcome_check_delay_hours',
        label='Outcome Check Delay', unit='h',
        description='Hours after a recommendation before checking if the VM was resized. Default 48h.',
    ),
    'SENTINEL_WEBHOOK_ENABLED': TunableDef(
        env_var='SENTINEL_WEBHOOK_ENABLED',
        default=True, type_hint='bool', live_reload=True,
        cfg_field='webhook_enabled',
        label='Webhook Enabled', unit='',
        description='Enable outbound webhook alerts for CRITICAL/EMERGENCY events (F5). Requires SENTINEL_WEBHOOK_URL.',
    ),
    # ── VM staleness pruning ─────────────────────────────────────────────────
    'SENTINEL_VM_PRUNE_INTERVAL_SECONDS': TunableDef(
        env_var='SENTINEL_VM_PRUNE_INTERVAL_SECONDS',
        default=900, type_hint='int', live_reload=True,
        cfg_field='vm_prune_interval_seconds',
        label='VM Prune Interval', unit='s',
        description='How often Sentinel removes forecast and right-sizing records for VMs no longer active in InfluxDB. Default 900s (15 min).',
    ),
}


# ---------------------------------------------------------------------------
# Type helpers
# ---------------------------------------------------------------------------

def _cast(value: Any, type_hint: str) -> Any:
    if type_hint == 'bool':
        return str(value).lower() in ('true', '1', 'yes', 'y')
    if type_hint == 'int':
        return int(float(value))
    if type_hint == 'float':
        return float(value)
    return str(value)


def _serialize(value: Any, type_hint: str) -> str:
    if type_hint == 'bool':
        return 'true' if value else 'false'
    return str(value)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _connect():
    return psycopg2.connect(
        host=os.getenv('SENTINEL_PG_HOST', 'postgres'),
        port=int(os.getenv('SENTINEL_PG_PORT', '5432')),
        dbname=os.getenv('SENTINEL_PG_DB', 'hypercore_balancer'),
        user=os.getenv('SENTINEL_PG_USER', 'sentinel'),
        password=os.getenv('SENTINEL_PG_PASSWORD', ''),
        connect_timeout=5,
    )


def _env_fallback(key: str) -> Any:
    defn = TUNABLES.get(key)
    if defn is None:
        return None
    raw = os.getenv(defn.env_var)
    return _cast(raw, defn.type_hint) if raw is not None else defn.default


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def seed_from_env() -> None:
    """
    Called once at startup. Inserts missing keys from env vars (or hardcoded
    defaults). Updates rows that are still at the hardcoded default if an env
    var is now present. Falls back silently if PostgreSQL is unreachable.
    """
    with _lock:
        try:
            conn = _connect()
            try:
                with conn.cursor() as cur:
                    for key, defn in TUNABLES.items():
                        raw_env = os.getenv(defn.env_var) or None
                        cur.execute(
                            'SELECT value FROM config.tunables WHERE cluster_id = %s AND key = %s',
                            (DEFAULT_CLUSTER_ID, key),
                        )
                        row = cur.fetchone()
                        if row is None:
                            value = _cast(raw_env, defn.type_hint) if raw_env is not None else defn.default
                            cur.execute(
                                '''INSERT INTO config.tunables (cluster_id, key, value, type)
                                   VALUES (%s, %s, %s, %s)''',
                                (DEFAULT_CLUSTER_ID, key,
                                 _serialize(value, defn.type_hint), defn.type_hint),
                            )
                        elif raw_env is not None and row[0] == _serialize(defn.default, defn.type_hint):
                            # Still at hardcoded default and env var is available — apply it
                            cur.execute(
                                '''UPDATE config.tunables
                                      SET value = %s, updated_at = NOW()
                                    WHERE cluster_id = %s AND key = %s''',
                                (_serialize(_cast(raw_env, defn.type_hint), defn.type_hint),
                                 DEFAULT_CLUSTER_ID, key),
                            )
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            logger.warning('tunables: PostgreSQL unavailable during seed, using env vars: %s', exc)


def get_all() -> dict:
    """Return all live-reload tunables as {key: typed_value}."""
    with _lock:
        try:
            conn = _connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        '''SELECT key, value, type FROM config.tunables
                           WHERE cluster_id = %s AND key = ANY(%s)''',
                        (DEFAULT_CLUSTER_ID, list(TUNABLES.keys())),
                    )
                    rows = cur.fetchall()
            finally:
                conn.close()
            return {r[0]: _cast(r[1], r[2]) for r in rows}
        except Exception as exc:
            logger.warning('tunables: PostgreSQL unavailable for get_all(), using env fallback: %s', exc)
            return {k: _env_fallback(k) for k in TUNABLES}


def save_many(updates: dict) -> None:
    """Upsert multiple tunables in one transaction. Unknown keys are skipped."""
    with _lock:
        try:
            conn = _connect()
            try:
                with conn.cursor() as cur:
                    for key, value in updates.items():
                        if key not in TUNABLES:
                            continue
                        defn = TUNABLES[key]
                        cur.execute(
                            '''INSERT INTO config.tunables (cluster_id, key, value, type, updated_at)
                               VALUES (%s, %s, %s, %s, NOW())
                               ON CONFLICT (cluster_id, key) DO UPDATE
                                   SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at''',
                            (DEFAULT_CLUSTER_ID, key,
                             _serialize(value, defn.type_hint), defn.type_hint),
                        )
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            logger.warning('tunables: PostgreSQL unavailable for save_many(), changes dropped: %s', exc)


def get_with_meta() -> list:
    """Return all tunables with full metadata — used by the tunables API."""
    with _lock:
        try:
            conn = _connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        '''SELECT key, value, type, updated_at FROM config.tunables
                           WHERE cluster_id = %s AND key = ANY(%s)
                           ORDER BY key''',
                        (DEFAULT_CLUSTER_ID, list(TUNABLES.keys())),
                    )
                    db_rows = {r[0]: (r[1], r[2], r[3]) for r in cur.fetchall()}
            finally:
                conn.close()
        except Exception as exc:
            logger.warning('tunables: PostgreSQL unavailable for get_with_meta(), using env fallback: %s', exc)
            db_rows = {}

    result = []
    for key, defn in sorted(TUNABLES.items()):
        if key in db_rows:
            raw_val, type_hint, updated_at = db_rows[key]
            value = _cast(raw_val, type_hint)
            updated_at_str = updated_at.isoformat() if updated_at else None
        else:
            value = _env_fallback(key)
            updated_at_str = None
        result.append({
            'key':         key,
            'value':       value,
            'default':     defn.default,
            'type':        defn.type_hint,
            'live_reload': defn.live_reload,
            'label':       defn.label,
            'unit':        defn.unit,
            'description': defn.description,
            'updated_at':  updated_at_str,
        })
    return result


def apply_live(base_cfg) -> Any:
    """
    Return a new Config with live-reloadable values from PostgreSQL.
    Called at the top of each analysis cycle — no restart needed.
    Falls back to the existing cfg values if the DB is unreachable.
    """
    try:
        live = get_all()
        overrides = {
            defn.cfg_field: live[key]
            for key, defn in TUNABLES.items()
            if key in live
        }
        return _dc_replace(base_cfg, **overrides)
    except Exception as exc:
        logger.warning('tunables: apply_live failed, keeping existing config: %s', exc)
        return base_cfg
