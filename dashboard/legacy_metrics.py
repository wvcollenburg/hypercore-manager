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

import fcntl
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

log = logging.getLogger('hcm.legacy_metrics')

SCRATCH_PREFIX = 'hcm_adopt_scratch'
SCRATCH_BUCKET = SCRATCH_PREFIX          # the generic name an older build used; restored into the first bucket


def scratch_name(bucket: str) -> str:
    """One scratch bucket per source bucket, so a restore after an interruption knows where the
    data belongs (a shared scratch would be copied back into every bucket)."""
    return f'{SCRATCH_PREFIX}__{bucket}'
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
    # to() returns everything it wrote as the query result; without the trailing group() |> limit()
    # the client downloads every copied point into the dashboard process (a week of raw points
    # is millions of rows: the gunicorn worker was killed for memory on the first real run)
    return (f'from(bucket: "{src}") |> range(start: {_rfc(start)}, stop: {_rfc(stop)}) '
            f'|> filter(fn: (r) => r._measurement == "{measurement}" and not exists r.cluster_id) '
            f'|> set(key: "cluster_id", value: "{cluster_id}") |> group(columns: [{cols}]) '
            f'|> to(bucket: "{dst}", org: "{org}") |> group() |> limit(n: 1)')


def copy_back_query(scratch: str, dst: str, org: str, measurement: Optional[str] = None,
                    start: Optional[datetime] = None, stop: Optional[datetime] = None) -> str:
    flt = f'|> filter(fn: (r) => r._measurement == "{measurement}") ' if measurement else ''
    rng = f'range(start: {_rfc(start)}, stop: {_rfc(stop)})' if start and stop else 'range(start: 0)'
    return f'from(bucket: "{scratch}") |> {rng} {flt}|> to(bucket: "{dst}", org: "{org}") |> group() |> limit(n: 1)'


def scratch_oldest_query(scratch: str = SCRATCH_BUCKET) -> str:
    return (f'from(bucket: "{scratch}") |> range(start: 0) |> limit(n: 1) '
            '|> keep(columns: ["_time"]) |> group() |> limit(n: 100000)')


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
SUMMARY_LOOKBACK_DAYS = 3 * 365   # the whole look-back is scanned: a gap before the upgrade must not hide older data


def presence_query(bucket: str, start: datetime, stop: datetime) -> str:
    """One untagged row per measurement in [start, stop) — cheap: InfluxDB stops at the first hit
    per series. Counting millions of points per window exceeded a 768 MB InfluxDB and got it
    killed, which is why the summary only establishes which days hold legacy data."""
    return (f'from(bucket: "{bucket}") |> range(start: {_rfc(start)}, stop: {_rfc(stop)}) '
            '|> filter(fn: (r) => not exists r.cluster_id) |> limit(n: 1) '          # pushed into storage: one point per series
            '|> keep(columns: ["_measurement", "_time"]) |> group(columns: ["_measurement"]) |> limit(n: 1)')


def untagged_days(adapter, bucket: str, stop: datetime, lookback_days: int = SUMMARY_LOOKBACK_DAYS,
                  progress: Callable = lambda **kw: None) -> dict:
    """{measurement: {days, first, last}} of untagged data before `stop`, one cheap query per day
    over the whole look-back (the query stops at the first point per series, so an empty day
    costs almost nothing). Raises on a failed query (a dead InfluxDB must not read as "no legacy
    data")."""
    per: dict = {}
    day_stop = stop.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    oldest = stop - timedelta(days=lookback_days)
    while day_stop > oldest:
        day_start = day_stop - timedelta(days=1)
        progress(bucket=bucket, day=day_start.strftime('%Y-%m-%d'), found=max((m['days'] for m in per.values()), default=0))
        rows = adapter.rows(presence_query(bucket, day_start, day_stop))
        if rows:
            for r in rows:
                m = per.setdefault(r['_measurement'], {'days': 0, 'first': None, 'last': None})
                m['days'] += 1
                t = r.get('_time')
                if t and (m['first'] is None or t < m['first']):
                    m['first'] = t
                if t and (m['last'] is None or t > m['last']):
                    m['last'] = t
        day_stop = day_start
    return per


def summary(adapter, buckets: list[str], stop: Optional[datetime] = None, progress: Callable = lambda **kw: None) -> dict:
    """Untagged data per bucket and measurement: {bucket: {measurement: {days, first, last}}},
    the number of days with legacy data and the span; `error` when a bucket could not be scanned."""
    out, days, first, last, error = {}, 0, None, None, None
    stop = stop or datetime.now(timezone.utc)
    for b in buckets:
        try:
            per = untagged_days(adapter, b, stop, progress=progress)
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


