"""
analysis/cluster_health.py — D1-D4, D6: Cluster-wide intelligence.

Computes a single ClusterHealthResult from the current forecast and
right-sizing results.  No InfluxDB or PostgreSQL access — pure computation
on already-collected in-memory data.

D1 — Cluster health score (0–100)
    Composite metric from forecast severity distribution and resource
    efficiency.  100 = perfect health; lower = action needed.

D2 — Bottleneck identification
    The resource dimension (cpu, ram, storage_capacity, etc.) with the
    worst severity or fastest annualised growth rate.

D3 — Growth rates
    Per-dimension annualised growth rate in units/year, derived directly
    from the linear regression slopes stored in each ForecastResult's
    model_metadata.  Averaged across all nodes for per-node dimensions.

D4 — Multi-cluster readiness
    Reports cluster_count = 1 for Phase 2 single-cluster deployments.
    The schema supports multiple clusters from Phase 1; this field is the
    readiness indicator for when additional clusters are registered.

D6 — Resource efficiency index (0–1)
    Measures how close VMs are to optimal utilisation.
    1.0 = every VM sized perfectly.
    0.0 = heavily over- or under-provisioned across the board.
    Derived from Phase 1 composite_score of right-sizing results.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import config as cfg_module
from analysis.forecaster  import ForecastResult
from analysis.rightsizing import RightSizingResult
from collector.nodes      import NodeTimeSeries

log = logging.getLogger(__name__)

# Maps severity level to a per-component health score (0–100)
_SEVERITY_HEALTH = {
    'EMERGENCY': 0.0,
    'CRITICAL':  25.0,
    'WARNING':   60.0,
    'ATTENTION': 80.0,
    'INFO':      100.0,
}

# For severity comparison (higher = worse)
_SEVERITY_RANK = {'EMERGENCY': 5, 'CRITICAL': 4, 'WARNING': 3, 'ATTENTION': 2, 'INFO': 1}


@dataclass
class ClusterHealthResult:
    """Cluster-wide intelligence summary — D1 through D4 and D6."""
    cluster_id: str

    # D1: aggregate cluster health (0–100, higher is healthier)
    health_score: float

    # D2: resource dimension that is the primary bottleneck
    bottleneck_resource: str | None

    # D3: annualised growth rate per dimension (units/year, averaged over nodes)
    growth_rates: dict[str, float] = field(default_factory=dict)

    # D6: resource efficiency index (0–1)
    efficiency_index: float = 1.0

    # per-dimension component health (dimension → 0–100)
    component_scores: dict[str, float] = field(default_factory=dict)

    # per-dimension structured reason for that score — used by the UI to
    # explain *why* a dimension scored what it did (HA headroom vs forecast
    # severity vs no data).  Keyed the same as component_scores.
    # Each value is a small dict: {"kind": "ha", "headroom_pct": float, "broken": bool}
    # for RAM, or {"kind": "forecast", "severity": str} for the rest.
    component_reasons: dict[str, dict] = field(default_factory=dict)

    # D4: registered cluster count (schema-level multi-cluster readiness marker)
    cluster_count: int = 1

    severity: str = 'INFO'


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _worst_severity_per_dim(forecasts: list[ForecastResult]) -> dict[str, str]:
    """Return {dimension: worst_severity} across all forecasts."""
    worst: dict[str, str] = {}
    for f in forecasts:
        dim  = f.resource_dimension
        prev = worst.get(dim)
        if prev is None or _SEVERITY_RANK.get(f.severity, 1) > _SEVERITY_RANK.get(prev, 1):
            worst[dim] = f.severity
    return worst


def _component_scores(severity_by_dim: dict[str, str]) -> dict[str, float]:
    return {dim: _SEVERITY_HEALTH.get(sev, 100.0)
            for dim, sev in severity_by_dim.items()}


def _compute_growth_rates(forecasts: list[ForecastResult]) -> dict[str, float]:
    """
    D3: annualised growth rate per dimension.
    slope (from model_metadata) is in value/day → multiply by 365.

    When a cluster-wide forecast exists for a dimension (entity_label='Cluster',
    node_uuid=None), its slope is used directly — it is the leading signal and
    is invariant under VM live-migration.  Otherwise the per-entity slopes are
    averaged as a fallback.
    """
    cluster_slope: dict[str, float] = {}
    per_entity:    dict[str, list[float]] = {}
    for f in forecasts:
        slope = f.model_metadata.get('slope')
        if slope is None:
            continue
        annual = slope * 365.0
        if f.node_uuid is None and f.entity_label == 'Cluster':
            cluster_slope[f.resource_dimension] = annual
        else:
            per_entity.setdefault(f.resource_dimension, []).append(annual)

    out: dict[str, float] = {}
    for dim in set(list(cluster_slope.keys()) + list(per_entity.keys())):
        if dim in cluster_slope:
            out[dim] = round(float(cluster_slope[dim]), 4)
        else:
            v = per_entity[dim]
            out[dim] = round(float(sum(v) / len(v)), 4)
    return out


def _efficiency_index(rightsizing: list[RightSizingResult]) -> float:
    """
    D6: Resource efficiency index.
    A VM with composite_score ≈ 0 → efficiency ≈ 1.0 (well-sized).
    A VM with composite_score ≈ 1 → efficiency ≈ 0.0 (badly sized).
    Returns the mean efficiency across all flagged VMs; 1.0 when none are flagged.
    """
    if not rightsizing:
        return 1.0
    scores = [max(0.0, 1.0 - r.composite_score) for r in rightsizing]
    return round(float(sum(scores) / len(scores)), 4)


def _overall_health(
    component_scores: dict[str, float],
    efficiency: float,
    worst_severity: str = 'INFO',
) -> float:
    """
    D1: Blend dimension component scores and efficiency into a 0–100 value.

    The efficiency (over/under-provisioning) weight is scaled by headroom urgency:
    over-provisioning is only a capacity crisis when exhaustion is imminent.
    When resources are nowhere near exhaustion, idle VMs are an optimisation
    opportunity, not an emergency — so efficiency contributes less to the score.

        EMERGENCY → 40% efficiency / 60% forecast
        CRITICAL  → 30% efficiency / 70% forecast
        WARNING   → 15% efficiency / 85% forecast
        INFO      → 5%  efficiency / 95% forecast
    """
    _EFF_WEIGHT = {
        'EMERGENCY': 0.40,
        'CRITICAL':  0.30,
        'WARNING':   0.15,
        'ATTENTION': 0.10,
        'INFO':      0.05,
    }
    eff_w  = _EFF_WEIGHT.get(worst_severity, 0.30)
    fore_w = 1.0 - eff_w

    if not component_scores:
        return round(efficiency * 100.0, 1)
    comp_avg = sum(component_scores.values()) / len(component_scores)
    blended  = fore_w * comp_avg + eff_w * (efficiency * 100.0)
    return round(max(0.0, min(100.0, blended)), 1)


def _bottleneck(
    component_scores: dict[str, float],
    growth_rates:     dict[str, float],
) -> str | None:
    """
    D2: Return the dimension with the lowest health component score.
    Tie-break by highest absolute annualised growth rate (fastest-growing
    dimension is the most urgent bottleneck).
    """
    if not component_scores:
        return None
    min_score  = min(component_scores.values())
    candidates = [d for d, s in component_scores.items() if s == min_score]
    if len(candidates) == 1:
        return candidates[0]
    return max(candidates, key=lambda d: abs(growth_rates.get(d, 0.0)))


def _ram_ha_check(
    nodes:       dict[str, NodeTimeSeries],
    vm_mem_assigned: dict[str, int],
) -> tuple[float | None, str]:
    """
    Compute RAM N-1 HA headroom.

    N-1 capacity = total cluster RAM − largest single-node RAM.
    If any node in the cluster fails, the survivors must be able to host all
    running VMs.  When assigned RAM exceeds N-1 capacity HA is already broken.

    Returns (headroom_pct, severity).
    headroom_pct is None when capacity data is unavailable.
    headroom_pct < 0 means HA is already violated.
    """
    capacities = [n.mem_capacity for n in nodes.values() if n.mem_capacity]
    if not capacities:
        return None, 'INFO'

    total_capacity = sum(capacities)
    largest_node   = max(capacities)
    n1_capacity    = total_capacity - largest_node
    if n1_capacity <= 0:
        # Single-node cluster — HA not applicable.
        return None, 'INFO'

    assigned = sum(vm_mem_assigned.values())
    headroom_pct = (n1_capacity - assigned) / n1_capacity * 100.0

    if headroom_pct < 0:
        sev = 'EMERGENCY'
    elif headroom_pct < 10:
        sev = 'CRITICAL'
    elif headroom_pct < 20:
        sev = 'WARNING'
    elif headroom_pct < 30:
        sev = 'ATTENTION'
    else:
        sev = 'INFO'

    log.info(
        'RAM HA headroom computed',
        extra={
            'assigned_gb':   round(assigned / 2**30, 1),
            'n1_capacity_gb': round(n1_capacity / 2**30, 1),
            'headroom_pct':  round(headroom_pct, 1),
            'severity':      sev,
        },
    )
    return round(headroom_pct, 1), sev


def _overall_severity(health_score: float) -> str:
    if health_score < 25.0:
        return 'EMERGENCY'
    if health_score < 50.0:
        return 'CRITICAL'
    if health_score < 70.0:
        return 'WARNING'
    if health_score < 85.0:
        return 'ATTENTION'
    return 'INFO'


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_cluster_health(
    forecasts:       list[ForecastResult],
    rightsizing:     list[RightSizingResult],
    cfg:             cfg_module.Config,
    nodes:           dict[str, NodeTimeSeries] | None = None,
    vm_mem_assigned: dict[str, int]            | None = None,
) -> ClusterHealthResult:
    """
    Compute the cluster-wide intelligence summary from the current in-memory
    forecast and right-sizing results.
    Returns a single ClusterHealthResult.

    nodes and vm_mem_assigned are used for the RAM N-1 HA headroom check.
    When omitted (or when data is unavailable), RAM component score is derived
    from forecast severity as before.
    """
    severity_by_dim  = _worst_severity_per_dim(forecasts)
    comp_scores      = _component_scores(severity_by_dim)
    growth_rates     = _compute_growth_rates(forecasts)
    efficiency       = _efficiency_index(rightsizing)

    # Reason per dimension — defaults to "forecast severity" for everything;
    # overridden for RAM below if the HA check ran.
    comp_reasons: dict[str, dict] = {
        dim: {'kind': 'forecast', 'severity': sev}
        for dim, sev in severity_by_dim.items()
    }

    # RAM N-1 HA check — overrides or fills in the forecast-based RAM score.
    ram_ha_sev = 'INFO'
    ram_headroom_pct: float | None = None
    if nodes is not None and vm_mem_assigned is not None:
        ram_headroom_pct, ram_ha_sev = _ram_ha_check(nodes, vm_mem_assigned)
        if ram_headroom_pct is not None:
            comp_scores['ram'] = _SEVERITY_HEALTH.get(ram_ha_sev, 100.0)
            comp_reasons['ram'] = {
                'kind':         'ha',
                'headroom_pct': round(ram_headroom_pct, 1),
                'broken':       ram_headroom_pct < 0,
                'severity':     ram_ha_sev,
            }

    all_sevs = list(severity_by_dim.values())
    if nodes is not None and vm_mem_assigned is not None:
        all_sevs.append(ram_ha_sev)
    worst_sev = max(all_sevs, key=lambda s: _SEVERITY_RANK.get(s, 1), default='INFO')
    health_score     = _overall_health(comp_scores, efficiency, worst_sev)
    bottleneck       = _bottleneck(comp_scores, growth_rates)
    severity         = _overall_severity(health_score)

    result = ClusterHealthResult(
        cluster_id=cfg.cluster_id,
        health_score=health_score,
        bottleneck_resource=bottleneck,
        growth_rates=growth_rates,
        efficiency_index=efficiency,
        component_scores=comp_scores,
        component_reasons=comp_reasons,
        cluster_count=1,   # D4: Phase 2 single-cluster; schema supports many
        severity=severity,
    )

    log.info(
        'Cluster health computed',
        extra={
            'health_score': health_score,
            'bottleneck':   bottleneck,
            'severity':     severity,
            'efficiency':   efficiency,
        },
    )
    return result
