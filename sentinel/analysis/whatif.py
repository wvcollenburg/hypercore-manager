"""
analysis/whatif.py — D5: What-if scenario modelling.

Simulates the effect of adding nodes or VMs on:
  - Cluster health score and severity
  - Per-dimension resource exhaustion dates
  - Bottleneck resource

All inputs come from the current state already stored in PostgreSQL
(latest forecast_snapshots and cluster_health_scores rows).  No InfluxDB
access is required — this is pure computation on already-concluded data.

Supported scenarios
-------------------
add_nodes
    Adds N nodes to the cluster.  Each node contributes capacity (CPU, RAM,
    storage) and dilutes the per-node load average.  Growth rates are
    unchanged — the cluster is still growing at the same pace but there is
    more headroom.

    CPU exhaustion: new capacity = existing_node_count + N normalised to the
    per-cluster percentage space.  Current average load and growth rate are
    used to project when the cluster re-hits exhaustion.

    Storage exhaustion: same capacity-dilution model applied to used_bytes.

    RAM: added purely as additional allocation headroom (growth rate unchanged).

add_vms
    Adds M new VMs.  Each new VM is assumed to consume the cluster average
    per-VM resource (derived from current forecasts and right-sizing data).
    This increases the effective growth slope for CPU and network dimensions.

    Storage exhaustion date is shortened proportionally to VM count increase.
    RAM allocation grows at the same per-VM average rate.

Response shape
--------------
{
    "scenario": "add_nodes",
    "count": 2,
    "baseline": {
        "health_score": 45.2,
        "severity": "CRITICAL",
        "bottleneck": "cpu",
        "exhaustion_dates": {"cpu": "2026-06-15", ...},
        "node_count": 3
    },
    "simulated": {
        "health_score": 72.5,
        "severity": "WARNING",
        "bottleneck": "storage_capacity",
        "exhaustion_dates": {"cpu": "2027-09-20", ...},
        "node_count": 5
    },
    "delta": {
        "health_score": 27.3,
        "bottleneck_changed": true,
        "exhaustion_gained_days": {"cpu": 462, ...}
    },
    "assumptions": { ... }
}
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal

log = logging.getLogger(__name__)

ScenarioType = Literal['add_nodes', 'add_vms', 'add_vms_like']

# Severity-to-health mapping (mirrors cluster_health.py)
_SEVERITY_HEALTH = {
    'EMERGENCY': 0.0,
    'CRITICAL':  25.0,
    'WARNING':   60.0,
    'ATTENTION': 82.0,
    'INFO':      100.0,
}
_HEALTH_SEVERITY = [
    (25.0,  'EMERGENCY'),
    (50.0,  'CRITICAL'),
    (75.0,  'WARNING'),
    (101.0, 'INFO'),
]


# ---------------------------------------------------------------------------
# Input / output types
# ---------------------------------------------------------------------------

@dataclass
class WhatIfRequest:
    scenario:          ScenarioType
    count:             int           # number of nodes or VMs to add
    # add_nodes: optional per-node capacity overrides (defaults = cluster avg)
    node_cpu_headroom_pct: float | None = None   # assumed avg CPU % free per new node
    node_storage_bytes:    int   | None = None   # assumed storage bytes per new node
    node_ram_bytes:        int   | None = None   # assumed RAM bytes per new node
    # add_vms_like: the template VM whose resource profile should be replicated
    source_vm_name:        str   | None = None


@dataclass
class _DimState:
    """Per-dimension state extracted from PostgreSQL rows."""
    current_usage_pct: float | None  = None  # as % of capacity (CPU only)
    current_bytes:     float | None  = None  # raw bytes (storage, ram)
    growth_rate_day:   float | None  = None  # slope in value/day
    exhaustion_date:   date  | None  = None
    severity:          str           = 'INFO'


@dataclass
class WhatIfResult:
    scenario:     ScenarioType
    count:        int
    baseline:     dict = field(default_factory=dict)
    simulated:    dict = field(default_factory=dict)
    delta:        dict = field(default_factory=dict)
    assumptions:  dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _severity_from_score(score: float) -> str:
    for threshold, label in _HEALTH_SEVERITY:
        if score < threshold:
            return label
    return 'INFO'


def _days_until(d: date | None) -> int | None:
    if d is None:
        return None
    return max(0, (d - date.today()).days)


def _new_exhaustion_from_diluted_growth(
    current_usage: float,
    capacity: float,
    growth_rate_per_day: float,
    extra_capacity: float,
) -> date | None:
    """
    Given current_usage approaching capacity at growth_rate_per_day, compute
    when it will reach (capacity + extra_capacity).
    Returns None if growth_rate ≤ 0 or headroom is effectively unlimited.
    """
    if growth_rate_per_day is None or growth_rate_per_day <= 0:
        return None
    new_cap = capacity + extra_capacity
    if current_usage >= new_cap:
        return date.today()
    days = (new_cap - current_usage) / growth_rate_per_day
    if days > 3650:
        return None
    return date.today() + timedelta(days=int(days))


def _new_exhaustion_from_faster_growth(
    current_usage: float,
    capacity: float,
    new_growth_rate_per_day: float,
) -> date | None:
    if new_growth_rate_per_day is None or new_growth_rate_per_day <= 0:
        return None
    if current_usage >= capacity:
        return date.today()
    days = (capacity - current_usage) / new_growth_rate_per_day
    if days > 3650:
        return None
    return date.today() + timedelta(days=int(days))


def _sev_from_date(d: date | None) -> str:
    if d is None:
        return 'INFO'
    days = (d - date.today()).days
    if days <= 7:
        return 'EMERGENCY'
    elif days <= 30:
        return 'CRITICAL'
    elif days <= 180:
        return 'WARNING'
    elif days <= 365:
        return 'ATTENTION'
    return 'INFO'


def _health_score_anchored(
    baseline_score: float,
    baseline_exhaustion: dict[str, date | None],
    sim_exhaustion: dict[str, date | None],
) -> tuple[float, dict[str, str]]:
    """
    Compute simulated health score anchored to the actual stored baseline score.

    Rather than rebuilding the score from first principles (which would miss
    right-sizing signals and other factors already baked into the stored score),
    we compute the *delta* from changed per-dimension severities and apply it
    to the actual baseline.  This ensures adding VMs always makes the score
    worse (or equal) and adding nodes always makes it better (or equal).
    """
    all_dims = set(baseline_exhaustion) | set(sim_exhaustion)
    if not all_dims:
        return round(baseline_score, 1), {}

    n = len(all_dims)
    sim_severities  = {d: _sev_from_date(sim_exhaustion.get(d))      for d in all_dims}
    base_severities = {d: _sev_from_date(baseline_exhaustion.get(d)) for d in all_dims}

    delta = sum(
        _SEVERITY_HEALTH[sim_severities[d]] - _SEVERITY_HEALTH[base_severities[d]]
        for d in all_dims
    ) / n

    score = round(baseline_score + 0.70 * delta, 1)
    score = max(0.0, min(100.0, score))
    return score, sim_severities


# ---------------------------------------------------------------------------
# add_nodes simulation
# ---------------------------------------------------------------------------

def _simulate_add_nodes(
    req: WhatIfRequest,
    baseline_health: dict,
    dim_states: dict[str, _DimState],
    node_count: int,
) -> tuple[dict, dict]:
    """
    Simulate adding req.count new nodes.

    Strategy per dimension:
      cpu             — capacity increases by count/node_count (more nodes share load)
      storage_capacity — same capacity-dilution model
      ram             — additional RAM headroom; growth rate unchanged
      network/latency — unaffected by adding nodes (demand-side unchanged)
    """
    new_node_count = node_count + req.count

    sim_dates:     dict[str, date | None] = {}
    assumptions:   dict[str, str]         = {}

    for dim, state in dim_states.items():
        if state.exhaustion_date is None and state.growth_rate_day is not None and state.growth_rate_day <= 0:
            sim_dates[dim] = None
            continue

        if dim == 'cpu':
            # Each new node adds one node-worth of CPU headroom.
            # Current cluster CPU capacity = node_count × 100 % points.
            # New cluster CPU capacity = new_node_count × 100 % points.
            # But forecasts are per-node %, so capacity per-node stays at 100%.
            # Model: cluster-wide avg CPU = current_usage × node_count / new_node_count.
            if node_count == 0:
                sim_dates[dim] = state.exhaustion_date
                continue

            # Effective extra headroom in % terms (per original cluster basis)
            extra_pct = (req.count / node_count) * 100.0
            current   = state.current_usage_pct or 0.0
            growth    = state.growth_rate_day or 0.0

            sim_dates[dim] = _new_exhaustion_from_diluted_growth(
                current_usage=current,
                capacity=100.0,
                growth_rate_per_day=growth,
                extra_capacity=extra_pct,
            )
            assumptions['cpu'] = (
                f'CPU headroom increased by {req.count}/{node_count} = '
                f'{extra_pct:.1f}% cluster-equivalent; growth rate unchanged.'
            )

        elif dim == 'storage_capacity':
            override = req.node_storage_bytes
            if override is not None:
                extra_bytes = override * req.count
            elif state.current_bytes and node_count:
                # Assume new nodes match current cluster average storage
                extra_bytes = (state.current_bytes / node_count) * req.count
            else:
                sim_dates[dim] = state.exhaustion_date
                continue

            sim_dates[dim] = _new_exhaustion_from_diluted_growth(
                current_usage=state.current_bytes or 0.0,
                capacity=(state.current_bytes or 0.0) * 1.5,  # headroom proxy
                growth_rate_per_day=state.growth_rate_day or 0.0,
                extra_capacity=extra_bytes,
            )
            assumptions['storage_capacity'] = (
                f'Storage capacity increased by {extra_bytes / 1e12:.2f} TB '
                f'({req.count} node(s) × cluster-average).'
            )

        elif dim == 'ram':
            override = req.node_ram_bytes
            if override is not None:
                extra_ram = override * req.count
            elif state.current_bytes and node_count:
                extra_ram = (state.current_bytes / node_count) * req.count
            else:
                sim_dates[dim] = state.exhaustion_date
                continue

            sim_dates[dim] = _new_exhaustion_from_diluted_growth(
                current_usage=state.current_bytes or 0.0,
                capacity=(state.current_bytes or 0.0) * 2.0,  # headroom proxy
                growth_rate_per_day=state.growth_rate_day or 0.0,
                extra_capacity=extra_ram,
            )
            assumptions['ram'] = (
                f'RAM headroom increased by {extra_ram / 1e9:.1f} GB '
                f'({req.count} node(s) × cluster-average).'
            )

        else:
            # network / storage_latency — not affected by adding nodes
            sim_dates[dim] = state.exhaustion_date

    baseline_exhaustion = {d: s.exhaustion_date for d, s in dim_states.items()}
    score, comp_severities = _health_score_anchored(
        baseline_health['health_score'], baseline_exhaustion, sim_dates
    )
    severity = _severity_from_score(score)

    bottleneck = None
    if comp_severities:
        _rank = {'EMERGENCY': 5, 'CRITICAL': 4, 'WARNING': 3, 'ATTENTION': 2, 'INFO': 1}
        bottleneck = max(comp_severities, key=lambda d: _rank.get(comp_severities[d], 0))

    simulated = {
        'health_score':    score,
        'severity':        severity,
        'bottleneck':      bottleneck,
        'exhaustion_dates': {d: v.isoformat() if v else None for d, v in sim_dates.items()},
        'node_count':      new_node_count,
        'component_severities': comp_severities,
    }
    return simulated, assumptions


# ---------------------------------------------------------------------------
# add_vms simulation
# ---------------------------------------------------------------------------

def _simulate_add_vms(
    req: WhatIfRequest,
    baseline_health: dict,
    dim_states: dict[str, _DimState],
    vm_count: int,
) -> tuple[dict, dict]:
    """
    Simulate adding req.count new VMs.

    Strategy per dimension:
      cpu / network / storage_capacity / ram
          — If an exhaustion date already exists, compress time-to-exhaust by
            scale_factor (days_remaining / scale_factor).  This anchors the
            result to the actual forecaster output rather than recomputing from
            a potentially mis-matched slope/current_usage pair.
          — If no exhaustion date exists, attempt to create one using the scaled
            growth rate against estimated remaining capacity.
      storage_latency — unaffected (latency is per-VSD, not per-VM count).
    """
    if vm_count == 0:
        return {**baseline_health, 'node_count': None, 'vm_count': req.count}, {
            'note': 'No existing VMs in forecast data; VM count simulation unavailable.'
        }

    scale_factor = (vm_count + req.count) / vm_count
    sim_dates:   dict[str, date | None] = {}
    assumptions: dict[str, str]         = {}

    for dim, state in dim_states.items():
        if dim == 'storage_latency':
            sim_dates[dim] = state.exhaustion_date
            continue

        growth = state.growth_rate_day
        if growth is None or growth <= 0:
            # No positive growth; adding VMs has no modelled effect
            sim_dates[dim] = state.exhaustion_date
            continue

        if state.exhaustion_date is not None:
            # Anchor to stored date: more VMs → faster growth → less time remaining
            days_remaining = max(0, (state.exhaustion_date - date.today()).days)
            new_days = max(0, int(days_remaining / scale_factor))
            sim_dates[dim] = date.today() + timedelta(days=new_days)
        else:
            # No current exhaustion projected; with faster growth one may appear
            new_growth = growth * scale_factor
            ref_usage  = state.current_usage_pct if dim == 'cpu' else state.current_bytes
            capacity   = 100.0 if dim == 'cpu' else ((ref_usage or 1.0) * 2.0)
            sim_dates[dim] = _new_exhaustion_from_faster_growth(
                current_usage=ref_usage or 0.0,
                capacity=capacity,
                new_growth_rate_per_day=new_growth,
            )

    assumptions['all_dimensions'] = (
        f'Growth rates scaled by {scale_factor:.3f}× '
        f'(existing {vm_count} VMs + {req.count} new VMs).'
    )

    baseline_exhaustion = {d: s.exhaustion_date for d, s in dim_states.items()}
    score, comp_severities = _health_score_anchored(
        baseline_health['health_score'], baseline_exhaustion, sim_dates
    )
    severity = _severity_from_score(score)

    bottleneck = None
    if comp_severities:
        _rank = {'EMERGENCY': 5, 'CRITICAL': 4, 'WARNING': 3, 'ATTENTION': 2, 'INFO': 1}
        bottleneck = max(comp_severities, key=lambda d: _rank.get(comp_severities[d], 0))

    simulated = {
        'health_score':    score,
        'severity':        severity,
        'bottleneck':      bottleneck,
        'exhaustion_dates': {d: v.isoformat() if v else None for d, v in sim_dates.items()},
        'vm_count':        vm_count + req.count,
        'component_severities': comp_severities,
    }
    return simulated, assumptions


# ---------------------------------------------------------------------------
# add_vms_like simulation
# ---------------------------------------------------------------------------

def _simulate_add_vms_like(
    req: WhatIfRequest,
    baseline_health: dict,
    dim_states: dict[str, _DimState],
    vm_count: int,
    source_vm_profile: dict,
) -> tuple[dict, dict]:
    """
    Simulate adding req.count VMs that each match the resource profile of a
    specific existing VM.

    Unlike add_vms (which scales cluster-wide growth rates), this uses the
    template VM's *absolute* per-dimension contribution:

      cpu             — VM's share of cluster CPU derived from right-sizing
                        utilisation data (cpu_fraction × current_cluster_cpu_pct)
      network         — VM's actual network current_usage bytes from its forecast
      ram / storage   — fallback to cluster-average per VM (no per-VM data)
      storage_latency — unaffected

    For dimensions with an existing exhaustion date, the additional load
    shortens the runway:
        days_saved = (count × vm_contribution) / growth_rate_per_day
        new_exhaustion = existing_exhaustion − days_saved

    For dimensions with no current exhaustion, a new date is projected from the
    elevated starting usage.
    """
    sim_dates:    dict[str, date | None] = {}
    assumptions:  dict[str, str]         = {}

    cpu_fraction     = source_vm_profile.get('cpu_fraction')       # VM share of cluster CPU
    network_bytes    = source_vm_profile.get('network_bytes')      # VM absolute network bytes
    network_fraction = source_vm_profile.get('network_fraction')   # VM share of cluster network

    for dim, state in dim_states.items():
        if dim == 'storage_latency':
            sim_dates[dim] = state.exhaustion_date
            continue

        growth = state.growth_rate_day
        if growth is None or growth <= 0:
            sim_dates[dim] = state.exhaustion_date
            continue

        # ── Derive this VM's absolute contribution in the same units as dim ──
        contribution: float | None = None
        source_label = 'cluster avg'

        if dim == 'cpu' and cpu_fraction is not None:
            current = state.current_usage_pct or 0.0
            contribution = current * cpu_fraction
            source_label = f'{source_vm_profile.get("vm_name")} right-sizing'

        elif dim == 'network':
            if network_bytes is not None:
                contribution = network_bytes
                source_label = f'{source_vm_profile.get("vm_name")} network forecast'
            elif network_fraction is not None:
                contribution = (state.current_bytes or 0.0) * network_fraction
                source_label = f'{source_vm_profile.get("vm_name")} network forecast'

        elif dim == 'ram':
            vm_ram = source_vm_profile.get('mem_bytes_allocated')
            if vm_ram is not None:
                contribution = float(vm_ram)
                source_label = f'{source_vm_profile.get("vm_name")} RAM allocation'

        if contribution is None:
            # Fallback: spread current load evenly across known VMs
            if vm_count > 0:
                ref = state.current_usage_pct if dim == 'cpu' else (state.current_bytes or 0.0)
                contribution = ref / vm_count
                source_label = 'cluster avg (no VM-specific data)'
            else:
                sim_dates[dim] = state.exhaustion_date
                continue

        total_increment = req.count * contribution
        assumptions[dim] = source_label

        if state.exhaustion_date is not None:
            # Shorten existing runway by the additional load
            days_remaining = max(0, (state.exhaustion_date - date.today()).days)
            days_saved = total_increment / growth
            new_days = max(0, int(days_remaining - days_saved))
            sim_dates[dim] = date.today() + timedelta(days=new_days)
        else:
            # No current exhaustion; project from elevated starting usage
            ref = state.current_usage_pct if dim == 'cpu' else (state.current_bytes or 0.0)
            capacity = 100.0 if dim == 'cpu' else (ref * 2.0 or 1.0)
            new_usage = ref + total_increment
            if new_usage >= capacity:
                sim_dates[dim] = date.today()
            else:
                days = (capacity - new_usage) / growth
                sim_dates[dim] = (date.today() + timedelta(days=int(days))) if days <= 3650 else None

    baseline_exhaustion = {d: s.exhaustion_date for d, s in dim_states.items()}
    score, comp_severities = _health_score_anchored(
        baseline_health['health_score'], baseline_exhaustion, sim_dates
    )
    severity = _severity_from_score(score)

    bottleneck = None
    if comp_severities:
        _rank = {'EMERGENCY': 5, 'CRITICAL': 4, 'WARNING': 3, 'ATTENTION': 2, 'INFO': 1}
        bottleneck = max(comp_severities, key=lambda d: _rank.get(comp_severities[d], 0))

    simulated = {
        'health_score':     score,
        'severity':         severity,
        'bottleneck':       bottleneck,
        'exhaustion_dates': {d: v.isoformat() if v else None for d, v in sim_dates.items()},
        'vm_count':         vm_count + req.count,
        'component_severities': comp_severities,
        'source_vm':        source_vm_profile.get('vm_name'),
    }
    return simulated, assumptions


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_whatif(req: WhatIfRequest, pg_state: dict) -> WhatIfResult:
    """
    Run a what-if scenario simulation.

    pg_state keys (all pre-fetched by the caller from PostgreSQL):
      health_row      — latest cluster_health_scores row
      forecast_rows   — latest forecast_snapshots rows (all dimensions/horizons)
      node_count      — count of distinct node_uuids with CPU forecasts
      vm_count        — count of distinct vm entity labels with network forecasts
      exhaustion_rows — latest resource_exhaustion rows
    """
    health_row     = pg_state.get('health_row') or {}
    forecast_rows  = pg_state.get('forecast_rows') or []
    exhaustion_rows = pg_state.get('exhaustion_rows') or []
    node_count     = pg_state.get('node_count') or 1
    vm_count       = pg_state.get('vm_count') or 0
    efficiency     = float(health_row.get('efficiency_index') or 1.0)

    # ── Build baseline exhaustion dates and dim states ───────────────────────
    dim_states: dict[str, _DimState] = {}

    # Populate growth rates from 30d forecast rows (most stable)
    for row in forecast_rows:
        if row.get('forecast_horizon_days') != 30:
            continue
        dim   = row.get('resource_dimension') or ''
        if not dim:
            continue
        state = dim_states.setdefault(dim, _DimState())
        meta  = row.get('model_metadata') or {}
        slope = meta.get('slope')
        if slope is not None:
            # Accumulate slopes; average later
            if state.growth_rate_day is None:
                state.growth_rate_day = slope
            else:
                state.growth_rate_day = (state.growth_rate_day + slope) / 2.0
        if row.get('current_usage') is not None:
            if dim == 'cpu':
                state.current_usage_pct = float(row['current_usage'])
            else:
                state.current_bytes = float(row['current_usage'])

    # Populate exhaustion dates and per-dim severity from exhaustion rows
    for row in exhaustion_rows:
        dim   = row.get('resource_dimension') or ''
        state = dim_states.setdefault(dim, _DimState())
        raw_date = row.get('exhaustion_date')
        if raw_date is not None:
            if isinstance(raw_date, str):
                from datetime import date as _date
                try:
                    state.exhaustion_date = _date.fromisoformat(raw_date[:10])
                except ValueError:
                    pass
            else:
                state.exhaustion_date = raw_date
        state.severity = row.get('severity') or 'INFO'

    # ── Baseline summary ─────────────────────────────────────────────────────
    baseline_exhaustion = {
        d: s.exhaustion_date.isoformat() if s.exhaustion_date else None
        for d, s in dim_states.items()
    }
    baseline = {
        'health_score':    float(health_row.get('health_score') or 0.0),
        'severity':        health_row.get('severity') or 'INFO',
        'bottleneck':      health_row.get('bottleneck_resource'),
        'exhaustion_dates': baseline_exhaustion,
        'node_count':      node_count,
        'vm_count':        vm_count,
    }

    # ── Run scenario ─────────────────────────────────────────────────────────
    if req.scenario == 'add_nodes':
        simulated, assumptions = _simulate_add_nodes(
            req, baseline, dim_states, node_count
        )
    elif req.scenario == 'add_vms_like':
        source_vm_profile = pg_state.get('source_vm_profile') or {}
        simulated, assumptions = _simulate_add_vms_like(
            req, baseline, dim_states, vm_count, source_vm_profile
        )
    else:  # add_vms
        simulated, assumptions = _simulate_add_vms(
            req, baseline, dim_states, vm_count
        )

    # ── Delta ────────────────────────────────────────────────────────────────
    health_delta = round(simulated['health_score'] - baseline['health_score'], 1)
    exhaustion_gained: dict[str, int | None] = {}
    for dim in set(list(baseline['exhaustion_dates']) + list(simulated['exhaustion_dates'])):
        b_str = baseline['exhaustion_dates'].get(dim)
        s_str = simulated['exhaustion_dates'].get(dim)
        b_days = _days_until(date.fromisoformat(b_str) if b_str else None)
        s_days = _days_until(date.fromisoformat(s_str) if s_str else None)
        if b_days is not None and s_days is not None:
            exhaustion_gained[dim] = s_days - b_days
        else:
            exhaustion_gained[dim] = None

    delta = {
        'health_score':         health_delta,
        'bottleneck_changed':   baseline.get('bottleneck') != simulated.get('bottleneck'),
        'exhaustion_gained_days': exhaustion_gained,
    }

    log.info(
        'What-if simulation complete',
        extra={
            'scenario':       req.scenario,
            'count':          req.count,
            'baseline_score': baseline['health_score'],
            'sim_score':      simulated['health_score'],
            'health_delta':   health_delta,
        },
    )

    return WhatIfResult(
        scenario=req.scenario,
        count=req.count,
        baseline=baseline,
        simulated=simulated,
        delta=delta,
        assumptions=assumptions,
    )
