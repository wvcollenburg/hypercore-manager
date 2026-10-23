"""
config_db.py — Live-tunable configuration store
================================================
PostgreSQL-backed key/value store for runtime-tunable settings.
All three containers (Balancer, Collector, Dashboard) share the same rows
in config.tunables on the sc_postgres instance.

Tunables are **per cluster**. Row cluster_id …0001 is the "Defaults" template
(see cluster_registry.py): its values are seeded from env vars / hardcoded
defaults and copied to every newly registered cluster. Every function takes an
optional cluster_id; omitted means the template.

Canonical copy: shared/config_db.py — run tools/sync_shared.py after editing.

Connection: PG_HOST / PG_PORT / PG_DB / PG_USER / PG_PASSWORD env vars.

Graceful degradation: if PostgreSQL is unreachable, all reads fall back to
the env var value for that key (or the hardcoded default). Writes are
silently dropped with a warning log. The service continues operating normally.

On first run, values are seeded from environment variables (falling back to
hardcoded defaults). After that, env vars are ignored for keys the user has
changed — the database wins.
"""

import logging
import os
import threading
from typing import Any

import psycopg2

logger = logging.getLogger(__name__)

_lock = threading.Lock()

# the defaults template row; real clusters are created by cluster_registry.create()
TEMPLATE_CLUSTER_ID = '00000000-0000-0000-0000-000000000001'
DEFAULT_CLUSTER_ID = TEMPLATE_CLUSTER_ID   # backwards-compatible name

# registry: key → (env_var_name, hardcoded_default, type_hint)
# The defaults below are the shipped values; the env var names remain only as a fallback
# while PostgreSQL is unreachable. Tunables are not set in .env: the Defaults template is
# seeded from these values on first start and edited in the dashboard afterwards.
TUNABLES = {
    # ── balancer: reactive ──────────────────────────────────────────────────
    'DRY_RUN':                        ('SC_DRY_RUN',                        True,   'bool'),
    'VERBOSITY':                      ('SC_VERBOSITY',                       3,      'int'),
    'AVG_WINDOW_MINUTES':             ('SC_AVG_WINDOW_MINUTES',              5,      'int'),
    'SAMPLE_INTERVAL_SECONDS':        ('SC_SAMPLE_INTERVAL_SECONDS',         30,     'int'),
    'RAM_LIMIT_PERCENT':              ('SC_RAM_LIMIT_PERCENT',               85.0,   'float'),
    'RAM_UPPER_THRESHOLD_PERCENT':    ('SC_RAM_UPPER_THRESHOLD_PERCENT',     65.0,   'float'),
    'CPU_UPPER_THRESHOLD_PERCENT':    ('SC_CPU_UPPER_THRESHOLD_PERCENT',     50.0,   'float'),
    'MAX_VCPU_RATIO':                 ('SC_MAX_VCPU_RATIO',                  2.0,    'float'),
    'VM_MOVE_COOLDOWN_MINUTES':       ('SC_VM_MOVE_COOLDOWN_MINUTES',        30,     'int'),
    'MIGRATION_COOLDOWN_MINUTES':     ('SC_MIGRATION_COOLDOWN_MINUTES',      5,      'int'),
    'EXCLUDE_NODE_IPS':               ('SC_EXCLUDE_NODE_IPS',                '',     'list'),
    'CPU_TARGET_TOLERANCE_PERCENT':   ('SC_CPU_TARGET_TOLERANCE_PERCENT',    5.0,    'float'),
    # ── balancer: predictive ────────────────────────────────────────────────
    'PREDICTIVE_BALANCING_ENABLED':   ('SC_PREDICTIVE_BALANCING_ENABLED',    True,   'bool'),
    'PREDICTIVE_INTERVAL_SECONDS':    ('SC_PREDICTIVE_INTERVAL_SECONDS',     43200,  'int'),
    'PREDICTIVE_LEAD_TIME_HOURS':     ('SC_PREDICTIVE_LEAD_TIME_HOURS',      1,      'int'),
    'PREDICTIVE_THRESHOLD':           ('SC_PREDICTIVE_THRESHOLD',            80.0,   'float'),
    'PREDICTIVE_MIN_HISTORY_HOURS':   ('SC_PREDICTIVE_MIN_HISTORY_HOURS',    336,    'int'),
    'PREDICTIVE_LOOKBACK_DAYS':       ('SC_PREDICTIVE_LOOKBACK_DAYS',        90,     'int'),
    'PREDICTIVE_MAX_WORKERS':         ('SC_PREDICTIVE_MAX_WORKERS',          8,      'int'),
    'PREDICTIVE_RESERVATION_MINUTES': ('SC_PREDICTIVE_RESERVATION_MINUTES',  5,      'int'),
    # ── collector ───────────────────────────────────────────────────────────
    'POLL_INTERVAL':                  ('SC_POLL_INTERVAL',                   30,     'int'),
    'SLOW_POLL_INTERVAL':             ('SC_SLOW_POLL_INTERVAL',              300,    'int'),
    'INFLUX_WRITE_RETRIES':           ('INFLUX_WRITE_RETRIES',               3,      'int'),
    'INFLUX_RETRY_DELAY':             ('INFLUX_RETRY_DELAY',                 5,      'int'),
    # ── dashboard ───────────────────────────────────────────────────────────
    'STALE_SECONDS':                  ('SC_DASHBOARD_STALE_SECONDS',         120,    'int'),
}


