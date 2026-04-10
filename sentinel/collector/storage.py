"""
collector/storage.py — Storage capacity and latency collection (A3, A4).

Storage capacity:  drive_metrics.used_bytes  — per-node and cluster-wide  (A3)
Storage latency:   vsd_metrics.read/write_latency_us — per-VSD             (A4)

Per-VSD granularity is preserved throughout (a single busy VSD must not
mask latency problems on others — see SENTINEL.md C2 spec).
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

import config as cfg_module
from influx_client import InfluxReader

log = logging.getLogger(__name__)


@dataclass
class DriveTimeSeries:
    """Capacity data for one physical drive."""
    node_uuid:  str
    drive_uuid: str
    drive_slot: str | None

    times:  list[datetime] = field(default_factory=list)
    values: list[float]    = field(default_factory=list)   # used_bytes

    # Derived
    mean_used_bytes: float | None = None
    max_used_bytes:  float | None = None
    latest_bytes:    float | None = None   # most recent sample


@dataclass
class VSDTimeSeries:
    """Latency and throughput data for one virtual storage device."""
    vm_uuid:  str
    vm_name:  str
    vsd_uuid: str
    vsd_name: str

    times:              list[datetime] = field(default_factory=list)
    read_latency_us:    list[float]    = field(default_factory=list)
    write_latency_us:   list[float]    = field(default_factory=list)
    read_kbps:          list[float]    = field(default_factory=list)
    write_kbps:         list[float]    = field(default_factory=list)

    # Derived latency stats
    read_lat_mean:  float | None = None
    read_lat_p95:   float | None = None
    write_lat_mean: float | None = None
    write_lat_p95:  float | None = None

    # Derived throughput stats
    read_kbps_mean:  float | None = None
    write_kbps_mean: float | None = None


@dataclass
class StorageSnapshot:
    """All storage data for a single collection cycle."""
    drives: dict[str, DriveTimeSeries]   = field(default_factory=dict)  # keyed by drive_uuid
    vsds:   dict[str, VSDTimeSeries]     = field(default_factory=dict)  # keyed by vsd_uuid

    # Cluster-wide capacity summary (populated by compute_cluster_totals)
    cluster_total_used_bytes:   float | None = None
    cluster_drive_count:        int          = 0

    # Per-node capacity rollup: {node_uuid: total_used_bytes}
    node_used_bytes: dict[str, float] = field(default_factory=dict)


def _compute_drive_stats(d: DriveTimeSeries) -> None:
    if d.values:
        arr = np.array(d.values, dtype=float)
        d.mean_used_bytes = float(np.mean(arr))
        d.max_used_bytes  = float(np.max(arr))
        d.latest_bytes    = float(arr[-1])   # most recent (list is time-ordered)


def _compute_vsd_stats(v: VSDTimeSeries) -> None:
    if v.read_latency_us:
        arr = np.array(v.read_latency_us, dtype=float)
        v.read_lat_mean = float(np.mean(arr))
        v.read_lat_p95  = float(np.percentile(arr, 95))

    if v.write_latency_us:
        arr = np.array(v.write_latency_us, dtype=float)
        v.write_lat_mean = float(np.mean(arr))
        v.write_lat_p95  = float(np.percentile(arr, 95))

    if v.read_kbps:
        v.read_kbps_mean  = float(np.mean(v.read_kbps))
    if v.write_kbps:
        v.write_kbps_mean = float(np.mean(v.write_kbps))


def _compute_cluster_totals(snap: StorageSnapshot) -> None:
    node_totals: dict[str, float] = {}
    for drive in snap.drives.values():
        nid = drive.node_uuid
        latest = drive.latest_bytes or 0.0
        node_totals[nid] = node_totals.get(nid, 0.0) + latest

    snap.node_used_bytes         = node_totals
    snap.cluster_total_used_bytes = sum(node_totals.values())
    snap.cluster_drive_count      = len(snap.drives)


def collect_storage(reader: InfluxReader, cfg: cfg_module.Config) -> StorageSnapshot:
    """
    Query InfluxDB and return a StorageSnapshot with per-drive and per-VSD data.
    """
    lookback = cfg.analysis_window_days
    snap = StorageSnapshot()

    # ── Drive capacity ────────────────────────────────────────────────────────
    try:
        drive_rows = reader.query_drive_capacity(lookback)
        for row in drive_rows:
            uid = row.get('drive_uuid') or 'unknown'
            if uid not in snap.drives:
                snap.drives[uid] = DriveTimeSeries(
                    node_uuid=row.get('node_uuid') or '',
                    drive_uuid=uid,
                    drive_slot=row.get('drive_slot'),
                )
            snap.drives[uid].times.append(row['time'])
            snap.drives[uid].values.append(float(row['value']))

        for d in snap.drives.values():
            _compute_drive_stats(d)

        _compute_cluster_totals(snap)
        log.debug('Drive rows fetched', extra={'drives': len(snap.drives)})
    except Exception:
        log.exception('Failed to fetch drive capacity metrics')

    # ── VSD latency ───────────────────────────────────────────────────────────
    try:
        lat_rows = reader.query_vsd_latency(lookback)
        for row in lat_rows:
            uid = row.get('vsd_uuid') or 'unknown'
            if uid not in snap.vsds:
                snap.vsds[uid] = VSDTimeSeries(
                    vm_uuid=row.get('vm_uuid') or '',
                    vm_name=row.get('vm_name') or '',
                    vsd_uuid=uid,
                    vsd_name=row.get('vsd_name') or '',
                )
            snap.vsds[uid].times.append(row['time'])
            snap.vsds[uid].read_latency_us.append(float(row.get('read_latency_us') or 0.0))
            snap.vsds[uid].write_latency_us.append(float(row.get('write_latency_us') or 0.0))
        log.debug('VSD latency rows fetched', extra={'vsds': len(snap.vsds)})
    except Exception:
        log.exception('Failed to fetch VSD latency metrics')

    # ── VSD throughput ────────────────────────────────────────────────────────
    try:
        tp_rows = reader.query_vsd_throughput(lookback)
        for row in tp_rows:
            uid = row.get('vsd_uuid') or 'unknown'
            if uid not in snap.vsds:
                snap.vsds[uid] = VSDTimeSeries(
                    vm_uuid=row.get('vm_uuid') or '',
                    vm_name=row.get('vm_name') or '',
                    vsd_uuid=uid,
                    vsd_name=row.get('vsd_name') or '',
                )
            snap.vsds[uid].read_kbps.append(float(row.get('read_kbps') or 0.0))
            snap.vsds[uid].write_kbps.append(float(row.get('write_kbps') or 0.0))
        log.debug('VSD throughput rows fetched', extra={'vsds': len(snap.vsds)})
    except Exception:
        log.exception('Failed to fetch VSD throughput metrics')

    for v in snap.vsds.values():
        _compute_vsd_stats(v)

    log.info(
        'Storage collection complete',
        extra={'drives': len(snap.drives), 'vsds': len(snap.vsds)},
    )
    return snap