# ------------------------------------------------------------------ shared state
# The dashboard runs several gunicorn workers; the scan and the adoption must be visible to all
# of them and run once, so their state lives in a locked JSON file rather than in memory.
STATE_FILE = os.environ.get('HCM_LEGACY_STATE_FILE', '/tmp/hcm-legacy-metrics.json')
HEARTBEAT_STALE = 600          # seconds without a progress update: the worker running it died
SUMMARY_STALE = 1800           # seconds a scan may take before another worker starts a new one
_LOCK = threading.Lock()


def _load() -> dict:
    """Read under a shared lock: a writer truncates and rewrites the file, and a read that
    landed in that gap saw an empty file and reported a running adoption as idle."""
    try:
        with open(STATE_FILE) as fh:
            fcntl.flock(fh, fcntl.LOCK_SH)
            try:
                return json.load(fh)
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
    except (OSError, ValueError):
        return {}


def _update(fn: Callable[[dict], None]) -> dict:
    """Read-modify-write under a file lock; fn mutates the dict in place."""
    with _LOCK:
        os.makedirs(os.path.dirname(STATE_FILE) or '.', exist_ok=True)
        with open(STATE_FILE, 'a+') as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                fh.seek(0)
                try:
                    data = json.load(fh)
                except ValueError:
                    data = {}
                fn(data)
                fh.seek(0); fh.truncate(); json.dump(data, fh, default=str); fh.flush()
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
        return data


def state() -> dict:
    """The adoption's state as every worker sees it; a run whose heartbeat stopped is reported failed."""
    st = _load().get('adoption') or {'state': 'idle'}
    if st.get('state') == 'running' and time.time() - float(st.get('heartbeat') or 0) > HEARTBEAT_STALE:
        st = dict(st, state='failed', error='the worker running the adoption stopped; run it again, it resumes where it was')
    return st


def summary_state(adapter, buckets: list[str], stop: Optional[datetime], max_age: float = 6 * 3600) -> dict:
    """The cached summary shared by all workers; one worker refreshes it in a background thread
    when it is older than max_age. Legacy data does not change, so the cache lives for hours and
    is invalidated after an adoption; a full three-year pass takes minutes on a large install."""
    def claim(d):
        sm = d.setdefault('summary', {})
        now = time.time()
        fresh = sm.get('at') and now - sm['at'] < max_age
        computing = sm.get('state') == 'computing' and now - float(sm.get('started') or 0) < SUMMARY_STALE
        d['_claimed'] = not fresh and not computing
        if d['_claimed']:
            sm.update({'state': 'computing', 'started': now, 'progress': None})
    data = _update(claim)
    if data.pop('_claimed', False):
        def run():
            try:
                res = summary(adapter, buckets, stop, progress=lambda **kw: _update(lambda d: d.setdefault('summary', {}).__setitem__('progress', kw)))
            except Exception as e:  # noqa: BLE001
                log.warning('legacy summary failed: %s', e)
                res = {'buckets': {}, 'days': 0, 'total': 0, 'first': None, 'last': None, 'error': str(e)[:200]}
            _update(lambda d: d.setdefault('summary', {}).update({'state': 'ready', 'data': res, 'at': None if res.get('error') else time.time()}))
        threading.Thread(target=run, name='hcm-legacy-summary', daemon=True).start()
        _update(lambda d: d.pop('_claimed', None))
    sm = _load().get('summary') or {}
    return {'state': sm.get('state', 'idle'), 'data': sm.get('data'), 'progress': sm.get('progress')}


def invalidate_summary() -> None:
    _update(lambda d: d.setdefault('summary', {}).update({'at': None}))


def start(cluster_id: str, adapter, buckets: list[str], org: str, registered_at: datetime, on_done: Optional[Callable] = None) -> dict:
    """Begin the adoption in a background thread of this worker; refuses while one runs anywhere.
    The cached summary, if ready, tells it what to copy without a second scan."""
    known = (_load().get('summary') or {}).get('data') or {}
    known = known.get('buckets') if known.get('state', 'ready') and not known.get('error') else None
    def claim(d):
        cur = d.get('adoption') or {}
        running = cur.get('state') == 'running' and time.time() - float(cur.get('heartbeat') or 0) <= HEARTBEAT_STALE
        d['_claimed'] = not running
        if d['_claimed']:
            d['adoption'] = {'state': 'running', 'cluster_id': cluster_id, 'started_at': datetime.now(timezone.utc).isoformat(),
                             'heartbeat': time.time(), 'bucket': None, 'measurement': None, 'done': 0, 'total': 0, 'points': 0, 'log': []}
    data = _update(claim)
    if not data.pop('_claimed', False):
        return state()
    _update(lambda d: d.pop('_claimed', None))

    def run():
        try:
            points = adopt(cluster_id, adapter, buckets, org, registered_at, progress=_progress, known=known)
            _update(lambda d: d['adoption'].update({'state': 'done', 'points': points, 'finished_at': datetime.now(timezone.utc).isoformat()}))
        except Exception as e:  # noqa: BLE001 — the thread is the error boundary
            log.exception('legacy adoption failed')
            where = _load().get('adoption') or {}
            step = where.get('window') or {}
            ctx = f"{where.get('bucket') or '?'}/{where.get('measurement') or '?'} {step.get('phase') or ''} {str(step.get('from') or '')[:10]}".strip()
            msg = ' '.join(str(e).split())[:400]
            _update(lambda d: d['adoption'].update({'state': 'failed', 'error': f'at {ctx}: {msg}', 'finished_at': datetime.now(timezone.utc).isoformat()}))
        finally:
            invalidate_summary()
            if on_done:
                on_done()
    threading.Thread(target=run, name='hcm-legacy-adopt', daemon=True).start()
    return state()