# ---------------------------------------------------------------------------
# type helpers
# ---------------------------------------------------------------------------

def _cast(value: str, type_hint: str) -> Any:
    if type_hint == 'bool':
        return str(value).lower() in ('true', '1', 'yes', 'y')
    if type_hint == 'int':
        return int(float(value))  # float() first handles "30.0" edge case
    if type_hint == 'float':
        return float(value)
    if type_hint == 'list':
        return [ip.strip() for ip in str(value).split(',') if ip.strip()]
    return str(value)


def _serialize(value: Any, type_hint: str) -> str:
    if type_hint == 'list':
        return ','.join(str(v) for v in value) if isinstance(value, list) else str(value)
    if type_hint == 'bool':
        return 'true' if value else 'false'
    return str(value)


# ---------------------------------------------------------------------------
# internal helpers
# ---------------------------------------------------------------------------

def _connect() -> psycopg2.extensions.connection:
    return psycopg2.connect(
        host=os.getenv('PG_HOST', 'postgres'),
        port=int(os.getenv('PG_PORT', '5432')),
        dbname=os.getenv('PG_DB', 'hypercore_balancer'),
        user=os.getenv('PG_USER', 'sentinel'),
        password=os.getenv('PG_PASSWORD', ''),
        connect_timeout=5,
    )


def _env_fallback(key: str) -> Any:
    """Return the env var value (or hardcoded default) for key — no DB needed."""
    entry = TUNABLES.get(key)
    if entry is None:
        return None
    env_var, default, type_hint = entry
    raw = os.getenv(env_var)
    return _cast(raw, type_hint) if raw else default


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def seed_from_env() -> None:
    """
    Called once at service startup. Inserts every key the Defaults template lacks, from the
    env var when set, else the hardcoded default. It never updates a stored value: an earlier
    version re-applied the env var whenever the stored value equalled the hardcoded default,
    which could not tell "never touched" from "set back to the default in the dashboard", so a
    node exclusion cleared in the UI, or dry-run switched back on, came back from .env at the
    next restart. Clusters copy from the template on creation. Falls back silently if
    PostgreSQL is unreachable.
    """
    with _lock:
        try:
            conn = _connect()
            try:
                with conn.cursor() as cur:
                    for key, (env_var, default, type_hint) in TUNABLES.items():
                        raw_env = os.getenv(env_var) or None
                        value = _cast(raw_env, type_hint) if raw_env is not None else default
                        cur.execute(
                            '''INSERT INTO config.tunables (cluster_id, key, value, type)
                               VALUES (%s, %s, %s, %s)
                               ON CONFLICT (cluster_id, key) DO NOTHING''',
                            (DEFAULT_CLUSTER_ID, key, _serialize(value, type_hint), type_hint),
                        )
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            logger.warning('config_db: PostgreSQL unavailable during seed, using env vars: %s', exc)


