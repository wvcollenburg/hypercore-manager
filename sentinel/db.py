"""
db.py — PostgreSQL connection helper for Sentinel.

Sentinel uses SENTINEL_PG_* env vars (distinct from the PG_* vars used by
the Balancer/Collector/Dashboard config store, though they point to the
same postgres instance).

All public functions accept an explicit Config so there are no hidden
module-level singletons — easier to test and reason about.
"""

import logging
from contextlib import contextmanager
from typing import Generator

import psycopg2

import config as cfg_module

log = logging.getLogger(__name__)


def _dsn(cfg: cfg_module.Config) -> str:
    return (
        f"host={cfg.pg_host} "
        f"port={cfg.pg_port} "
        f"dbname={cfg.pg_db} "
        f"user={cfg.pg_user} "
        f"password={cfg.pg_password}"
    )


@contextmanager
def connect(cfg: cfg_module.Config) -> Generator[psycopg2.extensions.connection, None, None]:
    """
    Context manager that yields an open connection and commits on clean exit,
    rolls back on exception, and always closes.

    Usage:
        with db.connect(cfg) as conn:
            with conn.cursor() as cur:
                cur.execute(...)
    """
    conn = psycopg2.connect(_dsn(cfg), connect_timeout=10)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def check_connectivity(cfg: cfg_module.Config) -> bool:
    """Return True if PostgreSQL is reachable, False otherwise."""
    try:
        with connect(cfg) as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT 1')
        return True
    except Exception as exc:
        log.warning('PostgreSQL connectivity check failed', extra={'error': str(exc)})
        return False
