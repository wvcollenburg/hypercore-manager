"""dashboard/vm_rows.py — a VM belongs to the node of its newest sample, not of the first series found."""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'dashboard'))
import vm_rows  # noqa: E402

NOW = datetime(2026, 11, 22, 16, 0, tzinfo=timezone.utc)


def _rec(minutes_ago, vm='vm-a', node='node-1', peer='1', cpu=10.0, **tags):
    values = {'vm_uuid': vm, 'vm_name': tags.get('name', 'alpha'), 'node_uuid': node, 'node_peer_id': peer,
              'vm_tag': tags.get('tag', 'prod'), 'vm_vlan': tags.get('vlan', '102'),
              'cpu_usage': cpu, 'rx_bit_rate': 1.0, 'tx_bit_rate': 2.0}
    return NOW - timedelta(minutes=minutes_ago), values


def test_a_migrated_vm_is_listed_on_the_node_it_runs_on_now():
    # Influx returns one table per series, the old node's (tag '1') before the new node's (tag '3')
    recs = [_rec(120), _rec(90), _rec(60), _rec(30, node='node-3', peer='3'), _rec(5, node='node-3', peer='3')]
    vm = vm_rows.fold(recs)['vm-a']
    assert (vm['node_uuid'], vm['node_peer_id']) == ('node-3', '3')


def test_the_order_of_the_series_does_not_matter():
    recs = [_rec(5, node='node-3', peer='3'), _rec(30, node='node-3', peer='3'), _rec(120), _rec(60)]
    vm = vm_rows.fold(recs)['vm-a']
    assert vm['node_peer_id'] == '3'
    times = [h['time'] for h in vm['history']]
    assert times == sorted(times) and len(times) == 4, 'history is one series, oldest first'


def test_name_tag_and_vlan_follow_the_newest_sample_too():
    recs = [_rec(60, name='old-name', tag='test', vlan='0'), _rec(5, name='new-name', tag='prod', vlan='201')]
    vm = vm_rows.fold(recs)['vm-a']
    assert (vm['name'], vm['tag'], vm['vlan']) == ('new-name', 'prod', '201')


def test_samples_without_a_node_are_left_out():
    recs = [_rec(60), _rec(5, node='unknown', peer='unknown')]
    vm = vm_rows.fold(recs)['vm-a']
    assert vm['node_peer_id'] == '1' and len(vm['history']) == 1
    assert vm_rows.fold([_rec(5, vm='vm-b', node='unknown', peer='unknown')]) == {}


def test_every_node_with_a_vm_gets_its_group():
    # the reported case: three nodes, the VMs on node 3 all arrived there by migration
    recs = [_rec(600, vm='a'), _rec(5, vm='a', node='node-3', peer='3'),
            _rec(600, vm='b', node='node-2', peer='2'), _rec(5, vm='b', node='node-3', peer='3'),
            _rec(5, vm='c'), _rec(5, vm='d', node='node-2', peer='2')]
    peers = sorted({v['node_peer_id'] for v in vm_rows.fold(recs).values()})
    assert peers == ['1', '2', '3']


def test_defaults_when_tags_are_missing():
    vm = vm_rows.fold([(NOW, {'vm_uuid': 'x', 'node_uuid': 'n'})])['x']
    assert (vm['name'], vm['node_peer_id'], vm['tag'], vm['vlan']) == ('Unknown', 'unknown', '', '0')


def test_current_cpu_is_the_newest_last_across_the_nodes_series():
    """A VM that migrated in the last 5 minutes has a last() on its old node too; the first table seen (the
    lowest node tag) gave its old node's CPU."""
    recs = [(NOW - timedelta(minutes=4), {'vm_uuid': 'a', 'node_uuid': 'node-1'}, 90.0),
            (NOW - timedelta(minutes=1), {'vm_uuid': 'a', 'node_uuid': 'node-3'}, 12.0),
            (NOW - timedelta(minutes=2), {'vm_uuid': 'b', 'node_uuid': 'node-2'}, 50.0)]
    assert vm_rows.newest_per(recs) == {'a': 12.0, 'b': 50.0}
    assert vm_rows.newest_per(list(reversed(recs))) == {'a': 12.0, 'b': 50.0}


