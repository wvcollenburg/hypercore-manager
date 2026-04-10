"""
analysis/forecaster.py — Forecasting engine (B1, B2, B3, B4, B5, B6, B7, B9).

For each resource dimension and each enabled horizon (30d, 90d, 180d, 365d),
fits a linear trend to the collected time series and produces a ForecastResult.

Phase 2 additions:
  B3  6-month (180d) strategic forecast.
  B4  12-month (365d) strategic forecast.
  B9  Anomaly filtering — IQR-fence outlier removal applied to each series
      before fitting when cfg.anomaly_filter_enabled is True.

Linear regression is appropriate for Phase 1/2:
- It is interpretable (slope = growth rate per day).
- R² directly maps to confidence score.
- It is cheap — no Prophet dependency, no training time.

Confidence scoring (B5):
    confidence = R² * (1 − p_value_penalty)
    where p_value_penalty reduces confidence when the trend is not
    statistically significant (p > 0.05 → penalty applied).

Exhaustion date (B6):
    Extrapolate the linear model forward until projected_value ≥ capacity.
    Returns None if the trend is flat or declining.

Driving metric (B7):
    For cluster-wide forecasts the driving metric is whichever resource
    dimension reaches exhaustion soonest.  For per-node forecasts it is the
    dimension with the steepest growth rate normalised to capacity.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Literal

import numpy as np
from scipy import stats

import config as cfg_module
from analysis.anomaly_filter import filter_series
from analysis.seasonality    import SeasonalityResult, detect as detect_seasonality
from collector.nodes   import NodeTimeSeries
from collector.storage import StorageSnapshot
from collector.vms     import VMTimeSeries

log = logging.getLogger(__name__)

ResourceDimension = Literal['cpu', 'ram', 'storage_capacity', 'storage_latency', 'network']
SeverityLevel     = Literal['INFO', 'ATTENTION', 'WARNING', 'CRITICAL', 'EMERGENCY']

# Exhaustion severity thresholds (days until full)
# > 365 days is informational — not yet actionable.
_SEVERITY_THRESHOLDS = [
    (7,   'EMERGENCY'),
    (30,  'CRITICAL'),
    (180, 'WARNING'),
    (365, 'ATTENTION'),
]


@dataclass
class ForecastResult:
    """One forecast for one resource dimension at one horizon."""
    cluster_id:           str
    node_uuid:            str | None          # None = cluster-wide
    entity_label:         str | None          # human-readable: peer_id / vsd_name / vm_name
    resource_dimension:   ResourceDimension
    forecast_horizon_days: int

    current_usage:        float | None
    current_capacity:     float | None
    forecast_value:       float | None        # projected usage at horizon end
    confidence_score:     float               # 0.0–1.0
    driving_metric:       str | None          # sub-field driving the forecast
    exhaustion_date:      date  | None
    severity:             SeverityLevel
    model_metadata:       dict                # slope, r2, p_value, n_samples, seasonality


# ---------------------------------------------------------------------------
# Internal: fit a trend line to a time series
# ---------------------------------------------------------------------------

def _fit(times: list[datetime], values: list[float]) -> dict | None:
    """
    Fit a linear trend to (time, value) pairs.
    Returns a dict with slope, intercept, r2, p_value, stderr, n_samples;
    or None if there are fewer than 3 samples.
    """
    n = len(values)
    if n < 3:
        return None

    # Convert datetimes to elapsed days from the first sample
    t0 = times[0]
    x = np.array([(t - t0).total_seconds() / 86400.0 for t in times], dtype=float)
    y = np.array(values, dtype=float)

    slope, intercept, r_value, p_value, stderr = stats.linregress(x, y)

    data_span_days = (times[-1] - times[0]).total_seconds() / 86400.0

    return {
        'slope':           float(slope),
        'intercept':       float(intercept),
        'r2':              float(r_value ** 2),
        'p_value':         float(p_value),
        'stderr':          float(stderr),
        'n_samples':       n,
        't0':              t0,
        'data_span_days':  data_span_days,
    }


def _confidence(fit: dict, horizon_days: int) -> float:
    """
    Map R², p-value, and data coverage to a 0–1 confidence score.

    Penalties applied:
    - p-value penalty: reduces confidence when the trend is not statistically
      significant (p > 0.05 → linear penalty up to 0.5 at p=1.0).
    - coverage penalty: reduces confidence proportionally when the observed
      data span is shorter than the forecast horizon.  2 hours of data for a
      30-day forecast → ~99% coverage penalty; 28 days for 30-day → ~7%.
    """
    r2 = max(0.0, min(1.0, fit['r2']))
    p  = fit['p_value']

    p_penalty = max(0.0, (p - 0.05) / 0.95 * 0.5) if p > 0.05 else 0.0

    coverage = min(1.0, fit.get('data_span_days', 0.0) / max(horizon_days, 1))
    coverage_penalty = 1.0 - coverage

    return round(max(0.0, r2 - p_penalty - coverage_penalty), 4)


def _project(fit: dict, days_ahead: int) -> float:
    """Return the model's predicted value N days after the last data point."""
    x_now = (datetime.now(timezone.utc) - fit['t0']).total_seconds() / 86400.0
    return fit['slope'] * (x_now + days_ahead) + fit['intercept']


