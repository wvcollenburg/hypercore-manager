"""
analysis/resize_advisor.py — C3-C6: Concrete resize recommendations.

Takes Phase 1 right-sizing results (RightSizingResult) and produces
actionable resize advice with four attributes:

C3 — Concrete recommendation
    For over-provisioned VMs: suggest reducing CPU allocation until the
    workload sits at a target efficiency band (40–60% average utilisation).
    For under-provisioned VMs: suggest increasing CPU allocation until the
    p95 usage drops to 85% of the under-provisioning threshold.
    Both expressed as a fraction (e.g. -0.50 = reduce by 50%) and as an
    absolute target average %.

C4 — Confidence score
    Extends the Phase 1 composite_score with a data-age adjustment.
    Shorter analysis windows reduce confidence proportionally.

C5 — Capacity reclaim estimate
    For over-provisioned VMs: CPU percentage points freed if the
    recommendation is acted on.  None for under-provisioned VMs.

C6 — Risk classification
    LOW:    consistent usage pattern, high confidence, no spike signals.
    MEDIUM: moderate confidence or light spike activity detected.
    HIGH:   spike patterns present, or low composite confidence.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import config as cfg_module
from analysis.rightsizing import RightSizingResult

log = logging.getLogger(__name__)

# Target CPU efficiency band for over-provisioned VMs
_TARGET_CPU_AVG_LOW  = 40.0
_TARGET_CPU_AVG_HIGH = 60.0
_TARGET_CPU_AVG      = (_TARGET_CPU_AVG_LOW + _TARGET_CPU_AVG_HIGH) / 2.0  # 50.0

# Under-provisioned target: bring p95 to 85% of the p95 threshold
_TARGET_P95_HEADROOM_FACTOR = 0.85


@dataclass
class ResizeAdvice:
    """Concrete resize advice for one VM — C3 through C6."""
    cluster_id:               str
    vm_uuid:                  str
    vm_name:                  str
    node_uuid:                str
    sizing_direction:         str          # 'over_provisioned' | 'under_provisioned'

    # C3: concrete CPU change recommendation
    # Fraction: -0.50 = reduce current allocation by 50%; +0.25 = increase by 25%.
    suggested_cpu_change_pct: float
    # Recommended steady-state average CPU% after acting on the advice.
    target_cpu_avg_pct:       float

    # C4: confidence in this advice (0–1)
    confidence_score:         float

    # C5: CPU % points reclaimed (over-provisioned only; None for under-provisioned)
    capacity_reclaim_cpu_pct: float | None

    # C6: operational risk of acting on this advice
    risk_level:               str          # 'LOW' | 'MEDIUM' | 'HIGH'
    risk_detail:              dict

    severity:                 str
    right_sizing_id:          int | None   # populated by writer after DB insert

    # vCPU context (carried from right-sizing; None when vm_inventory data absent)
    current_vcpu:             int | None
    recommended_vcpu:         int | None


# ---------------------------------------------------------------------------
# C4: confidence scoring
# ---------------------------------------------------------------------------

def _confidence(rs: RightSizingResult) -> float:
    """
    Extend composite_score with an analysis-window depth adjustment.
    A 14-day window → full confidence; shorter windows scale proportionally.
    """
    base          = rs.composite_score
    window_factor = min(1.0, rs.analysis_window_days / 14.0)
    return round(base * window_factor, 4)


# ---------------------------------------------------------------------------
# C6: risk classification
# ---------------------------------------------------------------------------

def _risk(rs: RightSizingResult) -> tuple[str, dict]:
    """
    Classify the operational risk of acting on this recommendation.

    HIGH   — recurring spike patterns detected, or composite confidence < 30%.
    MEDIUM — moderate composite (30–54%), or light spike activity (score > 0.1).
    LOW    — consistent pattern, composite ≥ 55%, no meaningful spike activity.
    """
    spike_score = rs.spike_signal_score or 0.0
    composite   = rs.composite_score

    factors = {
        'spike_score': round(spike_score, 4),
        'composite':   round(composite,   4),
        'has_spikes':  spike_score > 0.3,
        'high_conf':   composite  >= 0.70,
    }

    if spike_score > 0.3 or composite < 0.30:
        return 'HIGH', {**factors, 'reason': 'spike_pattern_or_low_confidence'}
    if spike_score > 0.1 or composite < 0.55:
        return 'MEDIUM', {**factors, 'reason': 'moderate_confidence_or_light_spikes'}
    return 'LOW', {**factors, 'reason': 'consistent_pattern_high_confidence'}


# ---------------------------------------------------------------------------
# C3 + C5: concrete recommendation and reclaim calculations
# ---------------------------------------------------------------------------

def _over_provisioned_advice(
    rs: RightSizingResult,
) -> tuple[float, float, float]:
    """
    Returns (suggested_cpu_change_pct, target_cpu_avg_pct, capacity_reclaim_cpu_pct).

    Suggest reducing allocation so that the current workload lands at
    _TARGET_CPU_AVG (50%) utilisation.

    Example: current_avg = 8% → need allocation ~16% of current capacity
    → change_pct = (8 / 50) - 1 = -0.84  (reduce by 84%)
    → reclaim    = 0.84 * 8% = 6.72 percentage points freed
    """
    current_avg = max(rs.cpu_avg_usage_pct or 0.0, 1.0)   # guard div-by-zero

    change_pct = (current_avg / _TARGET_CPU_AVG) - 1.0    # negative
    reclaim    = max(0.0, abs(change_pct) * current_avg)   # CPU % points freed

    return round(change_pct, 4), round(_TARGET_CPU_AVG, 1), round(reclaim, 2)


def _under_provisioned_advice(
    rs: RightSizingResult,
) -> tuple[float, float, None]:
    """
    Returns (suggested_cpu_change_pct, target_cpu_avg_pct, None).

    Suggest increasing allocation so that the current p95 usage drops to
    _TARGET_P95_HEADROOM_FACTOR × p95_threshold (i.e. with headroom).

    Example: p95=95%, threshold=90%, target=76.5%
    → needed multiplier = 95 / 76.5 ≈ 1.24  → change_pct = +0.24
    → new avg (estimated) = current_avg / 1.24
    """
    p95_threshold = 90.0   # default; aligns with cfg default
    target_p95    = p95_threshold * _TARGET_P95_HEADROOM_FACTOR   # 76.5

    current_p95 = rs.cpu_p95_usage_pct or 0.0
    current_avg = rs.cpu_avg_usage_pct or 0.0

    if current_p95 > 0:
        change_pct = (current_p95 / target_p95) - 1.0
    else:
        change_pct = 0.25   # default 25% increase when p95 not available

    # Estimated target average after resize
    target_avg = current_avg / (1.0 + change_pct) if (1.0 + change_pct) > 0 else current_avg

    return round(change_pct, 4), round(target_avg, 1), None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_resize_advisor(
    rightsizing_results: list[RightSizingResult],
    cfg:                 cfg_module.Config,
) -> list[ResizeAdvice]:
    """
    Produce concrete resize advice (C3-C6) from Phase 1 right-sizing results.
    Returns one ResizeAdvice per RightSizingResult.
    """
    advice: list[ResizeAdvice] = []

    for rs in rightsizing_results:
        if rs.sizing_direction == 'over_provisioned':
            cpu_change, target_avg, reclaim = _over_provisioned_advice(rs)
        else:
            cpu_change, target_avg, reclaim = _under_provisioned_advice(rs)

        confidence        = _confidence(rs)
        risk_level, risks = _risk(rs)

        # Compute recommended vCPU from current allocation and the fractional change.
        # Round to nearest integer; minimum 1.
        current_vcpu    = rs.current_vcpu
        if current_vcpu and current_vcpu > 0:
            recommended_vcpu = max(1, round(current_vcpu * (1.0 + cpu_change)))
        else:
            recommended_vcpu = None

        advice.append(ResizeAdvice(
            cluster_id=rs.cluster_id,
            vm_uuid=rs.vm_uuid,
            vm_name=rs.vm_name,
            node_uuid=rs.node_uuid,
            sizing_direction=rs.sizing_direction,
            suggested_cpu_change_pct=cpu_change,
            target_cpu_avg_pct=target_avg,
            confidence_score=confidence,
            capacity_reclaim_cpu_pct=reclaim,
            risk_level=risk_level,
            risk_detail=risks,
            severity=rs.severity,
            right_sizing_id=None,   # populated by writer after DB insert
            current_vcpu=current_vcpu,
            recommended_vcpu=recommended_vcpu,
        ))

    log.info('Resize advice generated', extra={'count': len(advice)})
    return advice
