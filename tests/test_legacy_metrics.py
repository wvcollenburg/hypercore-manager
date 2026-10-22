"""dashboard/legacy_metrics.py — adopting 0.9.x metrics without a cluster tag, with a fake InfluxDB."""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'dashboard'))
import legacy_metrics as lm  # noqa: E402

T0 = datetime(2026, 5, 1, tzinfo=timezone.utc)
NOW = datetime(2026, 10, 22, tzinfo=timezone.utc)


class _Fake:
    """Untagged points per bucket/measurement with first/last times; records every call."""
    def __init__(self, data, tagged_first=None, scratch_exists=False):
        self.data = data; self.tagged_first = tagged_first or {}; self.calls = []; self.buckets = {lm.SCRATCH_BUCKET} if scratch_exists else set()
    def rows(self, flux):
        self.calls.append(('rows', flux))
        bucket = flux.split('from(bucket: "')[1].split('"')[0] if 'from(bucket' in flux else flux.split('bucket: "')[1].split('"')[0]
        if 'not exists r.cluster_id' in flux and 'reduce' in flux:
            assert 'stop: ' in flux, 'the legacy scan is bounded by the registration era'
            return [{'_measurement': m, 'count': v['count'], 'first': v['first'], 'last': v['last']} for m, v in self.data.get(bucket, {}).items()]
        if 'exists r.cluster_id' in flux and 'reduce' in flux:
            return [{'_measurement': m, 'first': t} for m, t in self.tagged_first.get(bucket, {}).items()]
        if 'measurementTagKeys' in flux:
            return [{'_value': k} for k in ('_start', '_stop', '_measurement', '_field', 'node', 'vm_uuid')]
        raise AssertionError(flux)
    def run(self, flux): self.calls.append(('run', flux))
    def delete(self, bucket, start, stop, predicate): self.calls.append(('delete', bucket, start, stop, predicate))
    def bucket_exists(self, name): return name in self.buckets
    def create_bucket(self, name): self.buckets.add(name); self.calls.append(('create', name))
    def delete_bucket(self, name): self.buckets.discard(name); self.calls.append(('drop', name))


def test_flux_builders_and_windows():
    assert 'not exists r.cluster_id' in lm.untagged_summary_query('metrics') and 'group(columns: ["_measurement"])' in lm.untagged_summary_query('metrics')
    assert 'range(start: 0, stop: 2026-05-01T00:00:00.000000Z)' in lm.untagged_summary_query('metrics', T0)
    assert 'range(start: 2026-04-30T00:00:00.000000Z, stop: 2026-05-03T00:00:00.000000Z)' in lm.first_tagged_query('metrics', T0)
    q = lm.copy_query('metrics', lm.SCRATCH_BUCKET, 'org', 'vm_metrics', 'cid-1', T0, T0 + timedelta(days=1), ['_measurement', '_field', 'cluster_id', 'vm_uuid'])
    assert 'set(key: "cluster_id", value: "cid-1")' in q and 'group(columns: ["_measurement", "_field", "cluster_id", "vm_uuid"])' in q
    assert 'range(start: 2026-05-01T00:00:00.000000Z, stop: 2026-05-02T00:00:00.000000Z)' in q and f'to(bucket: "{lm.SCRATCH_BUCKET}", org: "org")' in q
    assert lm.group_columns(['_start', 'node', '_field', 'vm_uuid', 'cluster_id']) == ['_measurement', '_field', 'cluster_id', 'node', 'vm_uuid']
    w = lm.windows(T0, T0 + timedelta(days=20), 7)
    assert len(w) == 3 and w[0] == (T0, T0 + timedelta(days=7)) and w[-1][1] == T0 + timedelta(days=20)
    assert lm.windows(T0, T0, 7) == []


