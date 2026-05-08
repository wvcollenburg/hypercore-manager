"""
collector/nodes.py — Per-node metric collection (A1, A2).

Pulls CPU and RAM data from node_metrics and returns structured
NodeSnapshot objects ready for the analysis layer.

CPU:  direct from node_metrics.cpu_usage  (A1)
RAM:  direct from node_metrics.mem_usage_bytes — allocation only (A2)
      HyperCore never overcommits RAM; we track allocation trends, not pressure.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

import config as cfg_module
from influx_client import InfluxReader

log = logging.getLogger(__name__)


@dataclass
class NodeTimeSeries:
    """
    All time-series data for a single node over the collection window.
    Values are parallel arrays ordered by ascending timestamp.
    """
    node_uuid:    str
    node_peer_id: str | None

    # CPU usage % samples
    cpu_times:    list[datetime] = field(default_factory=list)
    cpu_values:   list[float]    = field(default_factory=list)

    # RAM usage bytes samples (allocation only)
    ram_times:    list[datetime] = field(default_factory=list)
    ram_values:   list[float]    = field(default_factory=list)

    # ── derived statistics (populated by _compute_stats) ──────────────────

    # CPU
    cpu_mean:     float | None = None
    cpu_p95:      float | None = None
    cpu_max:      float | None = None

    # RAM
    ram_mean:     float | None = None    # bytes — allocated (time-series mean)
    mem_capacity: int   | None = None    # bytes — total physical RAM (latest)


def _compute_stats(node: NodeTimeSeries) -> None:
    """Populate derived stats fields in-place."""
    if node.cpu_values:
        arr = np.array(node.cpu_values, dtype=float)
        node.cpu_mean = float(np.mean(arr))
        node.cpu_p95  = float(np.percentile(arr, 95))
        node.cpu_max  = float(np.max(arr))

    if node.ram_values:
        arr = np.array(node.ram_values, dtype=float)
        node.ram_mean = float(np.mean(arr))


def collect_nodes(reader: InfluxReader, cfg: cfg_module.Config) -> dict[str, NodeTimeSeries]:
    """
    Query InfluxDB and return a dict of {node_uuid: NodeTimeSeries}.
    Uses the analysis window as the lookback period so the forecasting layer
    always has a consistent history depth.
    """
    lookback = cfg.analysis_window_days
    nodes: dict[str, NodeTimeSeries] = {}

    # ── CPU ──────────────────────────────────────────────────────────────────
    try:
        cpu_rows = reader.query_node_cpu(lookback)
        for row in cpu_rows:
            uid = row.get('node_uuid') or 'unknown'
            if uid not in nodes:
                nodes[uid] = NodeTimeSeries(
                    node_uuid=uid,
                    node_peer_id=row.get('node_peer_id'),
                )
            nodes[uid].cpu_times.append(row['time'])
            nodes[uid].cpu_values.append(float(row['value']))
        log.debug('Node CPU rows fetched', extra={'nodes': len(nodes)})
    except Exception:
        log.exception('Failed to fetch node CPU metrics')

    # ── RAM ──────────────────────────────────────────────────────────────────
    try:
        ram_rows = reader.query_node_ram(lookback)
        for row in ram_rows:
            uid = row.get('node_uuid') or 'unknown'
            if uid not in nodes:
                nodes[uid] = NodeTimeSeries(
                    node_uuid=uid,
                    node_peer_id=row.get('node_peer_id'),
                )
            nodes[uid].ram_times.append(row['time'])
            nodes[uid].ram_values.append(float(row['value']))
        log.debug('Node RAM rows fetched', extra={'nodes': len(nodes)})
    except Exception:
        log.exception('Failed to fetch node RAM metrics')

    # ── derived stats ────────────────────────────────────────────────────────
    for node in nodes.values():
        _compute_stats(node)

    # ── node RAM capacity (physical total, not usage) ─────────────────────
    try:
        capacity_map = reader.query_node_mem_capacity()
        for uid, cap in capacity_map.items():
            if uid in nodes:
                nodes[uid].mem_capacity = cap
            else:
                # node appeared in capacity query but not in metrics (e.g. offline);
                # create a stub so the HA check can still account for its RAM
                nodes[uid] = NodeTimeSeries(node_uuid=uid, node_peer_id=None, mem_capacity=cap)
        log.debug('Node mem_capacity fetched', extra={'entries': len(capacity_map)})
    except Exception:
        log.exception('Failed to fetch node mem_capacity')

    log.info('Node collection complete', extra={'node_count': len(nodes)})
    return nodes