def _progress(**kw) -> None:
    def upd(d):
        a = d.setdefault('adoption', {})
        a.update(kw); a['heartbeat'] = time.time()
        if 'note' in kw:
            a.setdefault('log', []).append(kw['note'])
    _update(upd)


# ------------------------------------------------------------------ the work
def _run_windowed(adapter, start: datetime, stop: datetime, hours: int, make_query: Callable, wait: Callable,
                  progress: Callable = lambda **kw: None, phase: str = 'copy') -> None:
    """Run make_query(w0, w1) over [start, stop) in windows of `hours`. A failure (typically the
    InfluxDB container restarting after exceeding its memory) waits for the server to answer
    again, halves the window down to one hour and retries the same range. Every window reports
    progress, which is what keeps the run's heartbeat alive during a long measurement."""
    cur, size = start, timedelta(hours=hours)
    while cur < stop:
        w1 = min(cur + size, stop)
        progress(window={'phase': phase, 'from': cur.isoformat(), 'to': w1.isoformat()})
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
          chunk_hours: int = 7 * 24, now: Optional[datetime] = None, wait: Callable = wait_for_server, known: Optional[dict] = None) -> int:
    """Re-tag every untagged point written before `registered_at` (the first cluster registration,
    where tagging began) in `buckets` with cluster_id. Works in windows of `chunk_hours` (a week
    by default: InfluxDB now refuses an oversized query with an error instead of dying, and the
    window halves on a failure down to one hour). Returns the number of measurement-days adopted."""
    now = now or datetime.now(timezone.utc)
    era_end = min(registered_at + timedelta(days=2), now)
    # recovery: a previous run may have died between the delete and the copy back
    for scratch, target in [(scratch_name(b), b) for b in buckets] + [(SCRATCH_BUCKET, buckets[0])]:
        if not adapter.bucket_exists(scratch):
            continue
        progress(note=f'scratch bucket {scratch} found: restoring what an interrupted run left there into {target}')
        try:
            # one row per series (limit before keep so storage stops early), oldest time in Python
            rows = adapter.rows(scratch_oldest_query(scratch))
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f'reading {scratch}: {str(e)[:200]}') from e
        if rows:
            first_t = min(r['_time'] for r in rows)
            _run_windowed(adapter, first_t, now, chunk_hours, lambda w0, w1: copy_back_query(scratch, target, org, None, w0, w1), wait, progress, 'restore')
        adapter.delete_bucket(scratch)
        progress(note=f'restored data left in {scratch} by an interrupted run')
    adopted = 0
    for bucket in buckets:
        # the summary the panel showed (same scan) is reused when given; otherwise scan, keeping
        # the heartbeat alive so a long scan is not mistaken for a dead worker
        per = (known or {}).get(bucket) or untagged_days(adapter, bucket, era_end, progress=lambda **kw: progress(scanning=kw))
        per = {m: v for m, v in per.items() if v.get('first') and v.get('last')}
        if not per:
            continue
        tagged_first = {r['_measurement']: r['first'] for r in adapter.rows(first_tagged_query(bucket, registered_at))}
        for v in per.values():                      # from the state file they are ISO strings
            for k in ('first', 'last'):
                if isinstance(v.get(k), str):
                    v[k] = datetime.fromisoformat(v[k].replace(' ', 'T'))
        progress(bucket=bucket, total=len(per), done=0, scanning=None)
        scratch = scratch_name(bucket)
        adapter.create_bucket(scratch)
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
                              lambda w0, w1: copy_query(bucket, scratch, org, m, cluster_id, w0, w1, cols), wait, progress, 'copy')
                adapter.delete(bucket, start_t - timedelta(seconds=1), stop_t, f'_measurement="{m}"')
                _run_windowed(adapter, start_t, stop_t, chunk_hours,
                              lambda w0, w1: copy_back_query(scratch, bucket, org, m, w0, w1), wait, progress, 'copy back')
                adapter.delete(scratch, datetime(1970, 1, 1, tzinfo=timezone.utc), now + timedelta(days=1), f'_measurement="{m}"')
                adopted += int(info['days'])
                progress(done=i + 1, points=adopted, note=f'{bucket}/{m}: {info["days"]} day(s) of data')
        finally:
            adapter.delete_bucket(scratch)
    return adopted
