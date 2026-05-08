"""
analysis/seasonality.py — Seasonality detection (B8).

Detects daily, weekly, and monthly patterns in a time series using
autocorrelation.  The result feeds the C2 Signal 3 spike-pattern detector
and informs the forecaster's confidence scoring.

Design choices:
- Autocorrelation at target lags (daily=24h, weekly=168h, monthly=720h).
  A peak at a lag means the series repeats at that period.
- Minimum data requirement: 2× the period being tested (e.g. ≥48h of data
  to detect a daily pattern).  Below that threshold the result is None.
- Returns a SeasonalityResult with one score per period and a boolean flag.
  Scores are Pearson r values at the target lag (range −1 to 1).
  A score ≥ 0.4 is treated as a meaningful pattern.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np

log = logging.getLogger(__name__)

# autocorrelation score threshold to declare a pattern present
PATTERN_THRESHOLD = 0.40

# minimum samples required as a multiple of the lag being tested
MIN_SAMPLE_FACTOR = 2


@dataclass
class SeasonalityResult:
    daily_score:   float | None   # autocorrelation at ~24h lag
    weekly_score:  float | None   # autocorrelation at ~168h lag
    monthly_score: float | None   # autocorrelation at ~720h lag

    has_daily:    bool = False
    has_weekly:   bool = False
    has_monthly:  bool = False

    def any_pattern(self) -> bool:
        return self.has_daily or self.has_weekly or self.has_monthly


def _autocorr_at_lag(values: np.ndarray, lag: int) -> float | None:
    """
    Compute Pearson autocorrelation of values at the given sample lag.
    Returns None if there are insufficient samples.
    """
    n = len(values)
    if n < lag * MIN_SAMPLE_FACTOR:
        return None
    x = values[:n - lag]
    y = values[lag:]
    if x.std() == 0 or y.std() == 0:
        return None
    return float(np.corrcoef(x, y)[0, 1])


def _resample_hourly(times: list[datetime], values: list[float]) -> np.ndarray | None:
    """
    Resample an irregular time series to 1-hour buckets using mean aggregation.
    Returns a numpy array of hourly values, or None if there is no data.
    """
    if not times or not values:
        return None

    # normalise to UTC
    t0 = min(times)
    t0 = t0.replace(minute=0, second=0, microsecond=0)
    t_end = max(times)
    total_hours = max(1, int((t_end - t0).total_seconds() / 3600) + 1)

    buckets: list[list[float]] = [[] for _ in range(total_hours)]
    for t, v in zip(times, values):
        idx = int((t - t0).total_seconds() / 3600)
        if 0 <= idx < total_hours:
            buckets[idx].append(v)

    hourly = np.array([
        float(np.mean(b)) if b else np.nan
        for b in buckets
    ])

    # forward-fill NaN gaps (sparse data)
    mask = np.isnan(hourly)
    if mask.all():
        return None
    idx_arr = np.where(~mask, np.arange(len(hourly)), 0)
    np.maximum.accumulate(idx_arr, out=idx_arr)
    hourly[mask] = hourly[idx_arr[mask]]

    return hourly


def detect(times: list[datetime], values: list[float]) -> SeasonalityResult:
    """
    Analyse a time series for daily, weekly, and monthly periodicity.

    Args:
        times:  List of timestamps (timezone-aware or naive).
        values: Corresponding metric values.

    Returns:
        SeasonalityResult with per-period scores and boolean flags.
    """
    hourly = _resample_hourly(times, values)
    if hourly is None or len(hourly) < 24:
        return SeasonalityResult(daily_score=None, weekly_score=None, monthly_score=None)

    # standardise to zero-mean unit-variance for cleaner autocorrelation
    std = hourly.std()
    if std > 0:
        hourly = (hourly - hourly.mean()) / std

    daily_lag   = 24
    weekly_lag  = 168
    monthly_lag = 720

    d = _autocorr_at_lag(hourly, daily_lag)
    w = _autocorr_at_lag(hourly, weekly_lag)
    m = _autocorr_at_lag(hourly, monthly_lag)

    result = SeasonalityResult(
        daily_score=d,
        weekly_score=w,
        monthly_score=m,
        has_daily=   d is not None and d >= PATTERN_THRESHOLD,
        has_weekly=  w is not None and w >= PATTERN_THRESHOLD,
        has_monthly= m is not None and m >= PATTERN_THRESHOLD,
    )

    if result.any_pattern():
        log.debug(
            'Seasonality detected',
            extra={
                'daily':   round(d, 3) if d is not None else None,
                'weekly':  round(w, 3) if w is not None else None,
                'monthly': round(m, 3) if m is not None else None,
            },
        )

    return result
