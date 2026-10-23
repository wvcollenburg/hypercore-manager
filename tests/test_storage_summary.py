"""dashboard/storage_summary.py — the Infraviewer's storage overview, pure."""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'dashboard'))
import storage_summary as ss  # noqa: E402

NOW = datetime(2026, 10, 22, 1, 0, tzinfo=timezone.utc)


def _t(minutes_ago):
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


def test_per_vm_series_cluster_totals_and_worst_latency():
    rows = [
        ('vm-a', 'alpha', 'read_iops', _t(2), 100.0), ('vm-a', 'alpha', 'write_iops', _t(2), 50.0),
        ('vm-a', 'alpha', 'write_latency_us', _t(2), 900.0),
        ('vm-b', 'beta', 'read_iops', _t(2), 10.0), ('vm-b', 'beta', 'write_latency_us', _t(2), 4000.0),
        ('vm-a', 'alpha', 'read_iops', _t(1), 120.0), ('vm-a', 'alpha', 'write_latency_us', _t(1), 950.0),
        ('vm-a', 'alpha', 'bogus_field', _t(1), 1.0), ('vm-a', 'alpha', 'read_kbps', _t(1), None),
    ]
    out = ss.build(rows, [], NOW, 120)
    assert [v['uuid'] for v in out['vms']] == ['vm-a', 'vm-b'], 'sorted by name'
    a = out['vms'][0]
    assert [p['time'] for p in a['history']] == [_t(2), _t(1)], 'oldest first'
    assert a['history'][0] == {'time': _t(2), 'read_iops': 100.0, 'write_iops': 50.0, 'write_latency_us': 900.0}
    assert ss.build([('v', 'v', 'read_iops', _t(0), 12.3456)], [], NOW, 120)['vms'][0]['history'][0]['read_iops'] == 12.3, 'one decimal'
    assert 'bogus_field' not in a['history'][1] and 'read_kbps' not in a['history'][1]
    c = out['cluster']['history']
    assert c[0]['read_iops'] == 110.0 and c[0]['write_iops'] == 50.0, 'IOPS summed over VMs'
    assert c[0]['write_latency_us'] == 4000.0, 'latency is the worst VM (whose series is its worst disk)'
    assert c[1] == {'time': _t(1), 'read_iops': 120.0, 'write_latency_us': 950.0}


def test_stale_vms_and_disks_are_dropped_and_latest_samples_attach():
    rows = [('vm-old', 'old', 'read_iops', _t(10), 5.0), ('vm-a', 'alpha', 'read_iops', _t(1), 1.0)]
    latest = [
        {'uuid': 'd1', 'name': 'disk-1', 'type': 'VIRTIO_DISK', 'vm_uuid': 'vm-a', 'vm_name': 'alpha', 'node_uuid': 'n1', 'time': _t(1),
         'read_kbps': 10.0, 'write_kbps': 20.0, 'read_latency_us': 300.0, 'write_latency_us': 400.0, 'read_iops': 1.0, 'write_iops': 2.0},
        {'uuid': 'd0', 'name': 'a-disk', 'type': 'IDE_DISK', 'vm_uuid': 'vm-a', 'vm_name': 'alpha', 'node_uuid': 'n1', 'time': _t(1)},
        {'uuid': 'd9', 'name': 'gone', 'type': 'VIRTIO_DISK', 'vm_uuid': 'vm-old', 'vm_name': 'old', 'node_uuid': 'n1', 'time': _t(10)},
        {'uuid': 'd7', 'name': 'fresh-disk-of-vm-without-series', 'type': 'VIRTIO_DISK', 'vm_uuid': 'vm-new', 'vm_name': 'new', 'node_uuid': 'n2', 'time': _t(0)},
    ]
    out = ss.build(rows, latest, NOW, 120)
    by = {v['uuid']: v for v in out['vms']}
    assert set(by) == {'vm-a', 'vm-new'}, 'vm-old has only stale samples'
    assert [d['name'] for d in by['vm-a']['vsds']] == ['a-disk', 'disk-1'] and by['vm-a']['vsds'][1]['write_latency_us'] == 400.0
    assert by['vm-a']['vsds'][0]['read_kbps'] is None, 'missing fields stay None, never 0'
    assert by['vm-new']['history'] == [] and by['vm-new']['name'] == 'new'
    assert out['cluster']['history'] == [{'time': _t(1), 'read_iops': 1.0}], 'stale VMs do not feed the totals'


def test_flux_queries_aggregate_per_vm_in_influx():
    sum_q, max_q, latest_q = ss.flux_queries('metrics', '|> filter(fn: (r) => r["cluster_id"] == "c1")', 24, '10m', 120, stop=NOW)
    for q in (sum_q, max_q, latest_q):
        assert 'r["_measurement"] == "vsd_metrics"' in q and 'r["cluster_id"] == "c1"' in q
    assert 'range(start: -24h, stop: 2026-10-22T01:00:00Z)' in sum_q and sum_q.count('aggregateWindow(every: 10m') == 2
    assert 'stop: 2026-10-22T01:00:00Z' in max_q, 'both series queries end at the same instant'
    assert 'group(columns: ["vm_uuid", "vm_name", "_field"])' in sum_q and 'fn: sum' in sum_q and 'read_kbps' in sum_q
    assert 'fn: max' in max_q and 'write_latency_us' in max_q and 'read_iops' not in max_q
    assert 'last()' in latest_q and 'pivot' in latest_q and 'range(start: -240s)' in latest_q
