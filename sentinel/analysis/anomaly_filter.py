"""
analysis/anomaly_filter.py — B9: Anomaly filtering for forecast inputs.

Removes statistical outliers from a time series before fitting linear trend
models.  Uses the Tukey IQR fence method: values outside the interval
[Q1 − factor·IQR, Q3 + factor·IQR] are excluded before the regression is run.

The filter is conservative:
- It requires at least 6 data points before filtering (too few → no action).
- If more than max_removal_fraction (default 20 %) of the series would be
  removed, the originals are returned unchanged — a series dominated by
  "outliers" is probably just a high-variance series, not a corrupted one.
- If IQR is zero (flat series), filtering is skipped.

Usage:
    from analysis.anomaly_filter import filter_series

    times_clean, values_clean = filter_series(times, values)
"""

from __future__ import annotations

import logging
from datetime import datetime

import numpy as np

log = logging.getLogger(__name__)

_DEFAULT_IQR_FACTOR    = 1.5
_MIN_SAMPLES           = 6
_MAX_REMOVAL_FRACTION  = 0.20


def filter_series(
    times:      list[datetime],
    values:     list[float],
    iqr_factor: float = _DEFAULT_IQR_FACTOR,
) -> tuple[list[datetime], list[float]]:
    """
    Remove IQR-fence outliers from a (times, values) time series.

    Parameters
    ----------
    times:      Parallel list of datetime objects (must be same length as values).
    values:     Numeric observations corresponding to each timestamp.
    iqr_factor: Tukey fence multiplier (default 1.5 = standard outer fence).

    Returns
    -------
    (filtered_times, filtered_values) — both lists with outlier pairs removed.
    Returns the originals unchanged if filtering is skipped.
    """
    n = len(values)
    if n < _MIN_SAMPLES:
        return times, values

    arr = np.array(values, dtype=float)
    q1, q3 = float(np.percentile(arr, 25)), float(np.percentile(arr, 75))
    iqr = q3 - q1

    if iqr == 0.0:
        # all values identical or constant band — nothing meaningful to filter
        return times, values

    lo = q1 - iqr_factor * iqr
    hi = q3 + iqr_factor * iqr

    mask       = (arr >= lo) & (arr <= hi)
    n_removed  = int(np.sum(~mask))

    if n_removed == 0:
        return times, values

    if n_removed / n > _MAX_REMOVAL_FRACTION:
        log.debug(
            'Anomaly filter: %d/%d points outside fence — '
            'removal fraction %.1f%% exceeds limit; skipping',
            n_removed, n, n_removed / n * 100,
        )
        return times, values

    filtered_times  = [t for t, keep in zip(times,  mask) if keep]
    filtered_values = [v for v, keep in zip(values, mask) if keep]

    log.debug(
        'Anomaly filter: removed %d outliers from series of %d '
        '(fence [%.2f, %.2f])',
        n_removed, n, lo, hi,
    )
    return filtered_times, filtered_values