def copy_template_to(cluster_id: str) -> None:
    """Give a cluster every tunable it lacks, copied from the Defaults template.
    Used when a new key ships in a release so existing clusters pick it up."""
    with _lock:
        try:
            conn = _connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        '''INSERT INTO config.tunables (cluster_id, key, value, type)
                           SELECT %s, key, value, type FROM config.tunables WHERE cluster_id = %s
                           ON CONFLICT (cluster_id, key) DO NOTHING''',
                        (cluster_id, TEMPLATE_CLUSTER_ID),
                    )
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            logger.warning('config_db: copy_template_to(%s) failed: %s', cluster_id, exc)


def get(key: str, cluster_id: str = TEMPLATE_CLUSTER_ID) -> Any:
    """Return a single tunable cast to its Python type."""
    with _lock:
        try:
            conn = _connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        'SELECT value, type FROM config.tunables WHERE cluster_id = %s AND key = %s',
                        (cluster_id, key),
                    )
                    row = cur.fetchone()
            finally:
                conn.close()
            if row is None:
                return _env_fallback(key)
            return _cast(row[0], row[1])
        except Exception as exc:
            logger.warning('config_db: PostgreSQL unavailable for get(%s), using env fallback: %s', key, exc)
            return _env_fallback(key)


def get_all(cluster_id: str = TEMPLATE_CLUSTER_ID) -> dict:
    """Return all tunables as {key: typed_value}."""
    with _lock:
        try:
            conn = _connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        'SELECT key, value, type FROM config.tunables WHERE cluster_id = %s',
                        (cluster_id,),
                    )
                    rows = cur.fetchall()
            finally:
                conn.close()
            return {r[0]: _cast(r[1], r[2]) for r in rows}
        except Exception as exc:
            logger.warning('config_db: PostgreSQL unavailable for get_all(), using env fallback: %s', exc)
            return {k: _env_fallback(k) for k in TUNABLES}


def save(key: str, value: Any, cluster_id: str = TEMPLATE_CLUSTER_ID) -> None:
    """Upsert a single tunable."""
    if key not in TUNABLES:
        raise ValueError(f'Unknown tunable: {key}')
    _, _, type_hint = TUNABLES[key]
    with _lock:
        try:
            conn = _connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        '''INSERT INTO config.tunables (cluster_id, key, value, type, updated_at)
                           VALUES (%s, %s, %s, %s, NOW())
                           ON CONFLICT (cluster_id, key) DO UPDATE
                               SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at''',
                        (cluster_id, key, _serialize(value, type_hint), type_hint),
                    )
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            logger.warning('config_db: PostgreSQL unavailable for save(%s), change dropped: %s', key, exc)


def save_many(updates: dict, cluster_id: str = TEMPLATE_CLUSTER_ID) -> None:
    """Upsert multiple tunables in one transaction. Unknown keys are silently skipped."""
    with _lock:
        try:
            conn = _connect()
            try:
                with conn.cursor() as cur:
                    for key, value in updates.items():
                        if key not in TUNABLES:
                            continue
                        _, _, type_hint = TUNABLES[key]
                        cur.execute(
                            '''INSERT INTO config.tunables (cluster_id, key, value, type, updated_at)
                               VALUES (%s, %s, %s, %s, NOW())
                               ON CONFLICT (cluster_id, key) DO UPDATE
                                   SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at''',
                            (cluster_id, key, _serialize(value, type_hint), type_hint),
                        )
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            logger.warning('config_db: PostgreSQL unavailable for save_many(), changes dropped: %s', exc)


def get_with_meta(cluster_id: str = TEMPLATE_CLUSTER_ID) -> list:
    """Return all tunables with type and timestamp — for the settings API."""
    with _lock:
        try:
            conn = _connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        '''SELECT key, value, type, updated_at
                             FROM config.tunables
                            WHERE cluster_id = %s
                            ORDER BY key''',
                        (cluster_id,),
                    )
                    rows = cur.fetchall()
            finally:
                conn.close()
            return [
                {'key': r[0], 'value': _cast(r[1], r[2]), 'type': r[2], 'updated_at': str(r[3])}
                for r in rows
            ]
        except Exception as exc:
            logger.warning('config_db: PostgreSQL unavailable for get_with_meta(), using env fallback: %s', exc)
            return [
                {'key': k, 'value': _env_fallback(k), 'type': TUNABLES[k][2], 'updated_at': None}
                for k in sorted(TUNABLES)
            ]
