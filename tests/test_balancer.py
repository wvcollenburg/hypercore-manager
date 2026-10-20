"""Balancer/HyperCore_balancer.py — cluster evaluation and candidate selection with a mocked cluster.

No HyperCore, InfluxDB or Postgres involved: the API client and migration logger are
stand-ins, and config comes straight from a dict.
"""
import time

import pytest

import HyperCore_balancer as hb


CONFIG = {
    'DRY_RUN': True,
    'VERBOSITY': 0,
    'AVG_WINDOW_MINUTES': 5,
    'SAMPLE_INTERVAL_SECONDS': 30,
    'RAM_LIMIT_PERCENT': 85.0,
    'RAM_UPPER_THRESHOLD_PERCENT': 65.0,
    'CPU_UPPER_THRESHOLD_PERCENT': 50.0,
    'MAX_VCPU_RATIO': 2.0,
    'VM_MOVE_COOLDOWN_MINUTES': 30,
    'MIGRATION_COOLDOWN_MINUTES': 5,
    'EXCLUDE_NODE_IPS': [],
    'CPU_TARGET_TOLERANCE_PERCENT': 5.0,
}

GB = 1024 ** 3


def node(uuid, ip, cpu, used_gb, total_gb=64, threads=16, online=True, allow=True):
    return {
        'uuid': uuid, 'lanIP': ip, 'cpuUsage': cpu,
        'totalMemUsageBytes': used_gb * GB, 'memSize': total_gb * GB,
        'numThreads': threads,
        'networkStatus': 'ONLINE' if online else 'OFFLINE',
        'allowRunningVMs': allow,
    }


def vm(uuid, name, node_uuid, mem_gb=4, vcpus=2, tags='', state='RUNNING'):
    return {'uuid': uuid, 'name': name, 'nodeUUID': node_uuid, 'mem': mem_gb * GB,
            'numVCPU': vcpus, 'tags': tags, 'state': state}


def stats(vms, cpu_by_uuid):
    return [{'uuid': v['uuid'], 'cpuUsage': cpu_by_uuid.get(v['uuid'], 0.0)} for v in vms]


@pytest.fixture
def lb():
    balancer = hb.LoadBalancer(client=object(), config=dict(CONFIG), migration_logger=object())
    return balancer


def feed(lb, nodes, vms, vm_cpu, samples=3):
    """Push identical samples so the sliding-window averages equal the inputs."""
    for _ in range(samples):
        lb.update_metrics(nodes, stats(vms, vm_cpu))
    return lb.evaluate_cluster(nodes, vms)


def test_window_size_derived_from_config(lb):
    assert lb.max_history == (5 * 60) // 30 == 10


def test_evaluate_cluster_aggregates_per_node(lb):
    nodes = [node('n1', '10.0.0.241', 70.0, 32), node('n2', '10.0.0.242', 10.0, 8)]
    vms = [vm('v1', 'web', 'n1', vcpus=4), vm('v2', 'db', 'n1', vcpus=4), vm('v3', 'idle', 'n2'),
           vm('v4', 'off', 'n1', state='SHUTOFF')]
    state = feed(lb, nodes, vms, {'v1': 60.0, 'v2': 20.0})
    n1 = state['n1']
    assert n1['name'] == '10.0.0.241'
    assert n1['avg_cpu'] == pytest.approx(70.0)
    assert n1['avg_ram'] == pytest.approx(50.0)
    assert [v['name'] for v in n1['vms']] == ['db', 'web'], 'VMs sorted lightest first'
    assert 'off' not in [v['name'] for v in n1['vms']], 'non-running VMs ignored'
    assert n1['v_ratio'] == pytest.approx(8 / 16)
    assert n1['is_usable'] and state['n2']['is_usable']


