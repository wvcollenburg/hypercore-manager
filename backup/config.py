"""
config.py — environment-driven settings for the Backup service.

Clusters and their credentials come from the registry (cluster_registry.py), not
from here. Only ports, the API token, PostgreSQL and job tuning live in env vars.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _get(k: str):
    """BACKUP_* wins; the EXPORTS_* name from the module's first release still works."""
    v = os.getenv(k)
    if v is None and k.startswith('BACKUP_'):
        v = os.getenv('BACKUP_' + k[len('BACKUP_'):])
    return v


def _str(k: str, d: str) -> str:
    return _get(k) or d


def _int(k: str, d: int) -> int:
    try:
        return int(_get(k) or d)
    except ValueError:
        return d


@dataclass(frozen=True)
class Config:
    pg_host:     str = field(default_factory=lambda: _str('PG_HOST', os.getenv('SENTINEL_PG_HOST', 'postgres')))
    pg_port:     int = field(default_factory=lambda: _int('PG_PORT', 5432))
    pg_db:       str = field(default_factory=lambda: _str('PG_DB', 'hypercore_balancer'))
    pg_user:     str = field(default_factory=lambda: _str('PG_USER', 'sentinel'))
    pg_password: str = field(default_factory=lambda: _str('PG_PASSWORD', os.getenv('SENTINEL_PG_PASSWORD', '')))

    api_port:    int = field(default_factory=lambda: _int('BACKUP_API_PORT', 8082))
    api_token:   str = field(default_factory=lambda: _str('BACKUP_API_TOKEN', ''))

    # how long a HyperCore export/import task may run before the job gives up
    task_timeout_hours:    int = field(default_factory=lambda: _int('BACKUP_TASK_TIMEOUT_HOURS', 8))
    # TaskTag polling interval
    poll_interval_seconds: int = field(default_factory=lambda: _int('BACKUP_POLL_INTERVAL_SECONDS', 15))
    # defaults offered by the dashboard for new schedules
    default_retention:     int = field(default_factory=lambda: _int('BACKUP_DEFAULT_RETENTION', 7))
    default_run_time:      str = field(default_factory=lambda: _str('BACKUP_DEFAULT_RUN_TIME', '03:00'))
    # how often the scheduler re-reads schedules and the registry
    sync_interval_seconds: int = field(default_factory=lambda: _int('BACKUP_SYNC_INTERVAL_SECONDS', 60))


def load() -> Config:
    return Config()
