"""
config.py — Typed configuration from environment variables.

All Sentinel tunables are read here once at startup.  Every field has a
sensible default so the container runs in a dev environment with minimal .env
configuration.  In production, override via docker-compose environment block.
"""

import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ('true', '1', 'yes')


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(float(raw)) if raw else default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw else default


def _str(name: str, default: str) -> str:
    return os.getenv(name, default)


@dataclass(frozen=True)
class Config:
    # ── InfluxDB ─────────────────────────────────────────────────────────────
    influx_url:    str = field(default_factory=lambda: _str('INFLUX_URL',    'http://influxdb:8086'))
    influx_token:  str = field(default_factory=lambda: _str('INFLUX_TOKEN',  ''))
    influx_org:    str = field(default_factory=lambda: _str('INFLUX_ORG',    'hypercore'))
    influx_bucket: str = field(default_factory=lambda: _str('INFLUX_BUCKET', 'metrics'))

    # ── PostgreSQL ───────────────────────────────────────────────────────────
    pg_host:     str = field(default_factory=lambda: _str('SENTINEL_PG_HOST', 'postgres'))
    pg_port:     int = field(default_factory=lambda: _int('SENTINEL_PG_PORT', 5432))
    pg_db:       str = field(default_factory=lambda: _str('SENTINEL_PG_DB',   'hypercore_balancer'))
    pg_user:     str = field(default_factory=lambda: _str('SENTINEL_PG_USER', 'sentinel'))
    pg_password: str = field(default_factory=lambda: _str('SENTINEL_PG_PASSWORD', ''))

    # ── cluster identity (phase 1: single cluster) ───────────────────────────
    cluster_id: str = field(default_factory=lambda: _str(
        'SC_CLUSTER_ID', '00000000-0000-0000-0000-000000000001'
    ))

    # ── collection intervals ─────────────────────────────────────────────────
    # how often sentinel pulls recent metrics from InfluxDB (A7 real-time tier)
    collection_interval_seconds: int = field(default_factory=lambda: _int('SENTINEL_COLLECTION_INTERVAL_SECONDS', 300))
    # how often sentinel runs trend/forecast analysis (A7 trend tier)
    trend_interval_seconds: int = field(default_factory=lambda: _int('SENTINEL_TREND_INTERVAL_SECONDS', 3600))

    # ── forecast toggles (B1, B2, B3, B4) ──────────────────────────────────
    forecast_30d_enabled:  bool = field(default_factory=lambda: _bool('SENTINEL_FORECAST_30D_ENABLED',  True))
    forecast_90d_enabled:  bool = field(default_factory=lambda: _bool('SENTINEL_FORECAST_90D_ENABLED',  True))
    # B3: 6-month strategic forecast
    forecast_180d_enabled: bool = field(default_factory=lambda: _bool('SENTINEL_FORECAST_180D_ENABLED', True))
    # B4: 12-month strategic forecast
    forecast_365d_enabled: bool = field(default_factory=lambda: _bool('SENTINEL_FORECAST_365D_ENABLED', True))

    # ── anomaly filter (B9) ──────────────────────────────────────────────────
    # when enabled, IQR-fence outlier removal is applied to each time series
    # before fitting the linear regression model
    anomaly_filter_enabled: bool = field(default_factory=lambda: _bool('SENTINEL_ANOMALY_FILTER_ENABLED', True))

    # ── forecast statistical gate ────────────────────────────────────────────
    # minimum r² required before a positive slope is allowed to produce an
    # exhaustion date.  a linear fit with r² below this threshold is mostly
    # noise — extrapolating it forward yields exhaustion dates that wobble
    # cycle-to-cycle and don't reflect a real trend.  default 0.5 (the linear
    # model must explain ≥50% of variance).  set to 0.0 to disable the gate
    forecast_min_r2_for_exhaustion: float = field(
        default_factory=lambda: _float('SENTINEL_FORECAST_MIN_R2_FOR_EXHAUSTION', 0.5)
    )

    # ── cluster forecast daily aggregation ───────────────────────────────────
    # how sub-day samples roll up into the daily data point used by cluster
    # CPU / RAM / storage forecasts
    #   mean  — time-weighted average across the day.  best for steady
    #           workloads; understates peak in spiky / VDI environments
    #   p95   — 95th-percentile sample.  captures sustained peak (busiest
    #           ~1.2 hours of the day) without being fooled by single-spike
    #           outliers.  recommended for VDI and other non-persistent
    #           workloads where capacity must absorb the daytime peak
    #   max   — single highest sample of the day.  most sensitive to brief
    #           spikes from backup jobs, deploy/teardown, etc.  use only when
    #           you specifically want worst-case visibility
    forecast_daily_aggregate: str = field(
        default_factory=lambda: _str('SENTINEL_FORECAST_DAILY_AGGREGATE', 'mean')
    )

    # ── right-sizing thresholds ──────────────────────────────────────────────
    # C1: flag VMs using less than this % of allocated CPU
    cpu_over_provisioned_threshold: float = field(
        default_factory=lambda: _float('SENTINEL_CPU_OVER_PROVISIONED_THRESHOLD', 20.0)
    )
    # C2 signal 1: sustained under-provisioning
    cpu_under_provisioned_avg_threshold: float = field(
        default_factory=lambda: _float('SENTINEL_CPU_UNDER_PROVISIONED_AVG_THRESHOLD', 80.0)
    )
    cpu_under_provisioned_p95_threshold: float = field(
        default_factory=lambda: _float('SENTINEL_CPU_UNDER_PROVISIONED_P95_THRESHOLD', 90.0)
    )

    # ── analysis window ──────────────────────────────────────────────────────
    analysis_window_days: int = field(default_factory=lambda: _int('SENTINEL_ANALYSIS_WINDOW_DAYS', 14))
    # C2 signal 3: minimum spike recurrences before flagging as a pattern
    recurring_spike_min_occurrences: int = field(
        default_factory=lambda: _int('SENTINEL_RECURRING_SPIKE_MIN_OCCURRENCES', 3)
    )

    # ── data retention ───────────────────────────────────────────────────────
    retention_months: int = field(default_factory=lambda: _int('SENTINEL_RETENTION_MONTHS', 12))

    # ── health endpoint ──────────────────────────────────────────────────────
    health_port: int = field(default_factory=lambda: _int('SENTINEL_HEALTH_PORT', 8080))

    # ── REST API (F1-F4, F6) ─────────────────────────────────────────────────
    # port the sentinel REST API listens on (separate from the health endpoint)
    api_port: int = field(default_factory=lambda: _int('SENTINEL_API_PORT', 8081))
    # bearer token for API authentication.  if empty, auth is disabled (dev mode)
    api_token: str = field(default_factory=lambda: _str('SENTINEL_API_TOKEN', ''))

    # ── webhook/alert push (F5) ──────────────────────────────────────────────
    # target URL for outbound webhook POST requests.  empty = webhooks disabled
    webhook_url: str = field(default_factory=lambda: _str('SENTINEL_WEBHOOK_URL', ''))
    # HMAC-SHA256 signing secret.  empty = requests are unsigned (not recommended)
    webhook_secret: str = field(default_factory=lambda: _str('SENTINEL_WEBHOOK_SECRET', ''))
    # master enable/disable switch
    webhook_enabled: bool = field(
        default_factory=lambda: _bool('SENTINEL_WEBHOOK_ENABLED', True)
    )
    # HTTP timeout for each webhook attempt (seconds)
    webhook_timeout_seconds: int = field(
        default_factory=lambda: _int('SENTINEL_WEBHOOK_TIMEOUT_SECONDS', 10)
    )
    # maximum delivery attempts before marking as dead_letter
    webhook_max_retries: int = field(
        default_factory=lambda: _int('SENTINEL_WEBHOOK_MAX_RETRIES', 3)
    )
    # how far back to look for undelivered CRITICAL/EMERGENCY events on startup
    # (hours).  prevents replaying old events after a long outage
    webhook_lookback_hours: int = field(
        default_factory=lambda: _int('SENTINEL_WEBHOOK_LOOKBACK_HOURS', 24)
    )
    # how often the webhook dispatch loop runs (seconds)
    webhook_dispatch_interval_seconds: int = field(
        default_factory=lambda: _int('SENTINEL_WEBHOOK_DISPATCH_INTERVAL_SECONDS', 60)
    )

    # ── HyperCore API (C7 outcome tracking) ──────────────────────────────────
    # filled from the cluster registry by load(); nothing comes from the environment
    hc_host:       str  = ''      # REST base URL, e.g. https://10.0.0.1/rest/v1
    hc_username:   str  = ''
    hc_password:   str  = ''
    hc_verify_ssl: bool = False
    cluster_name:  str  = ''

    # ── outcome tracker (C7) ─────────────────────────────────────────────────
    # enable/disable outcome tracking entirely
    outcome_tracker_enabled: bool = field(
        default_factory=lambda: _bool('SENTINEL_OUTCOME_TRACKER_ENABLED', True)
    )
    # how long to wait after a recommendation is emitted before checking whether
    # the VM was resized.  default: 48 hours — enough time for an operator to act
    outcome_check_delay_hours: int = field(
        default_factory=lambda: _int('SENTINEL_OUTCOME_CHECK_DELAY_HOURS', 48)
    )
    # how often the outcome tracker loop runs (seconds).  default: 6 hours
    outcome_check_interval_seconds: int = field(
        default_factory=lambda: _int('SENTINEL_OUTCOME_CHECK_INTERVAL_SECONDS', 21600)
    )

    # ── VM staleness pruning ─────────────────────────────────────────────────
    # how often sentinel scans for and removes stale VM records (seconds)
    vm_prune_interval_seconds: int = field(
        default_factory=lambda: _int('SENTINEL_VM_PRUNE_INTERVAL_SECONDS', 900)
    )


def load(cluster=None) -> Config:
    """Return a Config instance populated from the environment and the cluster registry.

    *cluster* is a cluster_registry.Cluster; when omitted the first enabled cluster is
    used. With no cluster registered yet the HyperCore fields stay empty and
    cluster_id stays at the defaults template (nothing is analysed until a cluster
    exists — sentinel.py waits for one).
    """
    cfg = Config()
    if cluster is None:
        try:
            import cluster_registry
            enabled = cluster_registry.list_enabled()
            cluster = enabled[0] if enabled else None
        except Exception:
            cluster = None
    if cluster is not None:
        import dataclasses
        import cluster_registry
        try:
            username, password = cluster_registry.credentials(cluster.cluster_id)
        except Exception:
            username, password = cluster.username, ''
        # Config is a frozen dataclass: derive a new instance instead of assigning
        cfg = dataclasses.replace(
            cfg,
            cluster_id=cluster.cluster_id,
            cluster_name=cluster.name,
            hc_host=cluster.base_url,
            hc_verify_ssl=bool(cluster.verify_tls),
            hc_username=username,
            hc_password=password,
        )
    return cfg
