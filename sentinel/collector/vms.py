"""
collector/vms.py — Per-VM metric collection (A5, A6).

CPU:      vm_metrics.cpu_usage                   (A6 CPU footprint)
Network:  vm_metrics.rx_bit_rate / tx_bit_rate   (A5)

Node-level network throughput = sum of all VM rx/tx on that node.
There is no native node-level network endpoint in the HyperCore REST API;
this rollup is the correct approach per platform constraints.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

import config as cfg_module
from influx_client import InfluxReader

log = logging.getLogger(__name__)


@dataclass
class VMTimeSeries:
    """All time-series data for a single VM over the collection window."""
    vm_uuid:   str
    vm_name:   str
    node_uuid: str

    # CPU usage % samples
    cpu_times:  list[datetime] = field(default_factory=list)
    cpu_values: list[float]    = field(default_factory=list)

    # Network bit rate samples (bits per second)
    net_times:    list[datetime] = field(default_factory=list)
    rx_values:    list[float]    = field(default_factory=list)
    tx_values:    list[float]    = field(default_factory=list)

    # vCPU allocation (latest value from vm_inventory; None if not yet fetched)
    num_vcpu:  int | None   = None

    # ── Derived statistics ─────────────────────────────────────────────────

    cpu_mean:  float | None = None
    cpu_p95:   float | None = None
    cpu_max:   float | None = None

    rx_mean:   float | None = None   # bps
    tx_mean:   float | None = None   # bps
    rx_max:    float | None = None
    tx_max:    float | None = None


def _compute_stats(vm: VMTimeSeries) -> None:
    if vm.cpu_values:
        arr = np.array(vm.cpu_values, dtype=float)
        vm.cpu_mean = float(np.mean(arr))
        vm.cpu_p95  = float(np.percentile(arr, 95))
        vm.cpu_max  = float(np.max(arr))

    if vm.rx_values:
        arr = np.array(vm.rx_values, dtype=float)
        vm.rx_mean = float(np.mean(arr))
        vm.rx_max  = float(np.max(arr))

    if vm.tx_values:
        arr = np.array(vm.tx_values, dtype=float)
        vm.tx_mean = float(np.mean(arr))
        vm.tx_max  = float(np.max(arr))


def collect_vms(reader: InfluxReader, cfg: cfg_module.Config) -> dict[str, VMTimeSeries]:
    """
    Query InfluxDB and return a dict of {vm_uuid: VMTimeSeries}.
    """
    lookback = cfg.analysis_window_days
    vms: dict[str, VMTimeSeries] = {}

    # ── CPU ──────────────────────────────────────────────────────────────────
    try:
        cpu_rows = reader.query_vm_cpu(lookback)
        for row in cpu_rows:
            uid = row.get('vm_uuid') or 'unknown'
            if uid not in vms:
                vms[uid] = VMTimeSeries(
                    vm_uuid=uid,
                    vm_name=row.get('vm_name') or '',
                    node_uuid=row.get('node_uuid') or '',
                )
            vms[uid].cpu_times.append(row['time'])
            vms[uid].cpu_values.append(float(row['value']))
        log.debug('VM CPU rows fetched', extra={'vms': len(vms)})
    except Exception:
        log.exception('Failed to fetch VM CPU metrics')

    # ── Network ──────────────────────────────────────────────────────────────
    try:
        net_rows = reader.query_vm_network(lookback)
        for row in net_rows:
            uid = row.get('vm_uuid') or 'unknown'
            if uid not in vms:
                vms[uid] = VMTimeSeries(
                    vm_uuid=uid,
                    vm_name=row.get('vm_name') or '',
                    node_uuid=row.get('node_uuid') or '',
                )
            vms[uid].net_times.append(row['time'])
            vms[uid].rx_values.append(float(row.get('rx_bit_rate') or 0.0))
            vms[uid].tx_values.append(float(row.get('tx_bit_rate') or 0.0))
        log.debug('VM network rows fetched', extra={'vms': len(vms)})
    except Exception:
        log.exception('Failed to fetch VM network metrics')

    # ── vCPU allocation ───────────────────────────────────────────────────
    try:
        vcpu_map = reader.query_vm_vcpu()
        for uid, count in vcpu_map.items():
            if uid in vms:
                vms[uid].num_vcpu = count
        log.debug('VM vCPU counts fetched', extra={'entries': len(vcpu_map)})
    except Exception:
        log.exception('Failed to fetch VM vCPU counts')

    for vm in vms.values():
        _compute_stats(vm)

    log.info('VM collection complete', extra={'vm_count': len(vms)})
    return vms


def node_network_rollup(vms: dict[str, VMTimeSeries]) -> dict[str, dict]:
    """
    Compute node-level network throughput by summing per-VM rx/tx means.
    Returns {node_uuid: {'rx_bps': float, 'tx_bps': float}}.
    This is the correct approach — HyperCore has no native node network endpoint.
    """
    totals: dict[str, dict] = {}
    for vm in vms.values():
        nid = vm.node_uuid
        if nid not in totals:
            totals[nid] = {'rx_bps': 0.0, 'tx_bps': 0.0}
        totals[nid]['rx_bps'] += vm.rx_mean or 0.0
        totals[nid]['tx_bps'] += vm.tx_mean or 0.0
    return totals
