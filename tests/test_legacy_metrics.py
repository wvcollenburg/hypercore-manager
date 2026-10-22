"""dashboard/legacy_metrics.py — adopting 0.9.x metrics without a cluster tag, with a fake InfluxDB."""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'dashboard'))
import legacy_metrics as lm  # noqa: E402

T0 = datetime(2026, 5, 1, tzinfo=timezone.utc)
NOW = datetime(2026, 10, 22, tzinfo=timezone.utc)


def _window(flux):
    w0 = datetime.strptime(flux.split('range(start: ')[1].split(',')[0], '%Y-%m-%dT%H:%M:%S.%fZ').replace(tzinfo=timezone.utc)
    w1 = datetime.strptime(flux.split('stop: ')[1].split(')')[0], '%Y-%m-%dT%H:%M:%S.%fZ').replace(tzinfo=timezone.utc)
    return w0, w1


class _Fake:
    """Untagged data per bucket/measurement as a [first, last] span; records every call.
    `fail_windows_over` makes copy queries wider than that many hours fail once (a server dying)."""
    def __init__(self, data, scratch_exists=False, fail_windows_over=None):
        self.data = data; self.calls = []; self.buckets = {lm.SCRATCH_BUCKET} if scratch_exists else set()
        self.fail_windows_over = fail_windows_over; self.failed = 0; self.waits = 0
    def rows(self, flux):
        self.calls.append(('rows', flux))
        if '_monitoring' in flux:
            return []
        bucket = flux.split('from(bucket: "')[1].split('"')[0] if 'from(bucket' in flux else flux.split('bucket: "')[1].split('"')[0]
        if 'not exists r.cluster_id' in flux and 'limit(n: 1)' in flux:
            w0, w1 = _window(flux)
            return [{'_measurement': m, '_time': max(v[0], w0)} for m, v in self.data.get(bucket, {}).items() if v[0] < w1 and v[1] >= w0]
        if 'exists r.cluster_id' in flux and 'reduce' in flux and 'not exists' not in flux:
            return []                                     # no tagged points around the registration
        if 'measurementTagKeys' in flux:
            return [{'_value': k} for k in ('_start', '_stop', '_measurement', '_field', 'node', 'vm_uuid')]
        if bucket == lm.SCRATCH_BUCKET and 'first()' in flux:
            return [{'_time': T0}]
        raise AssertionError(flux)
    def run(self, flux):
        self.calls.append(('run', flux))
        if self.fail_windows_over is not None and 'set(key' in flux:
            w0, w1 = _window(flux)
            if (w1 - w0) > timedelta(hours=self.fail_windows_over):
                self.failed += 1; raise ConnectionError('Remote end closed connection without response')
    def delete(self, bucket, start, stop, predicate): self.calls.append(('delete', bucket, start, stop, predicate))
    def bucket_exists(self, name): return name in self.buckets
    def create_bucket(self, name): self.buckets.add(name); self.calls.append(('create', name))
    def delete_bucket(self, name): self.buckets.discard(name); self.calls.append(('drop', name))


def _wait(fk):
    fk.waits += 1


def test_flux_builders_and_windows():
    q = lm.copy_query('metrics', lm.SCRATCH_BUCKET, 'org', 'vm_metrics', 'cid-1', T0, T0 + timedelta(days=1), ['_measurement', '_field', 'cluster_id', 'vm_uuid'])
    assert 'set(key: "cluster_id", value: "cid-1")' in q and 'group(columns: ["_measurement", "_field", "cluster_id", "vm_uuid"])' in q
    assert 'range(start: 2026-05-01T00:00:00.000000Z, stop: 2026-05-02T00:00:00.000000Z)' in q and f'to(bucket: "{lm.SCRATCH_BUCKET}", org: "org")' in q
    assert lm.group_columns(['_start', 'node', '_field', 'vm_uuid', 'cluster_id']) == ['_measurement', '_field', 'cluster_id', 'node', 'vm_uuid']
    assert 'limit(n: 1)' in lm.presence_query('metrics', T0, T0 + timedelta(days=1)) and 'not exists r.cluster_id' in lm.presence_query('metrics', T0, T0 + timedelta(days=1))
    assert 'range(start: 2026-05-01T00:00:00.000000Z, stop: 2026-05-02T00:00:00.000000Z)' in lm.copy_back_query('s', 'metrics', 'org', 'vm_metrics', T0, T0 + timedelta(days=1))
    assert lm.windows(T0, T0 + timedelta(days=20), 7)[-1][1] == T0 + timedelta(days=20)