def _exhaustion_date(fit: dict, capacity: float) -> date | None:
    """
    Calculate when the linear trend crosses capacity.
    Returns None if the trend is flat/declining or never reaches capacity.
    """
    if fit['slope'] <= 0 or capacity is None:
        return None
    x_now    = (datetime.now(timezone.utc) - fit['t0']).total_seconds() / 86400.0
    current  = fit['slope'] * x_now + fit['intercept']
    if current >= capacity:
        return date.today()
    days_left = (capacity - current) / fit['slope']
    if days_left > 3650:  # more than 10 years — not actionable
        return None
    return (date.today() + timedelta(days=int(days_left)))


def _severity_from_exhaustion(exhaustion: date | None) -> SeverityLevel:
    if exhaustion is None:
        return 'INFO'
    days = (exhaustion - date.today()).days
    for threshold, level in _SEVERITY_THRESHOLDS:
        if days <= threshold:
            return level
    return 'INFO'


# ---------------------------------------------------------------------------
# B9 — Anomaly filter wrapper
# ---------------------------------------------------------------------------

def _maybe_filter(
    times:  list,
    values: list,
    cfg:    cfg_module.Config,
) -> tuple[list, list]:
    """Apply anomaly filtering when enabled in config (B9)."""
    if cfg.anomaly_filter_enabled:
        return filter_series(times, values)
    return times, values


# ---------------------------------------------------------------------------
# Per-dimension forecast builders
# ---------------------------------------------------------------------------

def _forecast_node_cpu(
    node: NodeTimeSeries,
    horizon: int,
    cfg: cfg_module.Config,
    cluster_id: str,
) -> ForecastResult | None:
    times, values = _maybe_filter(node.cpu_times, node.cpu_values, cfg)
    fit = _fit(times, values)
    if not fit:
        return None

    seasonality: SeasonalityResult = detect_seasonality(times, values)
    projected   = _project(fit, horizon)
    capacity    = 100.0  # CPU usage is a percentage
    exhaustion  = _exhaustion_date(fit, capacity)
    severity    = _severity_from_exhaustion(exhaustion)

    return ForecastResult(
        cluster_id=cluster_id,
        node_uuid=node.node_uuid,
        entity_label=f'Node {node.node_peer_id}' if node.node_peer_id else None,
        resource_dimension='cpu',
        forecast_horizon_days=horizon,
        current_usage=node.cpu_mean,
        current_capacity=capacity,
        forecast_value=round(projected, 2),
        confidence_score=_confidence(fit, horizon),
        driving_metric='cpu_usage',
        exhaustion_date=exhaustion,
        severity=severity,
        model_metadata={
            'slope':      fit['slope'],
            'r2':         fit['r2'],
            'p_value':    fit['p_value'],
            'n_samples':  fit['n_samples'],
            'data_span_days': round(fit['data_span_days'], 2),
            'has_daily_pattern':  seasonality.has_daily,
            'has_weekly_pattern': seasonality.has_weekly,
        },
    )