def _vsd(minutes_ago, node, read=1.0):
    return NOW - timedelta(minutes=minutes_ago), {'vsd_uuid': 'd1', 'vsd_name': 'disk0', 'vsd_type': 'VIRTIO_DISK',
                                                  'vm_uuid': 'a', 'vm_name': 'alpha', 'node_uuid': node, 'read_kbps': read}


def test_a_disk_is_attributed_to_the_node_of_its_newest_sample():
    disk = vm_rows.fold_vsds([_vsd(120, 'node-1'), _vsd(60, 'node-1'), _vsd(30, 'node-3'), _vsd(5, 'node-3', 7.0)])['d1']
    assert disk['node_uuid'] == 'node-3' and (disk['name'], disk['type'], disk['vm_name']) == ('disk0', 'VIRTIO_DISK', 'alpha')
    times = [h['time'] for h in disk['history']]
    assert times == sorted(times) and disk['history'][-1]['read_kbps'] == 7.0
    assert set(disk['history'][0]) == {'time', 'read_kbps', 'write_kbps', 'read_latency_us', 'write_latency_us',
                                       'read_iops', 'write_iops', 'capacity_bytes'}


def test_bad_numbers_on_the_foresight_routes_are_a_400(dash, monkeypatch):
    import rbac
    from types import SimpleNamespace
    cid = 'cccccccc-0000-0000-0000-0000000000dd'
    grants = [rbac.Grant(rbac.expand(['cluster.view']), 'cluster', cluster_id=cid)]
    monkeypatch.setattr(dash.access_db, 'access_for', lambda uid, superuser=False: rbac.Access(grants, rbac.Tree(), user_id=uid))
    monkeypatch.setattr(dash.access_db, 'user_state', lambda uid: {'username': 'u@x', 'enabled': True})
    monkeypatch.setattr(dash, '_enabled_clusters', lambda: [SimpleNamespace(cluster_id=cid)])
    monkeypatch.setattr(dash, '_sentinel_query', lambda sql, params: [{'params': params}])
    c = dash.app.test_client()
    with c.session_transaction() as s:
        s['user_id'], s['username'] = 'u1', 'u@x'
    for path in ('forecasts?limit=x', 'forecasts?horizon=30d', 'rightsizing?limit=', 'resize?limit=1e3',
                 'exhaustion?limit=many', 'cluster-health?limit=-'):
        assert c.get(f'/api/sentinel/{path}').status_code == 400, path
    r = c.get('/api/sentinel/forecasts?horizon=90&limit=5000')
    assert r.status_code == 200 and r.get_json()[0]['params'][-2:] == [90, 1000]


def test_a_started_vm_is_running_although_its_old_shutoff_series_is_still_in_range():
    """Power state is a tag: right after a start both series are in the queried range. The one that came last
    won, and 'SHUTOFF' sorts after 'RUNNING': a VM started from New VM showed 'shutoff' for two minutes."""
    t0 = NOW - timedelta(seconds=40)
    recs = [(t0, 'num_vcpu', 2, {'vm_uuid': 'v', 'vm_state': 'SHUTOFF', 'vm_name': 'vd01'}),
            (NOW, 'num_vcpu', 4, {'vm_uuid': 'v', 'vm_state': 'RUNNING', 'vm_name': 'vd01'}),
            (t0, 'mem_bytes', 1, {'vm_uuid': 'v', 'vm_state': 'SHUTOFF', 'vm_name': 'vd01'}),
            (NOW, 'mem_bytes', 8, {'vm_uuid': 'v', 'vm_state': 'RUNNING', 'vm_name': 'vd01'})]
    for order in (recs, list(reversed(recs))):
        e = vm_rows.newest_inventory(order)['v']
        assert e['values']['vm_state'] == 'RUNNING' and e['fields'] == {'num_vcpu': 4, 'mem_bytes': 8} and e['time'] == NOW


def test_a_stopped_vm_is_stopped_although_its_running_series_is_still_in_range():
    recs = [(NOW - timedelta(seconds=30), 'num_vcpu', 2, {'vm_uuid': 'v', 'vm_state': 'RUNNING'}),
            (NOW, 'num_vcpu', 2, {'vm_uuid': 'v', 'vm_state': 'SHUTOFF'})]
    assert vm_rows.newest_inventory(recs)['v']['values']['vm_state'] == 'SHUTOFF'
