"""
legacy_metrics.py — adopt metrics written by 0.9.x (no cluster_id tag) into one registered cluster.

Every view filters on the cluster_id tag the collector writes since the multi-cluster release;
points from before carry no tag and are invisible, so Foresight and the balancer start from zero
after an in-place upgrade. A 0.9.x installation had exactly one cluster, so its untagged points
belong to the cluster registered first. InfluxDB tags are immutable and a delete predicate
cannot say "where the tag is absent", hence the three passes per measurement and bucket:

  1. copy the untagged range re-tagged into a scratch bucket (Flux set + group + to, in windows)
  2. delete that measurement's range from the bucket (everything before its first tagged point)
  3. copy the scratch data back, then clear the scratch

Idempotent: a second run finds no untagged points. A run interrupted between steps 2 and 3
leaves the data in the scratch bucket; the next run restores it first. The InfluxDB access is
behind a small adapter so the orchestration is unit-testable with a fake.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

log = logging.getLogger('hcm.legacy_metrics')

SCRATCH_BUCKET = 'hcm_adopt_scratch'
SYSTEM_COLUMNS = ('_start', '_stop', '_time', '_value', '_measurement', '_field', 'result', 'table', 'cluster_id')


# ------------------------------------------------------------------ Flux
def untagged_summary_query(bucket: str, stop: Optional[datetime] = None, start: Optional[datetime] = None) -> str:
    """Untagged points per measurement with count, first and last time, within [start, stop).
    Callers scan in windows: a whole-bucket scan over months of points exceeds the memory an
    InfluxDB container is given and gets it killed (seen on a 768 MB limit)."""
    rng = f'range(start: {_rfc(start) if start else "0"}' + (f', stop: {_rfc(stop)})' if stop else ')')
    return (f'from(bucket: "{bucket}") |> {rng} |> filter(fn: (r) => not exists r.cluster_id) '
            '|> keep(columns: ["_measurement", "_time"]) |> group(columns: ["_measurement"]) '
            '|> reduce(fn: (r, accumulator) => ({count: accumulator.count + 1, '
            'first: if accumulator.count == 0 or r._time < accumulator.first then r._time else accumulator.first, '
            'last: if r._time > accumulator.last then r._time else accumulator.last}), '
            'identity: {count: 0, first: 2200-01-01T00:00:00Z, last: 1970-01-01T00:00:00Z})')


def first_tagged_query(bucket: str, around: datetime) -> str:
    """First tagged point per measurement, looked for in the two days around the registration
    (tagging began there), so the query never scans the whole bucket."""
    start, stop = around - timedelta(days=1), around + timedelta(days=2)
    return (f'from(bucket: "{bucket}") |> range(start: {_rfc(start)}, stop: {_rfc(stop)}) |> filter(fn: (r) => exists r.cluster_id) '
            '|> keep(columns: ["_measurement", "_time"]) |> group(columns: ["_measurement"]) '
            '|> reduce(fn: (r, accumulator) => ({first: if accumulator.first > r._time then r._time else accumulator.first}), '
            'identity: {first: 2200-01-01T00:00:00Z})')


def tag_keys_query(bucket: str, measurement: str) -> str:
    return ('import "influxdata/influxdb/schema"\n'
            f'schema.measurementTagKeys(bucket: "{bucket}", measurement: "{measurement}")')


def copy_query(src: str, dst: str, org: str, measurement: str, cluster_id: str, start: datetime, stop: datetime, group_columns: list[str]) -> str:
    cols = ', '.join(f'"{c}"' for c in group_columns)
    return (f'from(bucket: "{src}") |> range(start: {_rfc(start)}, stop: {_rfc(stop)}) '
            f'|> filter(fn: (r) => r._measurement == "{measurement}" and not exists r.cluster_id) '
            f'|> set(key: "cluster_id", value: "{cluster_id}") |> group(columns: [{cols}]) '
            f'|> to(bucket: "{dst}", org: "{org}")')


def copy_back_query(scratch: str, dst: str, org: str, measurement: Optional[str] = None,
                    start: Optional[datetime] = None, stop: Optional[datetime] = None) -> str:
    flt = f'|> filter(fn: (r) => r._measurement == "{measurement}") ' if measurement else ''
    rng = f'range(start: {_rfc(start)}, stop: {_rfc(stop)})' if start and stop else 'range(start: 0)'
    return f'from(bucket: "{scratch}") |> {rng} {flt}|> to(bucket: "{dst}", org: "{org}")'


def group_columns(tag_keys: list[str]) -> list[str]:
    """The group key to() needs so that cluster_id and the existing tags are written as tags."""
    return ['_measurement', '_field', 'cluster_id'] + sorted(k for k in tag_keys if k not in SYSTEM_COLUMNS)


def windows(start: datetime, stop: datetime, days: int = 7) -> list[tuple[datetime, datetime]]:
    out, cur = [], start
    while cur < stop:
        nxt = min(cur + timedelta(days=days), stop)
        out.append((cur, nxt)); cur = nxt
    return out


def _rfc(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


# ------------------------------------------------------------------ InfluxDB adapter
class InfluxAdapter:
    """The handful of calls the adoption needs, on an influxdb_client.InfluxDBClient."""

    def __init__(self, client, org: str):
        self.client, self.org = client, org

    def rows(self, flux: str) -> list[dict]:
        out = []
        for table in self.client.query_api().query(org=self.org, query=flux):
            for rec in table.records:
                out.append(dict(rec.values))
        return out

    def run(self, flux: str) -> None:
        self.client.query_api().query(org=self.org, query=flux)

    def delete(self, bucket: str, start: datetime, stop: datetime, predicate: str) -> None:
        self.client.delete_api().delete(start, stop, predicate, bucket=bucket, org=self.org)

    def bucket_exists(self, name: str) -> bool:
        return self.client.buckets_api().find_bucket_by_name(name) is not None

    def create_bucket(self, name: str) -> None:
        self.client.buckets_api().create_bucket(bucket_name=name, org=self.org)

    def delete_bucket(self, name: str) -> None:
        b = self.client.buckets_api().find_bucket_by_name(name)
        if b is not None:
            self.client.buckets_api().delete_bucket(b)


# ------------------------------------------------------------------ summary + state
SUMMARY_LOOKBACK_DAYS = 3 * 365
SUMMARY_GAP_DAYS = 45         # days without untagged data after the last hit end the look-back


def presence_query(bucket: str, start: datetime, stop: datetime) -> str:
    """One untagged row per measurement in [start, stop) — cheap: InfluxDB stops at the first hit
    per series. Counting millions of points per window exceeded a 768 MB InfluxDB and got it
    killed, which is why the summary only establishes which days hold legacy data."""
    return (f'from(bucket: "{bucket}") |> range(start: {_rfc(start)}, stop: {_rfc(stop)}) '
            '|> filter(fn: (r) => not exists r.cluster_id) |> keep(columns: ["_measurement", "_time"]) '
            '|> group(columns: ["_measurement"]) |> limit(n: 1)')


def untagged_days(adapter, bucket: str, stop: datetime, lookback_days: int = SUMMARY_LOOKBACK_DAYS,
                  gap_days: int = SUMMARY_GAP_DAYS) -> dict:
    """{measurement: {days, first, last}} of untagged data before `stop`, one query per day going
    back until `gap_days` empty days follow the last hit. Raises on a failed query (a dead InfluxDB
    must not read as "no legacy data")."""
    per: dict = {}
    day_stop = stop.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    oldest = stop - timedelta(days=lookback_days)
    empty_run, seen_any = 0, False
    while day_stop > oldest:
        day_start = day_stop - timedelta(days=1)
        rows = adapter.rows(presence_query(bucket, day_start, day_stop))
        if rows:
            seen_any, empty_run = True, 0
            for r in rows:
                m = per.setdefault(r['_measurement'], {'days': 0, 'first': None, 'last': None})
                m['days'] += 1
                t = r.get('_time')
                if t and (m['first'] is None or t < m['first']):
                    m['first'] = t
                if t and (m['last'] is None or t > m['last']):
                    m['last'] = t
        elif seen_any:
            empty_run += 1
            if empty_run >= gap_days:
                break
        day_stop = day_start
    return per


def summary(adapter, buckets: list[str], stop: Optional[datetime] = None) -> dict:
    """Untagged data per bucket and measurement: {bucket: {measurement: {days, first, last}}},
    the number of days with legacy data and the span; `error` when a bucket could not be scanned."""
    out, days, first, last, error = {}, 0, None, None, None
    stop = stop or datetime.now(timezone.utc)
    for b in buckets:
        try:
            per = untagged_days(adapter, b, stop)
        except Exception as e:  # noqa: BLE001 — reported, never mistaken for "nothing there"
            log.warning('legacy summary of %s: %s', b, e)
            error = f'{b}: {str(e)[:160]}'
            per = {}
        for m in per.values():
            days = max(days, m['days'])
            if m['first'] and (first is None or m['first'] < first):
                first = m['first']
            if m['last'] and (last is None or m['last'] > last):
                last = m['last']
        out[b] = per
    res = {'buckets': out, 'days': days, 'total': days, 'first': first.isoformat() if first else None, 'last': last.isoformat() if last else None}
    if error:
        res['error'] = error
    return res


_STATE: dict = {'state': 'idle'}
_LOCK = threading.Lock()
_SUMMARY: dict = {'state': 'idle', 'data': None, 'at': None}


def state() -> dict:
    return dict(_STATE)


def summary_state(adapter, buckets: list[str], stop: Optional[datetime], max_age: float = 300) -> dict:
    """The cached summary, refreshed in a background thread when older than max_age (a scan of
    months of data takes longer than an HTTP request should)."""
    with _LOCK:
        fresh = _SUMMARY['at'] and time.time() - _SUMMARY['at'] < max_age
        if _SUMMARY['state'] != 'computing' and not fresh:
            _SUMMARY['state'] = 'computing'

            def run():
                try:
                    data = summary(adapter, buckets, stop)
                except Exception as e:  # noqa: BLE001
                    log.warning('legacy summary failed: %s', e)
                    data = {'buckets': {}, 'total': 0, 'first': None, 'last': None, 'error': str(e)[:200]}
                with _LOCK:
                    # a failed scan is not cached: the next request retries (InfluxDB may just be restarting)
                    _SUMMARY.update({'state': 'ready', 'data': data, 'at': None if data.get('error') else time.time()})
            threading.Thread(target=run, name='hcm-legacy-summary', daemon=True).start()
        return {'state': _SUMMARY['state'], 'data': _SUMMARY['data']}


def invalidate_summary() -> None:
    with _LOCK:
        _SUMMARY.update({'at': None})


def start(cluster_id: str, adapter, buckets: list[str], org: str, registered_at: datetime, on_done: Optional[Callable] = None) -> dict:
    """Begin the adoption in a background thread; refuses while one runs."""
    with _LOCK:
        if _STATE.get('state') == 'running':
            return state()
        _STATE.clear()
        _STATE.update({'state': 'running', 'cluster_id': cluster_id, 'started_at': datetime.now(timezone.utc).isoformat(),
                       'bucket': None, 'measurement': None, 'done': 0, 'total': 0, 'points': 0, 'log': []})

    def run():
        try:
            points = adopt(cluster_id, adapter, buckets, org, registered_at, progress=_progress)
            _STATE.update({'state': 'done', 'points': points, 'finished_at': datetime.now(timezone.utc).isoformat()})
        except Exception as e:  # noqa: BLE001 — the thread is the error boundary
            log.exception('legacy adoption failed')
            _STATE.update({'state': 'failed', 'error': str(e)[:300], 'finished_at': datetime.now(timezone.utc).isoformat()})
        finally:
            invalidate_summary()
            if on_done:
                on_done()
    threading.Thread(target=run, name='hcm-legacy-adopt', daemon=True).start()
    return state()


def _progress(**kw) -> None:
    _STATE.update(kw)
    if 'note' in kw:
        _STATE.setdefault('log', []).append(kw['note'])


# ------------------------------------------------------------------ the work
def _run_windowed(adapter, start: datetime, stop: datetime, hours: int, make_query: Callable, wait: Callable) -> None:
    """Run make_query(w0, w1) over [start, stop) in windows of `hours`. A failure (typically the
    InfluxDB container restarting after exceeding its memory) waits for the server to answer
    again, halves the window down to one hour and retries the same range."""
    cur, size = start, timedelta(hours=hours)
    while cur < stop:
        w1 = min(cur + size, stop)
        try:
            adapter.run(make_query(cur, w1))
        except Exception as e:  # noqa: BLE001
            log.warning('legacy window %s–%s failed (%s); waiting for InfluxDB, retrying smaller', cur, w1, str(e)[:120])
            wait(adapter)
            if size > timedelta(hours=1):
                size = max(timedelta(hours=1), size / 2)
                continue
            raise
        cur = w1


def wait_for_server(adapter, timeout: float = 300) -> None:
    """Block until InfluxDB answers a trivial query again (it restarts within seconds after an
    out-of-memory kill); raises after `timeout`."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            adapter.rows('from(bucket: "_monitoring") |> range(start: -1m) |> limit(n: 1)')
            return
        except Exception:  # noqa: BLE001
            if time.monotonic() > deadline:
                raise
            time.sleep(5)


