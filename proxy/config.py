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
    # a directory the VM watches: an image tarball HyperCore Manager pushes lands here and the host
    # loads it and restarts the container on it (empty = no update hook, e.g. the stack's own proxy)
    update_dir:  str = field(default_factory=lambda: _str('PROXY_UPDATE_DIR', ''))
    # how long to wait for a hot-added device to show up
    device_wait_seconds: int = field(default_factory=lambda: _int('PROXY_DEVICE_WAIT_SECONDS', 120))
    nbd_slots:   int = field(default_factory=lambda: _int('PROXY_NBD_SLOTS', 8))
    writers:     int = field(default_factory=lambda: _int('PROXY_WRITERS', 4))
    concurrency: int = field(default_factory=lambda: _int('PROXY_JOB_CONCURRENCY', 2))
    # 'nbd' writes qcow2 through qemu-nbd (production); 'file' writes raw sparse files (tests)
    backend:     str = field(default_factory=lambda: _str('PROXY_BACKEND', 'nbd'))
    # qcow2 lazy refcounts (no flush per allocated cluster) and qemu-nbd's cache mode for backup
    # writes; the 'bench' job measures the alternatives on a target (see images.NbdBackend).
    # Lab 241 → CIFS share, source read from Scribe, 2026-10-22: writeback 37 MiB/s, unsafe 55, plain file 45
    qcow2_lazy_refcounts: bool = field(default_factory=lambda: _str('PROXY_QCOW2_LAZY_REFCOUNTS', 'true').lower() in ('on', 'true', '1', 'yes'))
    nbd_cache:   str = field(default_factory=lambda: _str('PROXY_NBD_CACHE', 'unsafe'))
    # off (compose network), auto (self-signed, pinned by the service) or cert (own certificate)
    tls:         str = field(default_factory=lambda: _str('PROXY_TLS', 'auto'))
    tls_cert:    str = field(default_factory=lambda: _str('PROXY_TLS_CERT', ''))
    tls_key:     str = field(default_factory=lambda: _str('PROXY_TLS_KEY', ''))


def load() -> Config:
    return Config()