def test_summary_counts_days_per_day_over_the_whole_lookback():
    fk = _Fake({'metrics': {'vm_metrics': (T0, T0 + timedelta(days=9, hours=12)), 'node_metrics': (T0 + timedelta(days=5), T0 + timedelta(days=6))},
                'metrics_longterm': {}})
    s = lm.summary(fk, ['metrics', 'metrics_longterm'], stop=T0 + timedelta(days=30))
    assert s['days'] in (10, 11) and s['buckets']['metrics']['vm_metrics']['days'] in (10, 11) and s['buckets']['metrics']['node_metrics']['days'] in (1, 2), 'day windows are hour-aligned, so a span may touch one extra window'
    assert s['first'] == T0.isoformat() and s['last'] >= (T0 + timedelta(days=9)).isoformat()
    scans = [c for c in fk.calls if c[0] == 'rows' and 'limit(n: 1)' in c[1] and 'from(bucket: "metrics")' in c[1]]
    assert lm.SUMMARY_LOOKBACK_DAYS + 2 >= len(scans) >= lm.SUMMARY_LOOKBACK_DAYS - 1, 'one cheap query per day over the whole look-back'
    far = _Fake({'metrics': {'vm_metrics': (T0 - timedelta(days=400), T0 - timedelta(days=399))}})
    assert lm.summary(far, ['metrics'], stop=T0)['days'] >= 1, 'data long before the upgrade, with nothing in between, is still found'
    assert all('count()' not in c[1] for c in scans), 'presence only, never a count'

    class _Dead(_Fake):
        def rows(self, flux): raise ConnectionError('Remote end closed connection without response')
    s = lm.summary(_Dead({}), ['metrics'], stop=T0)
    assert s['days'] == 0 and 'Remote end closed' in s['error'], 'a dead InfluxDB is reported, not read as no legacy data'


def test_adopt_copies_deletes_and_copies_back_in_windows():
    data = {'metrics': {'vm_metrics': (T0, T0 + timedelta(days=100)), 'node_metrics': (T0 + timedelta(days=3), T0 + timedelta(days=90))}, 'metrics_longterm': {}}
    fk = _Fake(data); notes = []
    reg = T0 + timedelta(days=119)
    n = lm.adopt('cid-1', fk, ['metrics', 'metrics_longterm'], 'org', reg, progress=lambda **kw: notes.append(kw), now=NOW, chunk_hours=24 * 30, wait=_wait)
    assert n == 101 + 88, 'measurement-days adopted'
    runs = [c[1] for c in fk.calls if c[0] == 'run']
    copies = [q for q in runs if 'set(key: "cluster_id"' in q and 'node_metrics' in q]
    assert len(copies) == 4, 'node_metrics: first untagged → registration in 30-day windows'
    assert all('to(bucket: "hcm_adopt_scratch"' in q for q in copies)
    backs = [q for q in runs if q.startswith('from(bucket: "hcm_adopt_scratch")') and 'node_metrics' in q]
    assert len(backs) == 4 and all('to(bucket: "metrics"' in q and 'range(start: 2' in q for q in backs), 'the copy back is windowed as well'
    main_del = [c for c in fk.calls if c[0] == 'delete' and c[1] == 'metrics']
    assert len(main_del) == 2 and all(c[3] == reg for c in main_del), 'deleted up to the registration'
    assert ('create', 'hcm_adopt_scratch') in fk.calls and ('drop', 'hcm_adopt_scratch') in fk.calls and not fk.buckets
    assert [n for n in notes if 'done' in n][-1]['done'] == 2 and fk.waits == 0


def test_adopt_shrinks_the_window_and_waits_when_a_query_fails():
    fk = _Fake({'metrics': {'vm_metrics': (T0, T0 + timedelta(days=2))}}, fail_windows_over=6)
    n = lm.adopt('cid-1', fk, ['metrics'], 'org', T0 + timedelta(days=3), now=NOW, chunk_hours=24, wait=_wait)
    assert n == 3
    copies = [c[1] for c in fk.calls if c[0] == 'run' and 'set(key' in c[1]]
    sizes = {(_window(q)[1] - _window(q)[0]) for q in copies}
    assert max(sizes) == timedelta(hours=24) and timedelta(hours=6) in sizes and min(sizes) >= timedelta(hours=1)
    assert fk.failed == 2 and fk.waits == 2, '24 h failed, 12 h failed, 6 h windows went through; each failure waited for the server'
    assert ('drop', 'hcm_adopt_scratch') in fk.calls
    fk2 = _Fake({'metrics': {'vm_metrics': (T0, T0 + timedelta(hours=3))}}, fail_windows_over=0)
    with pytest.raises(ConnectionError):
        lm.adopt('cid-1', fk2, ['metrics'], 'org', T0 + timedelta(days=1), now=NOW, chunk_hours=2, wait=_wait)
    assert not fk2.buckets, 'the scratch bucket is dropped even when giving up'


