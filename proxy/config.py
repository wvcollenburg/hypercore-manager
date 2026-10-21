"""
config.py — environment-driven settings for the backup proxy.

The proxy holds no cluster credentials and no database connection. Everything it needs for a
job arrives inside the job request from the backup service; the environment only sets the port,
the API token, where job state lives and a few tuning knobs.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

MiB = 1 << 20


def _str(k: str, d: str) -> str:
    return os.getenv(k, d) or d


def _int(k: str, d: int) -> int:
    try:
        return int(os.getenv(k, str(d)))
    except ValueError:
        return d


@dataclass(frozen=True)
class Config:
    api_port:    int = field(default_factory=lambda: _int('PROXY_API_PORT', 8083))
    api_token:   str = field(default_factory=lambda: _str('PROXY_API_TOKEN', ''))
    # job state (json per job) and mount points live here; survives container restarts via a volume
    data_dir:    str = field(default_factory=lambda: _str('PROXY_DATA_DIR', '/var/lib/hcm-proxy'))
    # /dev of the host (hot-added disks appear here) and the host's network interfaces (identity)
    dev_dir:     str = field(default_factory=lambda: _str('PROXY_DEV_DIR', '/dev'))
    host_net:    str = field(default_factory=lambda: _str('PROXY_HOST_NET', '/host/sys/class/net'))
    # how long to wait for a hot-added device to show up
    device_wait_seconds: int = field(default_factory=lambda: _int('PROXY_DEVICE_WAIT_SECONDS', 120))
    nbd_slots:   int = field(default_factory=lambda: _int('PROXY_NBD_SLOTS', 8))
    writers:     int = field(default_factory=lambda: _int('PROXY_WRITERS', 4))
    concurrency: int = field(default_factory=lambda: _int('PROXY_JOB_CONCURRENCY', 2))
    # 'nbd' writes qcow2 through qemu-nbd (production); 'file' writes raw sparse files (tests)
    backend:     str = field(default_factory=lambda: _str('PROXY_BACKEND', 'nbd'))


def load() -> Config:
    return Config()