def _forecast_node_ram(
    node: NodeTimeSeries,
    horizon: int,
    cfg: cfg_module.Config,
    cluster_id: str,
) -> ForecastResult | None:
    """
    RAM allocation tracking.  HyperCore does not overcommit RAM, so right-sizing
    is not applicable.  However, physical node RAM is finite: when allocation
    approaches 100% of physical capacity no further VMs can start.  Exhaustion
    is computed against node.mem_capacity when that value is available.
    """
    times, values = _maybe_filter(node.ram_times, node.ram_values, cfg)
    fit = _fit(times, values)
    if not fit:
        return None

    projected = _project(fit, horizon)

    # Use physical node RAM as capacity when collected; fall back to trend-only
    capacity   = node.mem_capacity if node.mem_capacity else None
    exhaustion = _exhaustion_date(fit, capacity) if capacity else None
    severity   = _severity_from_exhaustion(exhaustion)

    return ForecastResult(
        cluster_id=cluster_id,
        node_uuid=node.node_uuid,
        entity_label=f'Node {node.node_peer_id}' if node.node_peer_id else None,
        resource_dimension='ram',
        forecast_horizon_days=horizon,
        current_usage=node.ram_mean,
        current_capacity=capacity,
        forecast_value=round(projected, 0),
        confidence_score=_confidence(fit, horizon),
        driving_metric='mem_usage_bytes',
        exhaustion_date=exhaustion,
        severity=severity,
        model_metadata={
            'slope':          fit['slope'],
            'r2':             fit['r2'],
            'p_value':        fit['p_value'],
            'n_samples':      fit['n_samples'],
            'data_span_days': round(fit['data_span_days'], 2),
            'note': (
                'RAM allocation exhaustion vs. physical node capacity'
                if capacity else
                'RAM allocation trend only — physical capacity unavailable'
            ),
        },
    )


def _forecast_storage_capacity(
    storage: StorageSnapshot,
    horizon: int,
    cluster_id: str,
) -> list[ForecastResult]:
    """
    Build one cluster-wide storage forecast plus one per node.
    Uses the per-node latest used_bytes trends.
    """
    results: list[ForecastResult] = []

    # ── Per-node ─────────────────────────────────────────────────────────────
    # Group drives by node and fit a trend on the sum of used_bytes per node
    from collections import defaultdict
    node_drive_map: dict[str, list] = defaultdict(list)
    for d in storage.drives.values():
        node_drive_map[d.node_uuid].append(d)

    for node_uuid, drives in node_drive_map.items():
        # Build a per-node total used_bytes time series by summing drives at each timestamp
        # (simplified: use the most granular drive's timeline as the base)
        if not drives:
            continue
        # Use the drive with the most samples as the time axis
        ref = max(drives, key=lambda d: len(d.values))
        if len(ref.values) < 3:
            continue

        fit = _fit(ref.times, ref.values)
        if not fit:
            continue

        latest = ref.latest_bytes or 0.0
        projected = _project(fit, horizon)
        # We don't have total capacity from InfluxDB — severity is based on growth rate
        severity = 'WARNING' if fit['slope'] > 0 and fit['r2'] > 0.7 else 'INFO'

        results.append(ForecastResult(
            cluster_id=cluster_id,
            node_uuid=node_uuid,
            entity_label=None,   # filled in post-processing from nodes dict
            resource_dimension='storage_capacity',
            forecast_horizon_days=horizon,
            current_usage=latest,
            current_capacity=None,   # HyperCore capacity not exposed in metrics
            forecast_value=round(projected, 0),
            confidence_score=_confidence(fit, horizon),
            driving_metric='used_bytes',
            exhaustion_date=None,
            severity=severity,
            model_metadata={
                'slope':     fit['slope'],
                'r2':        fit['r2'],
                'p_value':   fit['p_value'],
                'n_samples': fit['n_samples'],
                'data_span_days': round(fit['data_span_days'], 2),
                'drive_uuid': ref.drive_uuid,
            },
        ))

    return results