def test_adopt_copies_deletes_and_copies_back_per_measurement():
    data = {'metrics': {'vm_metrics': {'count': 1000, 'first': T0, 'last': T0 + timedelta(days=100)},
                        'node_metrics': {'count': 50, 'first': T0 + timedelta(days=3), 'last': T0 + timedelta(days=90)}},
            'metrics_longterm': {}}
    tagged = {'metrics': {'vm_metrics': T0 + timedelta(days=120), 'node_metrics': T0 + timedelta(days=120)}}
    fk = _Fake(data, tagged)
    notes = []
    n = lm.adopt('cid-1', fk, ['metrics', 'metrics_longterm'], 'org', T0 + timedelta(days=119), progress=lambda **kw: notes.append(kw), now=NOW, chunk_days=30)
    assert n == 1050
    runs = [c[1] for c in fk.calls if c[0] == 'run']
    copies = [q for q in runs if 'set(key: "cluster_id"' in q and 'node_metrics' in q]
    assert len(copies) == 4, '117 days of node_metrics (first untagged → first tagged) in 30-day windows'
    assert all('to(bucket: "hcm_adopt_scratch"' in q for q in copies)
    backs = [q for q in runs if q.startswith('from(bucket: "hcm_adopt_scratch")')]
    assert len(backs) == 2 and all('to(bucket: "metrics"' in q for q in backs)
    deletes = [c for c in fk.calls if c[0] == 'delete']
    main_del = [c for c in deletes if c[1] == 'metrics']
    assert len(main_del) == 2 and all(c[3] == T0 + timedelta(days=120) for c in main_del), 'deleted up to the first tagged point'
    assert main_del[0][4] == '_measurement="node_metrics"'
    # order per measurement: copies → delete main → copy back → clear scratch
    kinds = [(c[0], (c[1] if c[0] != 'run' else ('copy' if 'set(key' in c[1] else 'back'))) for c in fk.calls if c[0] in ('run', 'delete')]
    first_m = kinds[:len(copies) + 3]
    assert [k for k in first_m] == [('run', 'copy')] * 4 + [('delete', 'metrics'), ('run', 'back'), ('delete', 'hcm_adopt_scratch')]
    assert ('create', 'hcm_adopt_scratch') in fk.calls and ('drop', 'hcm_adopt_scratch') in fk.calls
    assert not fk.buckets, 'scratch bucket dropped at the end'
    assert notes[-1]['points'] == 1050 and notes[-1]['done'] == 2


def test_adopt_without_tagged_points_uses_now_and_recovers_scratch():
    data = {'metrics': {'vm_metrics': {'count': 10, 'first': T0, 'last': T0 + timedelta(days=1)}}}
    fk = _Fake(data, {}, scratch_exists=True)
    notes = []
    assert lm.adopt('cid-1', fk, ['metrics'], 'org', T0 + timedelta(days=5), progress=lambda **kw: notes.append(kw), now=NOW) == 10
    assert fk.calls[0] == ('run', lm.copy_back_query(lm.SCRATCH_BUCKET, 'metrics', 'org')) and fk.calls[1] == ('drop', lm.SCRATCH_BUCKET)
    assert any('interrupted' in (n.get('note') or '') for n in notes)
    main_del = next(c for c in fk.calls if c[0] == 'delete' and c[1] == 'metrics')
    assert main_del[3] == T0 + timedelta(days=5), 'no tagged point found: the registration time bounds the era'
    assert lm.adopt('cid-1', _Fake({'metrics': {}}), ['metrics'], 'org', T0, now=NOW) == 0, 'idempotent'


def test_summary_totals_and_span():
    fk = _Fake({'metrics': {'vm_metrics': {'count': 7, 'first': T0, 'last': T0 + timedelta(days=2)}, 'x': {'count': 0, 'first': None, 'last': None}},
                'metrics_longterm': {'vm_metrics': {'count': 3, 'first': T0 - timedelta(days=1), 'last': T0 + timedelta(days=1)}}})
    s = lm.summary(fk, ['metrics', 'metrics_longterm'], stop=T0 + timedelta(days=10))
    assert s['total'] == 10 and 'x' not in s['buckets']['metrics'] and s['first'] == (T0 - timedelta(days=1)).isoformat() and s['last'] == (T0 + timedelta(days=2)).isoformat()