def adopt(cluster_id: str, adapter, buckets: list[str], org: str, registered_at: datetime, *, progress: Callable = lambda **kw: None,
          chunk_hours: int = 24, now: Optional[datetime] = None, wait: Callable = wait_for_server) -> int:
    """Re-tag every untagged point written before `registered_at` (the first cluster registration,
    where tagging began) in `buckets` with cluster_id. Works in windows of `chunk_hours` that
    shrink when a query fails. Returns the number of measurement-days adopted."""
    now = now or datetime.now(timezone.utc)
    era_end = min(registered_at + timedelta(days=2), now)
    # recovery: a previous run may have died between the delete and the copy back
    if adapter.bucket_exists(SCRATCH_BUCKET):
        for b in buckets:
            rows = adapter.rows(f'from(bucket: "{SCRATCH_BUCKET}") |> range(start: 0) |> keep(columns: ["_time"]) |> first()')
            if rows:
                first_t = min(r['_time'] for r in rows)
                _run_windowed(adapter, first_t, now, chunk_hours, lambda w0, w1: copy_back_query(SCRATCH_BUCKET, b, org, None, w0, w1), wait)
        adapter.delete_bucket(SCRATCH_BUCKET)
        progress(note='restored data left in the scratch bucket by an interrupted run')
    adopted = 0
    for bucket in buckets:
        per = untagged_days(adapter, bucket, era_end)
        if not per:
            continue
        tagged_first = {r['_measurement']: r['first'] for r in adapter.rows(first_tagged_query(bucket, registered_at))}
        progress(bucket=bucket, total=len(per), done=0)
        adapter.create_bucket(SCRATCH_BUCKET)
        try:
            for i, (m, info) in enumerate(sorted(per.items())):
                start_t, last_t = info['first'], info['last']
                stop_t = min(tagged_first.get(m) or registered_at, now)
                if last_t >= stop_t:
                    stop_t = last_t + timedelta(seconds=1)           # untagged points newer than the first tagged one
                progress(measurement=m, done=i)
                keys = [r.get('_value') for r in adapter.rows(tag_keys_query(bucket, m))]
                cols = group_columns([k for k in keys if k])
                _run_windowed(adapter, start_t, stop_t, chunk_hours,
                              lambda w0, w1: copy_query(bucket, SCRATCH_BUCKET, org, m, cluster_id, w0, w1, cols), wait)
                adapter.delete(bucket, start_t - timedelta(seconds=1), stop_t, f'_measurement="{m}"')
                _run_windowed(adapter, start_t, stop_t, chunk_hours,
                              lambda w0, w1: copy_back_query(SCRATCH_BUCKET, bucket, org, m, w0, w1), wait)
                adapter.delete(SCRATCH_BUCKET, datetime(1970, 1, 1, tzinfo=timezone.utc), now + timedelta(days=1), f'_measurement="{m}"')
                adopted += int(info['days'])
                progress(done=i + 1, points=adopted, note=f'{bucket}/{m}: {info["days"]} day(s) of data')
        finally:
            adapter.delete_bucket(SCRATCH_BUCKET)
    return adopted