def test_adopt_recovers_scratch_and_is_idempotent():
    fk = _Fake({'metrics': {'vm_metrics': (T0, T0 + timedelta(days=1))}}, scratch_exists=True)
    notes = []
    assert lm.adopt('cid-1', fk, ['metrics'], 'org', T0 + timedelta(days=5), progress=lambda **kw: notes.append(kw), now=NOW, wait=_wait) == 2
    first_runs = [c for c in fk.calls if c[0] == 'run'][:1]
    assert first_runs and first_runs[0][1].startswith('from(bucket: "hcm_adopt_scratch")') and 'to(bucket: "metrics"' in first_runs[0][1]
    assert any('interrupted' in (n.get('note') or '') for n in notes)
    assert lm.adopt('cid-1', _Fake({'metrics': {}}), ['metrics'], 'org', T0, now=NOW, wait=_wait) == 0, 'idempotent'


def test_state_is_shared_through_the_file_and_stale_runs_are_reported(tmp_path, monkeypatch):
    """Several gunicorn workers serve the dashboard: the state a worker writes must be what every
    other worker reads, a second start while one runs is refused, and a run whose worker died is
    reported failed instead of running forever."""
    import time as _t
    monkeypatch.setattr(lm, 'STATE_FILE', str(tmp_path / 'state.json'))
    fk = _Fake({'metrics': {'vm_metrics': (T0, T0 + timedelta(days=1))}})
    monkeypatch.setattr(lm, 'wait_for_server', _wait)
    st = lm.start('cid-1', fk, ['metrics'], 'org', T0 + timedelta(days=3))
    assert st['state'] in ('running', 'done')
    for _ in range(200):
        if lm.state()['state'] == 'done':
            break
        _t.sleep(0.01)
    seen = lm.state()                                   # read back through the file, as another worker would
    assert seen['state'] == 'done' and seen['points'] == 2 and seen['cluster_id'] == 'cid-1'
    # a run whose heartbeat stopped
    lm._update(lambda d: d['adoption'].update({'state': 'running', 'heartbeat': _t.time() - lm.HEARTBEAT_STALE - 1}))
    assert lm.state()['state'] == 'failed' and 'stopped' in lm.state()['error']
    # the summary is claimed once; a second caller sees "computing" and does not start another scan
    calls = []
    class _Slow(_Fake):
        def rows(self, flux):
            calls.append(1); _t.sleep(0.005); return super().rows(flux)
    slow = _Slow({'metrics': {}})
    a = lm.summary_state(slow, ['metrics'], T0, max_age=300); b = lm.summary_state(slow, ['metrics'], T0, max_age=300)
    assert a['state'] == 'computing' and b['state'] == 'computing'
    for _ in range(1500):
        if lm.summary_state(slow, ['metrics'], T0)['state'] == 'ready':
            break
        _t.sleep(0.01)
    _t.sleep(0.3)
    assert len(calls) <= lm.SUMMARY_LOOKBACK_DAYS + 2, 'one scan ran, not one per caller'
    assert lm.summary_state(slow, ['metrics'], T0)['data']['days'] == 0


def test_adopt_reuses_a_known_summary_and_heartbeats_while_scanning():
    fk = _Fake({'metrics': {'vm_metrics': (T0, T0 + timedelta(days=1))}})
    known = {'metrics': {'vm_metrics': {'days': 2, 'first': T0.isoformat(), 'last': (T0 + timedelta(days=1)).isoformat()}}}
    notes = []
    n = lm.adopt('cid-1', fk, ['metrics'], 'org', T0 + timedelta(days=3), progress=lambda **kw: notes.append(kw), now=NOW, wait=_wait, known=known)
    assert n == 2 and not any('limit(n: 1)' in c[1] and 'not exists' in c[1] for c in fk.calls if c[0] == 'rows'), 'no rescan when the summary is known'
    fk2 = _Fake({'metrics': {'vm_metrics': (T0, T0 + timedelta(days=1))}}); notes2 = []
    lm.adopt('cid-1', fk2, ['metrics'], 'org', T0 + timedelta(days=3), progress=lambda **kw: notes2.append(kw), now=NOW, wait=_wait)
    assert any(n.get('scanning') for n in notes2), 'without a known summary the scan reports progress (which refreshes the heartbeat)'


def test_every_window_reports_progress_so_the_heartbeat_moves():
    fk = _Fake({'metrics': {'vm_metrics': (T0, T0 + timedelta(days=3))}}); notes = []
    lm.adopt('cid-1', fk, ['metrics'], 'org', T0 + timedelta(days=5), progress=lambda **kw: notes.append(kw), now=NOW, chunk_hours=24, wait=_wait)
    windows = [n['window'] for n in notes if n.get('window')]
    assert len(windows) >= 10 and {w['phase'] for w in windows} == {'copy', 'copy back'}, 'copy and copy back report every window'
