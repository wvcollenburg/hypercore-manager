"""
analysis/rightsizing.py — VM right-sizing engine (C1, C2).

C1 — Over-provisioned VM detection
    A VM is over-provisioned when its average CPU usage stays below
    cfg.cpu_over_provisioned_threshold (default 20%) across the full
    analysis window.  RAM right-sizing is NOT applicable on HyperCore
    (no overcommit).

C2 — Under-provisioned VM detection (three-signal model)
    Three independent signals feed a merged confidence score.

    Signal 1: CPU sustained (weighted by statistical significance)
        - avg CPU > cfg.cpu_under_provisioned_avg_threshold (default 80%)
        - OR p95 CPU > cfg.cpu_under_provisioned_p95_threshold (default 90%)
        - Score = normalised distance above threshold, weighted by sample count.

    Signal 2: Storage latency correlation
        - Detects read/write latency degradation trend on each VSD attached
          to this VM.
        - Per-VSD granularity — a single busy disk must not mask others.
        - Score = mean of per-VSD latency growth confidence scores.

    Signal 3: Recurring spike patterns (requires B8 seasonality)
        - Looks for CPU spikes repeating at regular intervals.
        - Minimum cfg.recurring_spike_min_occurrences (default 3) before flagging.
        - Bucketed by hour-of-day and weekday vs. weekend.
        - Score = fraction of qualifying buckets that breach the p95 threshold,
          weighted by the number of occurrences above the minimum.

    Composite score:
        Weighted mean of the three signal scores, where each weight is the
        statistical confidence (sample size and significance) of that signal.
        Signals with insufficient data contribute zero weight — a strong CPU
        signal is never diluted by an inconclusive latency signal.

Severity mapping (composite score → severity):
    ≥ 0.80 → CRITICAL
    ≥ 0.50 → WARNING
    otherwise → INFO
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
from scipy import stats

import config as cfg_module
from analysis.seasonality import detect as detect_seasonality
from collector.storage import StorageSnapshot, VSDTimeSeries
from collector.vms     import VMTimeSeries

log = logging.getLogger(__name__)

SeverityLevel = str   # 'INFO' | 'WARNING' | 'CRITICAL' | 'EMERGENCY'


@dataclass
class RightSizingResult:
    cluster_id:           str
    vm_uuid:              str
    vm_name:              str
    node_uuid:            str
    sizing_direction:     str           # 'over_provisioned' | 'under_provisioned'
    severity:             SeverityLevel
    analysis_window_days: int

    # Signal scores (None = inconclusive / not applicable)
    cpu_signal_score:     float | None
    cpu_avg_usage_pct:    float | None
    cpu_p95_usage_pct:    float | None
    latency_signal_score: float | None
    spike_signal_score:   float | None
    composite_score:      float

    signal_detail:        dict          # full per-signal breakdown

    current_vcpu:         int | None    # vCPU allocation at time of analysis


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _severity(composite: float) -> SeverityLevel:
    if composite >= 0.80:
        return 'CRITICAL'
    if composite >= 0.50:
        return 'WARNING'
    return 'INFO'


def _weighted_composite(scores_weights: list[tuple[float, float]]) -> float:
    """
    Compute a weighted mean of (score, weight) pairs.
    Pairs with weight ≤ 0 are excluded.
    Returns 0.0 if all weights are zero (all signals inconclusive).
    """
    total_w = sum(w for _, w in scores_weights if w > 0)
    if total_w == 0:
        return 0.0
    return sum(s * w for s, w in scores_weights if w > 0) / total_w


# ---------------------------------------------------------------------------
# Signal 1: CPU sustained
# ---------------------------------------------------------------------------

def _cpu_signal(vm: VMTimeSeries, cfg: cfg_module.Config, direction: str) -> tuple[float | None, dict]:
    """
    Returns (score, detail_dict).
    score is None if there are insufficient samples.
    direction: 'over_provisioned' or 'under_provisioned'
    """
    if not vm.cpu_values or len(vm.cpu_values) < 3:
        return None, {'reason': 'insufficient_samples', 'n': len(vm.cpu_values)}

    arr  = np.array(vm.cpu_values, dtype=float)
    mean = float(np.mean(arr))
    p95  = float(np.percentile(arr, 95))
    n    = len(arr)

    if direction == 'over_provisioned':
        threshold = cfg.cpu_over_provisioned_threshold
        if mean >= threshold:
            return 0.0, {'reason': 'not_over_provisioned', 'mean': round(mean, 2)}
        # Score = how far below threshold, normalised to [0,1]
        score = min(1.0, (threshold - mean) / threshold)
        return round(score, 4), {'mean': round(mean, 2), 'threshold': threshold, 'n': n}

    else:  # under_provisioned
        avg_thresh = cfg.cpu_under_provisioned_avg_threshold
        p95_thresh = cfg.cpu_under_provisioned_p95_threshold

        avg_breach = mean >= avg_thresh
        p95_breach = p95  >= p95_thresh

        if not avg_breach and not p95_breach:
            return 0.0, {'reason': 'below_thresholds', 'mean': round(mean, 2), 'p95': round(p95, 2)}

        # Score = normalised max exceedance above the relevant threshold
        avg_exc = max(0.0, (mean - avg_thresh) / (100.0 - avg_thresh)) if avg_breach else 0.0
        p95_exc = max(0.0, (p95  - p95_thresh) / (100.0 - p95_thresh)) if p95_breach else 0.0
        score   = min(1.0, max(avg_exc, p95_exc))

        return round(score, 4), {
            'mean':         round(mean, 2),
            'p95':          round(p95, 2),
            'avg_breach':   avg_breach,
            'p95_breach':   p95_breach,
            'avg_exc':      round(avg_exc, 4),
            'p95_exc':      round(p95_exc, 4),
            'n':            n,
        }


# ---------------------------------------------------------------------------
# Signal 2: Storage latency correlation
# ---------------------------------------------------------------------------

def _latency_signal(vm_uuid: str, storage: StorageSnapshot | None) -> tuple[float | None, dict]:
    """
    Score latency degradation trend across all VSDs attached to this VM.
    Returns the mean of per-VSD trend scores weighted by R².
    """
    if storage is None:
        return None, {'reason': 'no_storage_data'}

    vm_vsds: list[VSDTimeSeries] = [
        v for v in storage.vsds.values() if v.vm_uuid == vm_uuid
    ]
    if not vm_vsds:
        return None, {'reason': 'no_vsds_for_vm'}

    vsd_scores = []
    vsd_details = []

    for vsd in vm_vsds:
        if len(vsd.read_latency_us) < 3:
            vsd_details.append({'vsd_uuid': vsd.vsd_uuid, 'reason': 'insufficient_samples'})
            continue

        times = vsd.times
        vals  = vsd.read_latency_us

        t0 = times[0]
        x  = np.array([(t - t0).total_seconds() / 86400.0 for t in times])
        y  = np.array(vals, dtype=float)

        slope, _, r_value, p_value, _ = stats.linregress(x, y)
        r2 = r_value ** 2

        if slope <= 0 or r2 < 0.1:
            # No meaningful upward trend
            vsd_details.append({'vsd_uuid': vsd.vsd_uuid, 'slope': round(float(slope), 4), 'r2': round(r2, 4), 'trending': False})
            continue

        # Score = R² weighted by slope magnitude (normalised to current mean)
        mean_lat = float(np.mean(y))
        norm_slope = (slope / mean_lat) if mean_lat > 0 else 0.0
        score = min(1.0, r2 * min(1.0, norm_slope * 10))

        vsd_scores.append((score, r2))
        vsd_details.append({
            'vsd_uuid': vsd.vsd_uuid,
            'vsd_name': vsd.vsd_name,
            'slope_us_per_day': round(float(slope), 2),
            'r2':               round(r2, 4),
            'p_value':          round(float(p_value), 4),
            'mean_read_lat_us': round(mean_lat, 1),
            'score':            round(score, 4),
        })

    if not vsd_scores:
        return 0.0, {'vsd_details': vsd_details}

    composite = _weighted_composite(vsd_scores)
    return round(composite, 4), {'vsd_details': vsd_details, 'composite': round(composite, 4)}


# ---------------------------------------------------------------------------
# Signal 3: Recurring spike patterns
# ---------------------------------------------------------------------------

def _spike_signal(
    vm: VMTimeSeries,
    cfg: cfg_module.Config,
) -> tuple[float | None, dict]:
    """
    Detect CPU spikes that recur at fixed times (hour-of-day + weekday bucket).
    Requires at least cfg.recurring_spike_min_occurrences in a bucket to count.
    Depends on B8 seasonality to confirm periodicity exists before scoring.
    """
    if not vm.cpu_times or len(vm.cpu_times) < cfg.recurring_spike_min_occurrences * 24:
        return None, {'reason': 'insufficient_history'}

    # Check for seasonality first — signal 3 is only meaningful if there IS a pattern
    seasonality = detect_seasonality(vm.cpu_times, vm.cpu_values)
    if not seasonality.any_pattern():
        return 0.0, {'reason': 'no_seasonality_detected'}

    spike_threshold = cfg.cpu_under_provisioned_p95_threshold
    min_occ         = cfg.recurring_spike_min_occurrences

    # Bucket: (hour_of_day, is_weekend)
    buckets: dict[tuple[int, bool], list[float]] = {}
    for t, v in zip(vm.cpu_times, vm.cpu_values):
        hour       = t.hour
        is_weekend = t.weekday() >= 5
        key        = (hour, is_weekend)
        buckets.setdefault(key, []).append(v)

    qualifying_buckets = []
    spike_buckets      = []

    for (hour, is_weekend), vals in buckets.items():
        if len(vals) < min_occ:
            continue
        qualifying_buckets.append((hour, is_weekend))
        # Count occurrences that breach the spike threshold
        spikes = sum(1 for v in vals if v >= spike_threshold)
        if spikes >= min_occ:
            spike_buckets.append({
                'hour':       hour,
                'is_weekend': is_weekend,
                'occurrences': spikes,
                'total':       len(vals),
                'pct':         round(spikes / len(vals), 3),
            })

    if not qualifying_buckets or not spike_buckets:
        return 0.0, {
            'reason':             'no_recurring_spikes',
            'qualifying_buckets': len(qualifying_buckets),
            'seasonality': {
                'daily':   seasonality.has_daily,
                'weekly':  seasonality.has_weekly,
            },
        }

    # Score = fraction of qualifying buckets with recurring spikes,
    # weighted by mean spike frequency
    frac  = len(spike_buckets) / len(qualifying_buckets)
    mean_pct = float(np.mean([b['pct'] for b in spike_buckets]))
    score = min(1.0, frac * (1.0 + mean_pct))

    return round(score, 4), {
        'spike_buckets':      spike_buckets,
        'qualifying_buckets': len(qualifying_buckets),
        'score':              round(score, 4),
        'seasonality': {
            'daily':   seasonality.has_daily,
            'weekly':  seasonality.has_weekly,
        },
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_rightsizing(
    metrics_cache: dict,
    cfg: cfg_module.Config,
) -> list[RightSizingResult]:
    """
    Analyse all VMs for over/under-provisioning.
    Returns a flat list of RightSizingResult objects (one per VM per direction
    if both conditions are present, though in practice a VM is flagged for one).
    """
    vms:     dict[str, VMTimeSeries] = metrics_cache.get('vms',     {})
    storage: StorageSnapshot | None  = metrics_cache.get('storage')
    results: list[RightSizingResult] = []

    for vm in vms.values():
        if not vm.cpu_values:
            continue

        # Skip VMs not running on any node — powered-off and template VMs have no
        # node assignment (reported as peer unknown on the dashboard / empty node_uuid
        # in InfluxDB).  Their cpu_usage is 0.0 for the entire window, which would
        # falsely flag them as maximally over-provisioned.
        if not vm.node_uuid or vm.node_uuid.lower() in ('', 'unknown'):
            log.debug('Skipping VM with no node assignment (powered off/template)',
                      extra={'vm_name': vm.vm_name})
            continue

        cpu_mean = vm.cpu_mean or 0.0
        cpu_p95  = vm.cpu_p95  or 0.0

        # ── C1: Over-provisioned ──────────────────────────────────────────
        if cpu_mean < cfg.cpu_over_provisioned_threshold:
            cpu_score, cpu_detail = _cpu_signal(vm, cfg, 'over_provisioned')
            if cpu_score is not None and cpu_score > 0:
                composite = cpu_score  # Only one signal for C1
                results.append(RightSizingResult(
                    cluster_id=cfg.cluster_id,
                    vm_uuid=vm.vm_uuid,
                    vm_name=vm.vm_name,
                    node_uuid=vm.node_uuid,
                    sizing_direction='over_provisioned',
                    severity=_severity(composite),
                    analysis_window_days=cfg.analysis_window_days,
                    cpu_signal_score=cpu_score,
                    cpu_avg_usage_pct=round(cpu_mean, 2),
                    cpu_p95_usage_pct=round(cpu_p95, 2),
                    latency_signal_score=None,
                    spike_signal_score=None,
                    composite_score=round(composite, 4),
                    signal_detail={'cpu': cpu_detail},
                    current_vcpu=vm.num_vcpu,
                ))

        # ── C2: Under-provisioned ─────────────────────────────────────────
        # Only run if there's any indication of high CPU — avoids noisy results
        if cpu_mean >= cfg.cpu_over_provisioned_threshold:
            s1_score, s1_detail = _cpu_signal(vm, cfg, 'under_provisioned')
            s2_score, s2_detail = _latency_signal(vm.vm_uuid, storage)
            s3_score, s3_detail = _spike_signal(vm, cfg)

            # Weight each signal by its statistical confidence
            # Signals returning None have zero weight (inconclusive)
            s1_weight = float(len(vm.cpu_values)) / 1000.0 if s1_score is not None else 0.0
            s2_weight = 0.5 if s2_score is not None and s2_score > 0 else 0.0
            s3_weight = 0.5 if s3_score is not None and s3_score > 0 else 0.0
            # Normalise weights so they don't exceed 1.0 each
            s1_weight = min(1.0, s1_weight)

            composite = _weighted_composite([
                (s1_score or 0.0, s1_weight),
                (s2_score or 0.0, s2_weight),
                (s3_score or 0.0, s3_weight),
            ])

            if composite > 0.0:
                results.append(RightSizingResult(
                    cluster_id=cfg.cluster_id,
                    vm_uuid=vm.vm_uuid,
                    vm_name=vm.vm_name,
                    node_uuid=vm.node_uuid,
                    sizing_direction='under_provisioned',
                    severity=_severity(composite),
                    analysis_window_days=cfg.analysis_window_days,
                    cpu_signal_score=s1_score,
                    cpu_avg_usage_pct=round(cpu_mean, 2),
                    cpu_p95_usage_pct=round(cpu_p95, 2),
                    latency_signal_score=s2_score,
                    spike_signal_score=s3_score,
                    composite_score=round(composite, 4),
                    signal_detail={
                        'signal_1_cpu':     s1_detail,
                        'signal_2_latency': s2_detail,
                        'signal_3_spikes':  s3_detail,
                    },
                    current_vcpu=vm.num_vcpu,
                ))

    log.info('Right-sizing analysis complete', extra={'results': len(results)})
    return results