def test_excluded_and_offline_nodes_are_unusable(lb):
    lb.config['EXCLUDE_NODE_IPS'] = ['10.0.0.243']
    nodes = [node('n1', '10.0.0.241', 10.0, 8), node('n2', '10.0.0.242', 10.0, 8, online=False),
             node('n3', '10.0.0.243', 10.0, 8)]
    state = feed(lb, nodes, [], {})
    assert state['n1']['is_usable']
    assert not state['n2']['is_usable']
    assert not state['n3']['is_usable'] and state['n3']['is_excluded']


def test_no_candidate_when_cluster_is_calm(lb):
    nodes = [node('n1', '10.0.0.241', 30.0, 16), node('n2', '10.0.0.242', 20.0, 16)]
    vms = [vm('v1', 'a', 'n1'), vm('v2', 'b', 'n2')]
    state = feed(lb, nodes, vms, {'v1': 10.0, 'v2': 10.0})
    cand, src, tgt, mode = lb.select_candidate(state)
    assert cand is None and mode == 'RAM_FOCUS'


def test_cpu_focus_moves_lightest_vm_from_hot_node_to_cool_node(lb):
    nodes = [node('n1', '10.0.0.241', 80.0, 20), node('n2', '10.0.0.242', 15.0, 10)]
    vms = [vm('v1', 'heavy', 'n1'), vm('v2', 'light', 'n1')]
    state = feed(lb, nodes, vms, {'v1': 60.0, 'v2': 5.0})
    cand, src, tgt, mode = lb.select_candidate(state)
    assert mode == 'CPU_FOCUS'
    assert cand['name'] == 'light', 'lightest VM goes first'
    assert src['name'] == '10.0.0.241' and tgt['name'] == '10.0.0.242'


def test_pinned_vm_is_never_moved_off_its_node(lb):
    nodes = [node('n1', '10.0.0.241', 80.0, 20), node('n2', '10.0.0.242', 15.0, 10)]
    vms = [vm('v1', 'pinned', 'n1', tags='node_241')]
    state = feed(lb, nodes, vms, {'v1': 5.0})
    cand, *_ = lb.select_candidate(state)
    assert cand is None


def test_vm_in_cooldown_is_skipped(lb):
    nodes = [node('n1', '10.0.0.241', 80.0, 20), node('n2', '10.0.0.242', 15.0, 10)]
    vms = [vm('v1', 'recent', 'n1')]
    lb.vm_cooldowns['v1'] = time.time()
    state = feed(lb, nodes, vms, {'v1': 5.0})
    cand, *_ = lb.select_candidate(state)
    assert cand is None


def test_anti_affinity_blocks_target_with_partner(lb):
    nodes = [node('n1', '10.0.0.241', 80.0, 20), node('n2', '10.0.0.242', 15.0, 10)]
    vms = [vm('v1', 'dc01', 'n1', tags='anti_dc02'), vm('v2', 'dc02', 'n2')]
    state = feed(lb, nodes, vms, {'v1': 5.0, 'v2': 5.0})
    cand, *_ = lb.select_candidate(state)
    assert cand is None, 'only target hosts the anti-affinity partner'


def test_reverse_anti_affinity_is_honoured(lb):
    """dc02 on the target carries anti_dc01; dc01 must not land there."""
    nodes = [node('n1', '10.0.0.241', 80.0, 20), node('n2', '10.0.0.242', 15.0, 10)]
    vms = [vm('v1', 'dc01', 'n1'), vm('v2', 'dc02', 'n2', tags='anti_dc01')]
    state = feed(lb, nodes, vms, {'v1': 5.0, 'v2': 5.0})
    cand, *_ = lb.select_candidate(state)
    assert cand is None


def test_target_ram_ceiling_respected(lb):
    nodes = [node('n1', '10.0.0.241', 80.0, 20, total_gb=64),
             node('n2', '10.0.0.242', 15.0, 52, total_gb=64)]      # 81% used
    vms = [vm('v1', 'big', 'n1', mem_gb=8)]                       # would push n2 to 93%
    state = feed(lb, nodes, vms, {'v1': 5.0})
    cand, *_ = lb.select_candidate(state)
    assert cand is None


