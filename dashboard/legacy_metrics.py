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
def untagged_summary_query(bucket: str, stop: Optional[datetime] = None) -> str:
    """Untagged points per measurement with count, first and last time. `stop` bounds the scan to
    the legacy era (nothing written after the first registration can be legacy)."""
    rng = f'range(start: 0, stop: {_rfc(stop)})' if stop else 'range(start: 0)'
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


def copy_back_query(scratch: str, dst: str, org: str, measurement: Optional[str] = None) -> str:
    flt = f'|> filter(fn: (r) => r._measurement == "{measurement}") ' if measurement else ''
    return f'from(bucket: "{scratch}") |> range(start: 0) {flt}|> to(bucket: "{dst}", org: "{org}")'


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
def summary(adapter, buckets: list[str], stop: Optional[datetime] = None) -> dict:
    """Untagged points per bucket and measurement: {bucket: {measurement: {count, first, last}}}, totals."""
    out, total, first, last = {}, 0, None, None
    for b in buckets:
        per = {}
        try:
            rows = adapter.rows(untagged_summary_query(b, stop))
        except Exception as e:  # noqa: BLE001 — a missing bucket is not an error for the UI
            log.warning('legacy summary of %s: %s', b, e)
            rows = []
        for r in rows:
            n = int(r.get('count') or 0)
            if n <= 0:
                continue
            per[r['_measurement']] = {'count': n, 'first': r.get('first'), 'last': r.get('last')}
            total += n
            f, l = r.get('first'), r.get('last')
            if f and (first is None or f < first):
                first = f
            if l and (last is None or l > last):
                last = l
        out[b] = per
    return {'buckets': out, 'total': total, 'first': first.isoformat() if first else None, 'last': last.isoformat() if last else None}


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
                    _SUMMARY.update({'state': 'ready', 'data': data, 'at': time.time()})
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
def adopt(cluster_id: str, adapter, buckets: list[str], org: str, registered_at: datetime, *, progress: Callable = lambda **kw: None,
          chunk_days: int = 7, now: Optional[datetime] = None) -> int:
    """Re-tag every untagged point written before `registered_at` (the first cluster registration,
    where tagging began) in `buckets` with cluster_id. Returns the number of points adopted."""
    now = now or datetime.now(timezone.utc)
    era_end = min(registered_at + timedelta(days=2), now)
    # recovery: a previous run may have died between the delete and the copy back
    if adapter.bucket_exists(SCRATCH_BUCKET):
        for b in buckets:
            adapter.run(copy_back_query(SCRATCH_BUCKET, b, org))
        adapter.delete_bucket(SCRATCH_BUCKET)
        progress(note='restored data left in the scratch bucket by an interrupted run')
    adopted = 0
    for bucket in buckets:
        rows = adapter.rows(untagged_summary_query(bucket, era_end))
        per = {r['_measurement']: r for r in rows if int(r.get('count') or 0) > 0}
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
                for w0, w1 in windows(start_t, stop_t, chunk_days):
                    adapter.run(copy_query(bucket, SCRATCH_BUCKET, org, m, cluster_id, w0, w1, cols))
                adapter.delete(bucket, start_t - timedelta(seconds=1), stop_t, f'_measurement="{m}"')
                adapter.run(copy_back_query(SCRATCH_BUCKET, bucket, org, m))
                adapter.delete(SCRATCH_BUCKET, datetime(1970, 1, 1, tzinfo=timezone.utc), now + timedelta(days=1), f'_measurement="{m}"')
                adopted += int(info['count'])
                progress(done=i + 1, points=adopted, note=f'{bucket}/{m}: {info["count"]} points')
        finally:
            adapter.delete_bucket(SCRATCH_BUCKET)
    return adopted
