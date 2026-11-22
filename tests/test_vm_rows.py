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
