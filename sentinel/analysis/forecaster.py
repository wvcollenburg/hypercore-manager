"""
analysis/forecaster.py — Forecasting engine (B1, B2, B3, B4, B5, B6, B7, B9).

For each resource dimension and each enabled horizon (30d, 90d, 180d, 365d),
fits a linear trend to the collected time series and produces a ForecastResult.

Capacity exhaustion forecasts for CPU, RAM, and storage are computed
cluster-wide.  VM live-migration shifts load between nodes constantly, so a
per-node series is non-stationary with respect to placement and produces
misleading exhaustion dates.  The cluster-wide aggregate is invariant under
migration and reflects real workload growth.

Per-node CPU/RAM forecasts are still emitted but as informational signals only
(severity always INFO, no exhaustion date).  They remain useful for spotting
imbalance — a node trending hotter than the cluster — but are not the leading
capacity signal.

Cluster aggregates are bucketed to daily means before fitting.  Test VMs that
exist for only a few hours, nightly tear-down/reset cycles, and other
intraday churn would otherwise dominate the regression on a short window
(14d, 30d) — each spike's leverage is amplified when it sits at the right
edge of the data and the rest of the window is flat.  A daily mean per node
collapses sub-day churn to a single value, so a VM that only ever existed
for one day contributes only as much weight as any other day, and a real
trend across days is what the slope captures.

N+1 capacity:
    For CPU and RAM the cluster forecast uses N+1 capacity — the cluster's
    capacity minus the largest single node.  That gives headroom to survive
    one node loss AND to maintain full functionality during a rolling upgrade.
    Storage redundancy is regulated separately (SCRIBE replication factor)
    so storage forecasts use raw cluster totals.

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


def _exhaustion_date(fit: dict, capacity: float, min_r2: float = 0.0) -> date | None:
    """
    Calculate when the linear trend crosses capacity.
    Returns None if the trend is flat/declining, never reaches capacity, or
    the fit's r² is below `min_r2` (linear model isn't a meaningful descriptor
    of the data — extrapolating it would amplify noise into spurious dates).
    """
    if fit['slope'] <= 0 or capacity is None:
        return None
    if fit['r2'] < min_r2:
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
# Cluster-wide series aggregation
# ---------------------------------------------------------------------------

def _aggregate_series(
    series: list[tuple[list[datetime], list[float]]],
    agg:        Literal['sum', 'mean'],
    bucket:     Literal['minute', 'day']        = 'day',
    bucket_agg: Literal['mean', 'p95', 'max']   = 'mean',
) -> tuple[list[datetime], list[float]]:
    """
    Combine per-entity time series into a single cluster-wide series.

    Samples are bucketed by `bucket` (default 'day').  Daily bucketing
    collapses intraday spikes from short-lived test VMs, nightly resets, and
    other sub-day churn into a single value per entity per day, so the
    cluster fit reflects real cross-day trends rather than placement / test
    noise.  A 14-day window therefore produces ~14 cluster data points
    instead of thousands of minute-level samples — fewer points but each one
    represents a real day of cluster behaviour.

    `bucket_agg` controls how samples within a bucket roll up per entity:
        'mean' — time-weighted average; suits steady workloads.
        'p95'  — 95th percentile; captures sustained peak (e.g. VDI
                 daytime occupancy) without being skewed by transient spikes.
        'max'  — single highest sample; worst-case visibility.

    Across entities at each bucket, values are aggregated by `agg`.  Missing
    entity values are forward-filled from that entity's last observed bucket
    so a transient sample gap on one node does not produce an artificial
    drop in the cluster aggregate.  Buckets before every entity has been
    observed at least once are dropped.
    """
    if not series:
        return [], []

    def _truncate(t: datetime) -> datetime:
        if bucket == 'day':
            return t.replace(hour=0, minute=0, second=0, microsecond=0)
        return t.replace(second=0, microsecond=0)

    def _rollup(values_in_bucket: list[float]) -> float:
        if bucket_agg == 'max':
            return max(values_in_bucket)
        if bucket_agg == 'p95':
            return float(np.percentile(values_in_bucket, 95))
        return sum(values_in_bucket) / len(values_in_bucket)

    entity_buckets: list[dict[datetime, float]] = []
    all_buckets: set[datetime] = set()

    for times, values in series:
        per_bucket: dict[datetime, list[float]] = {}
        for t, v in zip(times, values):
            b = _truncate(t)
            per_bucket.setdefault(b, []).append(v)
        rolled = {b: _rollup(vs) for b, vs in per_bucket.items()}
        entity_buckets.append(rolled)
        all_buckets.update(rolled.keys())

    if not all_buckets:
        return [], []

    out_times:  list[datetime] = []
    out_values: list[float]    = []
    last: list[float | None]   = [None] * len(entity_buckets)

    for b in sorted(all_buckets):
        for i, eb in enumerate(entity_buckets):
            if b in eb:
                last[i] = eb[b]
        if any(v is None for v in last):
            continue
        contrib = [v for v in last if v is not None]
        out_values.append(sum(contrib) if agg == 'sum' else sum(contrib) / len(contrib))
        out_times.append(b)

    return out_times, out_values


# ---------------------------------------------------------------------------
# Cluster-wide forecast builders (leading capacity signal)
# ---------------------------------------------------------------------------

def _forecast_cluster_ram(
    nodes: dict[str, NodeTimeSeries],
    horizon: int,
    cfg: cfg_module.Config,
    cluster_id: str,
) -> ForecastResult | None:
    """
    Cluster-wide RAM allocation forecast.

    Sums ram_values across nodes per timestamp bucket — invariant under VM
    live-migration, unlike a per-node series.  Exhaustion is computed against
    N+1 capacity (sum of node mem_capacity minus the largest node) so the
    forecast preserves headroom for one node loss and rolling upgrades.
    """
    if not nodes:
        return None
    series = [(n.ram_times, n.ram_values) for n in nodes.values()]
    times, values = _aggregate_series(series, agg='sum', bucket_agg=cfg.forecast_daily_aggregate)
    times, values = _maybe_filter(times, values, cfg)
    fit = _fit(times, values)
    if not fit:
        return None

    capacities = [n.mem_capacity for n in nodes.values() if n.mem_capacity]
    capacity: float | None
    if not capacities:
        capacity = None
        capacity_note = 'Cluster RAM trend — physical capacity unavailable'
    elif len(capacities) == 1:
        capacity = float(sum(capacities))
        capacity_note = 'Single-node cluster — full capacity (no N+1 headroom)'
    else:
        total   = sum(capacities)
        largest = max(capacities)
        capacity = float(total - largest)
        capacity_note = (
            f'N+1 capacity = total ({total}) − largest node ({largest}); '
            f'survives one node loss and rolling upgrades'
        )

    projected   = _project(fit, horizon)
    exhaustion  = (
        _exhaustion_date(fit, capacity, min_r2=cfg.forecast_min_r2_for_exhaustion)
        if capacity else None
    )
    severity    = _severity_from_exhaustion(exhaustion)
    current     = sum((n.ram_mean or 0.0) for n in nodes.values())

    return ForecastResult(
        cluster_id=cluster_id,
        node_uuid=None,
        entity_label='Cluster',
        resource_dimension='ram',
        forecast_horizon_days=horizon,
        current_usage=current,
        current_capacity=capacity,
        forecast_value=round(projected, 0),
        confidence_score=_confidence(fit, horizon),
        driving_metric='cluster_mem_usage_bytes',
        exhaustion_date=exhaustion,
        severity=severity,
        model_metadata={
            'slope':            fit['slope'],
            'r2':               fit['r2'],
            'p_value':          fit['p_value'],
            'n_samples':        fit['n_samples'],
            'data_span_days':   round(fit['data_span_days'], 2),
            'node_count':       len(nodes),
            'capacity_model':   'n_plus_1' if capacities and len(capacities) > 1 else 'raw',
            'daily_aggregate':  cfg.forecast_daily_aggregate,
            'note':             capacity_note,
        },
    )


def _forecast_cluster_cpu(
    nodes: dict[str, NodeTimeSeries],
    horizon: int,
    cfg: cfg_module.Config,
    cluster_id: str,
) -> ForecastResult | None:
    """
    Cluster-wide CPU usage forecast.

    Mean of per-node cpu_usage % per timestamp bucket — invariant under VM
    live-migration as a measure of total cluster CPU pressure.  Capacity is
    set to (N-1)/N · 100% so the exhaustion crossover marks the point at
    which the cluster can no longer absorb the loss of a single node or
    sustain a rolling upgrade at current load.

    Equal weighting assumes nodes have comparable core counts (typical for a
    HyperCore cluster — homogeneous hardware per cluster); skew on
    heterogeneous clusters will be small relative to the migration noise this
    aggregation eliminates.
    """
    if not nodes:
        return None
    series = [(n.cpu_times, n.cpu_values) for n in nodes.values()]
    times, values = _aggregate_series(series, agg='mean', bucket_agg=cfg.forecast_daily_aggregate)
    times, values = _maybe_filter(times, values, cfg)
    fit = _fit(times, values)
    if not fit:
        return None

    n = len(nodes)
    if n == 1:
        capacity = 100.0
        capacity_note = 'Single-node cluster — 100% (no N+1 headroom)'
    else:
        capacity = (n - 1) / n * 100.0
        capacity_note = (
            f'N+1 capacity = {n - 1}/{n} · 100% = {capacity:.1f}% — preserves '
            f'headroom for one node loss / rolling upgrade'
        )

    projected  = _project(fit, horizon)
    exhaustion = _exhaustion_date(fit, capacity, min_r2=cfg.forecast_min_r2_for_exhaustion)
    severity   = _severity_from_exhaustion(exhaustion)
    means      = [node.cpu_mean for node in nodes.values() if node.cpu_mean is not None]
    current    = float(sum(means) / len(means)) if means else None

    return ForecastResult(
        cluster_id=cluster_id,
        node_uuid=None,
        entity_label='Cluster',
        resource_dimension='cpu',
        forecast_horizon_days=horizon,
        current_usage=current,
        current_capacity=capacity,
        forecast_value=round(projected, 2),
        confidence_score=_confidence(fit, horizon),
        driving_metric='cluster_cpu_usage_pct',
        exhaustion_date=exhaustion,
        severity=severity,
        model_metadata={
            'slope':            fit['slope'],
            'r2':               fit['r2'],
            'p_value':          fit['p_value'],
            'n_samples':        fit['n_samples'],
            'data_span_days':   round(fit['data_span_days'], 2),
            'node_count':       n,
            'capacity_model':   'n_plus_1' if n > 1 else 'raw',
            'daily_aggregate':  cfg.forecast_daily_aggregate,
            'note':             capacity_note,
        },
    )


# ---------------------------------------------------------------------------
# Per-dimension forecast builders
# ---------------------------------------------------------------------------

def _forecast_node_cpu(
    node: NodeTimeSeries,
    horizon: int,
    cfg: cfg_module.Config,
    cluster_id: str,
) -> ForecastResult | None:
    """
    Per-node CPU trend — informational only.

    VM live-migration moves CPU load between nodes constantly, so a per-node
    series is not a reliable capacity signal.  This forecast is emitted as an
    imbalance indicator (compare against the cluster-wide forecast) with
    severity forced to INFO and no exhaustion date.
    """
    times, values = _maybe_filter(node.cpu_times, node.cpu_values, cfg)
    fit = _fit(times, values)
    if not fit:
        return None

    seasonality: SeasonalityResult = detect_seasonality(times, values)
    projected   = _project(fit, horizon)

    return ForecastResult(
        cluster_id=cluster_id,
        node_uuid=node.node_uuid,
        entity_label=f'Node {node.node_peer_id}' if node.node_peer_id else None,
        resource_dimension='cpu',
        forecast_horizon_days=horizon,
        current_usage=node.cpu_mean,
        current_capacity=100.0,
        forecast_value=round(projected, 2),
        confidence_score=_confidence(fit, horizon),
        driving_metric='cpu_usage',
        exhaustion_date=None,
        severity='INFO',
        model_metadata={
            'slope':      fit['slope'],
            'r2':         fit['r2'],
            'p_value':    fit['p_value'],
            'n_samples':  fit['n_samples'],
            'data_span_days': round(fit['data_span_days'], 2),
            'has_daily_pattern':  seasonality.has_daily,
            'has_weekly_pattern': seasonality.has_weekly,
            'scope':      'node_imbalance_signal',
            'note':       'Per-node trend — informational only (cluster-wide is the leading capacity signal)',
        },
    )


def _forecast_node_ram(
    node: NodeTimeSeries,
    horizon: int,
    cfg: cfg_module.Config,
    cluster_id: str,
) -> ForecastResult | None:
    """
    Per-node RAM allocation trend — informational only.

    VM live-migration moves RAM allocation between nodes constantly, so a
    per-node series produces misleading exhaustion dates.  This forecast is
    emitted as an imbalance indicator with severity forced to INFO and no
    exhaustion date.  Cluster-wide RAM is the leading capacity signal.
    """
    times, values = _maybe_filter(node.ram_times, node.ram_values, cfg)
    fit = _fit(times, values)
    if not fit:
        return None

    projected = _project(fit, horizon)
    capacity  = node.mem_capacity if node.mem_capacity else None

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
        exhaustion_date=None,
        severity='INFO',
        model_metadata={
            'slope':          fit['slope'],
            'r2':             fit['r2'],
            'p_value':        fit['p_value'],
            'n_samples':      fit['n_samples'],
            'data_span_days': round(fit['data_span_days'], 2),
            'scope':          'node_imbalance_signal',
            'note':           'Per-node trend — informational only (cluster-wide is the leading capacity signal)',
        },
    )


def _forecast_cluster_storage(
    storage: StorageSnapshot,
    horizon: int,
    cfg: cfg_module.Config,
    cluster_id: str,
) -> ForecastResult | None:
    """
    Cluster-wide storage capacity forecast.

    Sums used_bytes across all drives per timestamp bucket.  SCRIBE
    distributes blocks reasonably evenly across the cluster, so a per-node
    storage trend is mostly placement noise — the cluster aggregate is the
    meaningful signal.  Storage redundancy is regulated by SCRIBE replication
    factor independently of N+1, so capacity here is the raw sum (not
    available from current metrics, so severity is slope-based).
    """
    drives = list(storage.drives.values())
    if not drives:
        return None
    series = [(d.times, d.values) for d in drives]
    times, values = _aggregate_series(series, agg='sum', bucket_agg=cfg.forecast_daily_aggregate)
    times, values = _maybe_filter(times, values, cfg)
    fit = _fit(times, values)
    if not fit:
        return None

    projected = _project(fit, horizon)
    severity: SeverityLevel = 'WARNING' if fit['slope'] > 0 and fit['r2'] > 0.7 else 'INFO'
    latest = sum((d.latest_bytes or 0.0) for d in drives)

    return ForecastResult(
        cluster_id=cluster_id,
        node_uuid=None,
        entity_label='Cluster',
        resource_dimension='storage_capacity',
        forecast_horizon_days=horizon,
        current_usage=latest,
        current_capacity=None,
        forecast_value=round(projected, 0),
        confidence_score=_confidence(fit, horizon),
        driving_metric='cluster_used_bytes',
        exhaustion_date=None,
        severity=severity,
        model_metadata={
            'slope':            fit['slope'],
            'r2':               fit['r2'],
            'p_value':          fit['p_value'],
            'n_samples':        fit['n_samples'],
            'data_span_days':   round(fit['data_span_days'], 2),
            'drive_count':      len(drives),
            'daily_aggregate':  cfg.forecast_daily_aggregate,
            'note':             'Cluster-wide storage trend (capacity not exposed in metrics — slope-based severity)',
        },
    )


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
        # ── Cluster-wide CPU (A1, leading capacity signal) ─────────────────
        if nodes:
            r = _forecast_cluster_cpu(nodes, horizon, cfg, cluster_id)
            if r:
                results.append(r)

        # ── Cluster-wide RAM (A2, leading capacity signal) ─────────────────
        if nodes:
            r = _forecast_cluster_ram(nodes, horizon, cfg, cluster_id)
            if r:
                results.append(r)

        # ── Per-node CPU (informational — imbalance signal only) ──────────
        for node in nodes.values():
            r = _forecast_node_cpu(node, horizon, cfg, cluster_id)
            if r:
                results.append(r)

        # ── Per-node RAM (informational — imbalance signal only) ──────────
        for node in nodes.values():
            r = _forecast_node_ram(node, horizon, cfg, cluster_id)
            if r:
                results.append(r)

        # ── Cluster-wide storage capacity (A3) ────────────────────────────
        if storage:
            r = _forecast_cluster_storage(storage, horizon, cfg, cluster_id)
            if r:
                results.append(r)

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