def test_target_vcpu_ratio_respected(lb):
    nodes = [node('n1', '10.0.0.241', 80.0, 20), node('n2', '10.0.0.242', 15.0, 10, threads=4)]
    vms = [vm('v1', 'a', 'n1', vcpus=2), vm('v2', 'b', 'n2', vcpus=8)]  # n2 already at 2.0
    state = feed(lb, nodes, vms, {'v1': 5.0, 'v2': 5.0})
    cand, *_ = lb.select_candidate(state)
    assert cand is None


def test_reserved_target_is_skipped_until_reservation_expires(lb):
    nodes = [node('n1', '10.0.0.241', 80.0, 20), node('n2', '10.0.0.242', 15.0, 10)]
    vms = [vm('v1', 'a', 'n1')]
    lb.reserved_nodes['n2'] = time.time() + 300
    state = feed(lb, nodes, vms, {'v1': 5.0})
    assert lb.select_candidate(state)[0] is None
    lb.reserved_nodes['n2'] = time.time() - 1
    assert lb.select_candidate(state)[0]['name'] == 'a'
    assert 'n2' not in lb.reserved_nodes, 'expired reservation is cleared'


def test_ram_focus_moves_vm_when_ram_hot_and_cpu_calm(lb):
    nodes = [node('n1', '10.0.0.241', 20.0, 48, total_gb=64),   # 75% RAM
             node('n2', '10.0.0.242', 10.0, 8, total_gb=64)]
    vms = [vm('v1', 'a', 'n1', mem_gb=8), vm('v2', 'b', 'n1', mem_gb=4)]
    state = feed(lb, nodes, vms, {'v1': 5.0, 'v2': 5.0})
    cand, src, tgt, mode = lb.select_candidate(state)
    assert mode == 'RAM_FOCUS'
    assert cand is not None and tgt['name'] == '10.0.0.242'


def test_cpu_crisis_never_falls_through_to_ram_focus(lb):
    """With a hot CPU node but no viable CPU move, the balancer must not spend the
    cooldown on a RAM move instead."""
    nodes = [node('n1', '10.0.0.241', 90.0, 48, total_gb=64),
             node('n2', '10.0.0.242', 95.0, 8, total_gb=64)]    # hotter than the source: not a valid target
    vms = [vm('v1', 'a', 'n1', mem_gb=8)]
    state = feed(lb, nodes, vms, {'v1': 5.0})
    cand, _, _, mode = lb.select_candidate(state)
    assert cand is None and mode == 'CPU_FOCUS'


def test_affinity_violation_moves_pinned_vm_home(lb):
    nodes = [node('n1', '10.0.0.241', 10.0, 8), node('n2', '10.0.0.242', 10.0, 8)]
    vms = [vm('v1', 'pinned', 'n2', tags='node_241')]
    state = feed(lb, nodes, vms, {'v1': 5.0})
    usable = [u for u in state.values() if u['is_usable']]
    result = lb.find_affinity_violation(usable)
    assert result is not None
    moved_vm, src, tgt, *_ = result
    assert moved_vm['name'] == 'pinned'
    assert src['name'] == '10.0.0.242' and tgt['name'] == '10.0.0.241'


def test_affinity_violation_none_when_compliant(lb):
    nodes = [node('n1', '10.0.0.241', 10.0, 8), node('n2', '10.0.0.242', 10.0, 8)]
    vms = [vm('v1', 'pinned', 'n1', tags='node_241'), vm('v2', 'dc01', 'n1', tags='anti_dc02'),
           vm('v3', 'dc02', 'n2')]
    state = feed(lb, nodes, vms, {})
    usable = [u for u in state.values() if u['is_usable']]
    assert lb.find_affinity_violation(usable) == (None, None, None, None)
