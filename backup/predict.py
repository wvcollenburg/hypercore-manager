"""
predict.py — how long a VM's backup will take, learned from its own runs.

A backup is one sample per VM per night, and its length is set by how much changed, not by the
shape of a time series, so this is a small model rather than the balancer's forecasting:

    seconds = overhead(VM) + MiB to copy(VM, kind, weekday) / read rate(cluster)

- MiB to copy: a delta takes the 80th percentile of the VM's recent deltas on the same weekday
  (the Monday after a weekend and patch days differ), falling back to all weekdays; a full takes the
  size of the VM's last full.
- read rate: median MiB/s of the cluster's recent copies, shared by its VMs, so it is known after a
  few runs of any VM.
- overhead: median of snapshot, clones, maps, cleanup and retention.
- a synthetic full night adds the time the last synthetic fulls took, or a full's size at the read
  rate when none ran yet.
Exports have no proxy: their active time (copy + overhead) is learned per VM directly.
A VM is learned once it has LEARN_MIN_RUNS timed runs of the kind that matters. Pure functions: the
history comes in as run rows.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

LEARN_MIN_RUNS = 5          # timed runs before a VM's duration is trusted
PERCENTILE = 0.8            # plan on a high percentile: the deadline matters, not the average
SAME_WEEKDAY_MIN = 3        # same-weekday deltas needed before the weekday pattern is used
RECENT_DELTAS = 8
RECENT_RATE_RUNS = 20


def percentile(values: list[float], q: float) -> float | None:
    vals = sorted(float(v) for v in values if v is not None)
    if not vals:
        return None
    k = (len(vals) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def median(values: list[float]) -> float | None:
    return percentile(values, 0.5)


def _weekday(run: dict, tz: ZoneInfo) -> int | None:
    at = run.get('work_started_at') or run.get('started_at')
    if not isinstance(at, datetime):
        return None
    return (at if at.tzinfo else at.replace(tzinfo=timezone.utc)).astimezone(tz).weekday()


def _timed(runs: list[dict]) -> list[dict]:
    """Completed runs that carry the timing columns (migration 035), newest first as given."""
    return [r for r in runs if r.get('status') == 'COMPLETE' and r.get('copy_seconds') is not None]


def cluster_rate(runs: list[dict]) -> float | None:
    """MiB per second the cluster's proxies read, from runs that copied something measurable."""
    rates = [float(r['blocks_changed']) / float(r['copy_seconds']) for r in _timed(runs)
             if (r.get('mode') or 'incremental') == 'incremental' and (r.get('blocks_changed') or 0) > 0
             and float(r['copy_seconds']) >= 1.0][:RECENT_RATE_RUNS]
    return median(rates)


def predict(runs: list[dict], kind: str, mode: str, weekday: int | None, rate: float | None,
            tz: str = 'UTC') -> dict:
    """Prediction for one VM. runs: its own run rows, newest first. kind: 'delta', 'full' or
    'synthetic' (a delta followed by a synthetic full). Returns {learned, seconds, samples, needed}.
    seconds is None when there is nothing to go on."""
    zone = ZoneInfo(tz or 'UTC')
    timed = _timed(runs)
    if mode == 'export':
        active = [float(r['copy_seconds']) + float(r.get('overhead_seconds') or 0) for r in timed]
        sec = percentile(active[:RECENT_DELTAS], PERCENTILE)
        return {'learned': len(active) >= LEARN_MIN_RUNS, 'seconds': sec, 'samples': len(active), 'needed': LEARN_MIN_RUNS}
    deltas = [r for r in timed if r.get('point_kind') == 'delta']
    fulls = [r for r in timed if r.get('point_kind') == 'full']
    overhead = median([r.get('overhead_seconds') for r in timed[:RECENT_DELTAS]])
    out = {'learned': False, 'seconds': None, 'samples': len(deltas), 'needed': LEARN_MIN_RUNS}
    if rate is None or rate <= 0:
        return out
    if kind == 'full':
        if not fulls:
            return dict(out, samples=0, needed=1)
        mib = float(fulls[0].get('blocks_changed') or 0)
        out.update(learned=True, samples=len(fulls), needed=1)
    else:
        same = [r for r in deltas if _weekday(r, zone) == weekday] if weekday is not None else []
        pool = same if len(same) >= SAME_WEEKDAY_MIN else deltas
        mib = percentile([float(r.get('blocks_changed') or 0) for r in pool[:RECENT_DELTAS]], PERCENTILE)
        if mib is None:
            return out
        out['learned'] = len(deltas) >= LEARN_MIN_RUNS
    seconds = (overhead or 0.0) + mib / rate
    if kind == 'synthetic':
        synths = [float(r['synth_seconds']) for r in timed if r.get('synth_seconds')]
        if synths:
            seconds += percentile(synths, PERCENTILE)
        elif fulls:
            seconds += float(fulls[0].get('blocks_changed') or 0) / rate
        else:
            out['learned'] = False
    out['seconds'] = seconds
    return out