def _forecast_storage_latency(
    storage: StorageSnapshot,
    horizon: int,
    cluster_id: str,
) -> list[ForecastResult]:
    """One forecast per VSD for latency trend (A4, B1/B2 storage_latency dimension)."""
    results = []
    for vsd in storage.vsds.values():
        if len(vsd.read_latency_us) < 3:
            continue
        fit = _fit(vsd.times, vsd.read_latency_us)
        if not fit:
            continue

        projected   = _project(fit, horizon)
        # Severity: if read latency is trending up significantly, flag it
        severity: SeverityLevel = 'INFO'
        if fit['slope'] > 0 and fit['r2'] > 0.6:
            severity = 'WARNING'

        label = f'{vsd.vm_name} / {vsd.vsd_name}' if vsd.vm_name else vsd.vsd_name
        results.append(ForecastResult(
            cluster_id=cluster_id,
            node_uuid=None,
            entity_label=label,
            resource_dimension='storage_latency',
            forecast_horizon_days=horizon,
            current_usage=vsd.read_lat_mean,
            current_capacity=None,
            forecast_value=round(projected, 1),
            confidence_score=_confidence(fit, horizon),
            driving_metric='read_latency_us',
            exhaustion_date=None,
            severity=severity,
            model_metadata={
                'slope':     fit['slope'],
                'r2':        fit['r2'],
                'p_value':   fit['p_value'],
                'n_samples': fit['n_samples'],
                'data_span_days': round(fit['data_span_days'], 2),
                'vsd_uuid':  vsd.vsd_uuid,
                'vsd_name':  vsd.vsd_name,
                'vm_uuid':   vsd.vm_uuid,
            },
        ))

    return results


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_forecasts(
    metrics_cache: dict,
    cfg: cfg_module.Config,
) -> list[ForecastResult]:
    """
    Run all enabled forecasts over the current metrics cache snapshot.
    Returns a flat list of ForecastResult objects.
    """
    results: list[ForecastResult] = []
    cluster_id = cfg.cluster_id

    nodes:   dict[str, NodeTimeSeries] = metrics_cache.get('nodes',   {})
    vms:     dict[str, VMTimeSeries]   = metrics_cache.get('vms',     {})
    storage: StorageSnapshot | None    = metrics_cache.get('storage')

    horizons = []
    if cfg.forecast_30d_enabled:
        horizons.append(30)
    if cfg.forecast_90d_enabled:
        horizons.append(90)
    if cfg.forecast_180d_enabled:
        horizons.append(180)
    if cfg.forecast_365d_enabled:
        horizons.append(365)

    if not horizons:
        log.info('All forecast horizons disabled')
        return results

    for horizon in horizons:
        # ── CPU per node (A1) ──────────────────────────────────────────────
        for node in nodes.values():
            r = _forecast_node_cpu(node, horizon, cfg, cluster_id)
            if r:
                results.append(r)

        # ── RAM per node (A2) ─────────────────────────────────────────────
        for node in nodes.values():
            r = _forecast_node_ram(node, horizon, cfg, cluster_id)
            if r:
                results.append(r)

        # ── Storage capacity (A3) ─────────────────────────────────────────
        if storage:
            results.extend(_forecast_storage_capacity(storage, horizon, cluster_id))

        # ── Storage latency (A4) ──────────────────────────────────────────
        if storage:
            results.extend(_forecast_storage_latency(storage, horizon, cluster_id))

        # ── Network (A5): VM-level means, node rollup ─────────────────────
        # Forecasting network throughput at the VM level
        for vm in vms.values():
            if len(vm.net_times) < 3:
                continue
            net_times, rx_values = _maybe_filter(vm.net_times, vm.rx_values, cfg)
            rx_fit = _fit(net_times, rx_values)
            if rx_fit:
                projected = _project(rx_fit, horizon)
                results.append(ForecastResult(
                    cluster_id=cluster_id,
                    node_uuid=vm.node_uuid,
                    entity_label=vm.vm_name,
                    resource_dimension='network',
                    forecast_horizon_days=horizon,
                    current_usage=vm.rx_mean,
                    current_capacity=None,
                    forecast_value=round(projected, 0),
                    confidence_score=_confidence(rx_fit, horizon),
                    driving_metric='rx_bit_rate',
                    exhaustion_date=None,
                    severity='INFO',
                    model_metadata={
                        'slope':          rx_fit['slope'],
                        'r2':             rx_fit['r2'],
                        'p_value':        rx_fit['p_value'],
                        'n_samples':      rx_fit['n_samples'],
                        'data_span_days': round(rx_fit['data_span_days'], 2),
                        'vm_uuid':        vm.vm_uuid,
                        'vm_name':        vm.vm_name,
                    },
                ))

    # Back-fill entity_label for storage_capacity rows using the nodes dict
    node_peer_ids = {n.node_uuid: n.node_peer_id for n in nodes.values()}
    for r in results:
        if r.entity_label is None and r.node_uuid:
            peer_id = node_peer_ids.get(r.node_uuid)
            r.entity_label = f'Node {peer_id}' if peer_id else None

    log.info('Forecasts generated', extra={'count': len(results), 'horizons': horizons})
    return results
