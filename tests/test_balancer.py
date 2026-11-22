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


# ── a balancer thread that cannot die on bad settings, and a supervisor that does not log in every minute ──

def test_a_window_shorter_than_one_sample_still_holds_one_sample(monkeypatch):
    """AVG_WINDOW_MINUTES=1 with SAMPLE_INTERVAL_SECONDS=120 (both allowed) gave a window of 0 samples:
    mean() of an empty deque raised and killed the thread."""
    monkeypatch.setattr(hb.config_db, 'get_all', lambda cid: {})
    lb = hb.LoadBalancer(client=object(), config=dict(CONFIG, AVG_WINDOW_MINUTES=1, SAMPLE_INTERVAL_SECONDS=120),
                         migration_logger=object())
    assert lb.max_history == 1
    state = feed(lb, [node('n1', '10.0.0.241', 10.0, 8)], [vm('v1', 'a', 'n1')], {'v1': 5.0}, samples=1)
    assert state['n1']['avg_cpu'] == pytest.approx(10.0)
    lb.config.update(AVG_WINDOW_MINUTES=1, SAMPLE_INTERVAL_SECONDS=300)
    lb.max_history = 10
    lb.reload_config()
    assert lb.max_history == 1, 'the hot reload uses the same floor'


def test_a_node_reporting_zero_threads_does_not_divide_by_zero(lb):
    nodes = [dict(node('n1', '10.0.0.241', 10.0, 8), numThreads=0)]
    state = feed(lb, nodes, [vm('v1', 'a', 'n1')], {})
    assert state['n1']['threads'] == 1


class _Logger:
    def __init__(self): self.events = []
    def log_event(self, **kw): self.events.append(kw)
    def close(self): pass


class _Client:
    """A cluster that answers with fixed nodes and VMs and records migrations."""
    def __init__(self, nodes, vms, cpu=None, fail_migrate=False):
        self.nodes, self.vms, self.cpu = nodes, vms, cpu or {}
        self.fail_migrate, self.migrations, self.task_states = fail_migrate, [], []
        self.closed = False
    def get_nodes(self): return self.nodes
    def get_vms(self): return self.vms
    def get_vm_stats(self): return stats(self.vms, self.cpu)
    def get_task_status(self, tag): return self.task_states.pop(0) if self.task_states else 'RUNNING'
    def migrate(self, vm_uuid, target):
        self.migrations.append((vm_uuid, target))
        if self.fail_migrate:
            raise RuntimeError('HTTP 500')
        return {'taskTag': f't{len(self.migrations)}'}
    def close(self): self.closed = True


def _balancer(client, **config):
    lb = hb.LoadBalancer(client=client, config={**CONFIG, 'PREDICTIVE_BALANCING_ENABLED': False, **config},
                         migration_logger=_Logger())
    lb._last_config_check = time.time() + 10 ** 6       # no hot reload in these tests
    return lb


def test_a_failing_cycle_does_not_end_the_thread():
    """An exception after the fetch used to escape the loop: the thread ended, logged out, and the
    supervisor started a new one with a new login."""
    client = _Client([node('n1', '10.0.0.241', 10.0, 8)], [])
    lb = _balancer(client)
    lb.max_history = 1
    calls, sleeps = [], []

    def boom(state):
        calls.append(1)
        raise ValueError('bad data')
    lb.select_candidate = boom

    def sleep(sec):
        sleeps.append(sec)
        if len(sleeps) >= 3:
            lb.stop.set()
    lb._sleep = sleep
    lb.start()
    assert len(calls) == 3, 'the loop went on after the failure'
    assert sleeps == [30, 30, 30]


class _T:
    def __init__(self, cluster, alive=True, started_at=0.0):
        self.cluster, self.alive, self.started_at = cluster, alive, started_at
    def is_alive(self): return self.alive


class _C:
    def __init__(self, cid, updated_at='u1'):
        self.cluster_id, self.name, self.updated_at = cid, cid, updated_at


def test_a_thread_that_keeps_dying_is_restarted_with_a_growing_backoff():
    """A thread that died (refused login, crash) was recreated on every 60 s registry pass: 60 logins an hour."""
    clusters = {'c1': _C('c1')}
    threads, backoff, started = {}, {}, []
    clock = {'now': 0.0}

    def start(cl, ev):
        started.append(clock['now'])
        return _T(cl, alive=False, started_at=clock['now'])      # dies at once, like a refused login
    for _ in range(60):                                            # one hour of registry passes
        hb.reconcile(threads, clusters, set(), backoff, start, clock['now'])
        clock['now'] += hb.REGISTRY_POLL_SECONDS
    assert len(started) <= 6, f'{len(started)} restarts (logins) in an hour'
    assert backoff['c1'][0] >= 4
    clusters['c1'] = _C('c1', updated_at='u2')                     # a new password is tried at once
    n = len(started)
    hb.reconcile(threads, clusters, set(), backoff, start, clock['now'])
    assert len(started) == n + 1


def test_a_healthy_thread_is_left_alone_and_a_removed_cluster_stopped():
    c = _C('c1'); threads, backoff = {}, {}
    hb.reconcile(threads, {'c1': c}, set(), backoff, lambda cl, ev: _T(cl), 0.0)
    t, ev = threads['c1']
    hb.reconcile(threads, {'c1': c}, set(), backoff, lambda cl, ev: pytest.fail('restarted'), 60.0)
    assert threads['c1'][0] is t
    hb.reconcile(threads, {}, set(), backoff, lambda cl, ev: pytest.fail('restarted'), 120.0)
    assert threads == {} and ev.is_set() and backoff == {}


# ── a migration task the cluster never reports on ──

def test_a_task_without_status_is_given_up_after_repeated_unknowns():
    """get_task_status answers UNKNOWN on any error and UNINITIALIZED for a tag the cluster does not know;
    such a task blocked the cluster's balancer forever."""
    client = _Client([node('n1', '10.0.0.241', 10.0, 8)], [])
    lb = _balancer(client)
    lb.active_task = {'tag': 't9', 'vm': 'a', 'started': time.time()}
    client.task_states = ['UNKNOWN'] * (hb.TASK_MAX_UNKNOWN - 1)
    for _ in range(hb.TASK_MAX_UNKNOWN - 1):
        assert lb._cycle() == 10
    assert lb.active_task is not None, 'a few unknowns are tolerated'
    client.task_states = ['RUNNING', 'UNINITIALIZED']
    lb._cycle()
    assert lb.active_task['unknown'] == 0, 'a real answer resets the count'
    client.task_states = ['UNINITIALIZED'] * hb.TASK_MAX_UNKNOWN
    for _ in range(hb.TASK_MAX_UNKNOWN):
        lb._cycle()
    assert lb.active_task is None
    assert lb.last_migration_time > 0, 'the cooldown starts, then placement is read afresh'


def test_a_task_running_past_the_deadline_is_given_up():
    client = _Client([node('n1', '10.0.0.241', 10.0, 8)], [])
    lb = _balancer(client)
    lb.active_task = {'tag': 't9', 'vm': 'a', 'started': time.time() - hb.TASK_DEADLINE_SECONDS - 1}
    client.task_states = ['RUNNING']
    lb._cycle()
    assert lb.active_task is None


# ── the affinity pass ──

def _pin_violation():
    nodes = [node('n1', '10.0.0.241', 10.0, 8), node('n2', '10.0.0.242', 10.0, 8)]
    return nodes, [vm('v1', 'pinned', 'n2', tags='node_241')]


def test_a_refused_affinity_move_is_not_logged_as_done_and_waits_before_retrying():
    """After the except the code fell through to log_event(dry_run=False), and cooldowns do not apply to
    affinity: 3 cycles gave 3 POSTs and 3 'moved' rows."""
    nodes, vms = _pin_violation()
    client = _Client(nodes, vms, fail_migrate=True)
    lb = _balancer(client, DRY_RUN=False)
    for _ in range(3):
        lb._cycle()
    assert len(client.migrations) == 1, 'one attempt, then a backoff of minutes'
    assert lb.migration_logger.events == [], 'a refused move is not logged as a migration'
    n, until = lb._affinity_backoff['v1']
    assert n == 1 and until - time.time() > 200
    lb._affinity_backoff['v1'] = (1, time.time() - 1)           # backoff over: one more attempt, longer wait
    lb._cycle()
    assert len(client.migrations) == 2 and lb._affinity_backoff['v1'][0] == 2
    client.fail_migrate = False
    lb._affinity_backoff['v1'] = (2, time.time() - 1)
    lb._cycle()
    assert len(client.migrations) == 3 and 'v1' not in lb._affinity_backoff
    assert [e['dry_run'] for e in lb.migration_logger.events] == [False]


def test_a_dry_run_violation_is_reported_once_and_the_other_engines_still_run():
    """In dry-run one violation logged 'would move' every cycle (~2,880 rows a day) and skipped the
    reactive and predictive engines for good."""
    nodes, vms = _pin_violation()
    client = _Client(nodes, vms)
    lb = _balancer(client, DRY_RUN=True)
    lb.max_history = 1
    reactive = []
    real = lb.select_candidate
    lb.select_candidate = lambda state: (reactive.append(1), real(state))[1]
    for _ in range(5):
        lb._cycle()
    assert len(lb.migration_logger.events) == 1 and lb.migration_logger.events[0]['dry_run'] is True
    assert client.migrations == []
    assert len(reactive) == 5, 'the reactive engine ran in every cycle'
    lb._affinity_reported['v1'] = time.time() - hb.AFFINITY_DRY_REPORT_SECONDS - 1
    lb._cycle()
    assert len(lb.migration_logger.events) == 2, 'a lasting violation is mentioned again after a day'


def test_a_skipped_vm_lets_the_next_violation_through(lb):
    nodes = [node('n1', '10.0.0.241', 10.0, 8), node('n2', '10.0.0.242', 10.0, 8)]
    vms = [vm('v1', 'p1', 'n2', tags='node_241'), vm('v2', 'p2', 'n2', tags='node_241')]
    state = feed(lb, nodes, vms, {})
    usable = [u for u in state.values() if u['is_usable']]
    first = lb.find_affinity_violation(usable)[0]['uuid']
    second = lb.find_affinity_violation(usable, {first})[0]['uuid']
    assert {first, second} == {'v1', 'v2'}
    assert lb.find_affinity_violation(usable, {'v1', 'v2'}) == (None, None, None, None)


# ── Oracle recommendations meet the reactive engine's target test when they run ──

class _Rec:
    def __init__(self, vm_uuid, vm_name, source, target, source_ip='10.0.0.241', target_ip='10.0.0.242', in_s=60):
        self.vm_uuid, self.vm_name, self.source_node, self.target_node = vm_uuid, vm_name, source, target
        self.source_ip, self.target_ip, self.reason = source_ip, target_ip, 'test'
        self.peak_time = time.time() + in_s


class _DoneThread:
    def is_alive(self): return False


def _oracle(nodes, vms, recs, **config):
    client = _Client(nodes, vms)
    lb = _balancer(client, DRY_RUN=False, PREDICTIVE_BALANCING_ENABLED=True, PREDICTIVE_LEAD_TIME_HOURS=1,
                   PREDICTIVE_INTERVAL_SECONDS=10 ** 6, **config)
    lb.last_predictive_run = time.time()
    lb.max_history = 10 ** 6                                   # the reactive engine waits for its buffer
    lb._predictive_thread, lb._predictive_results = _DoneThread(), recs
    return client, lb


@pytest.mark.parametrize('target_node,why', [
    (node('n2', '10.0.0.242', 0.0, 1, allow=False), 'maintenance (allowRunningVMs false)'),
    (node('n2', '10.0.0.242', 0.0, 1, online=False), 'offline'),
    (node('n2', '10.0.0.242', 0.0, 63), 'only 1 GiB free'),
    (node('n2', '10.0.0.242', 0.0, 1, threads=2), 'vCPU ratio'),
])
def test_an_oracle_move_to_a_node_that_cannot_take_the_vm_is_dropped(target_node, why):
    """The Oracle picked 'every node except the source' by predicted load alone; an empty maintenance node
    (predicted 0) was its first choice, and the main loop only checked that the uuid still existed."""
    nodes = [node('n1', '10.0.0.241', 90.0, 8), target_node]
    vms = [vm('v1', 'a', 'n1', mem_gb=4, vcpus=4), vm('v2', 'b', 'n2', vcpus=2)]
    client, lb = _oracle(nodes, vms, [_Rec('v1', 'a', 'n1', 'n2')])
    lb._cycle()
    assert client.migrations == [], why
    assert lb._pending_predictive_recs == []


def test_an_oracle_move_against_exclusion_pin_or_anti_affinity_is_dropped():
    nodes = [node('n1', '10.0.0.241', 90.0, 8), node('n2', '10.0.0.242', 10.0, 8), node('n3', '10.0.0.243', 10.0, 8)]
    vms = [vm('v1', 'a', 'n1', tags='node_243'), vm('v2', 'dc01', 'n1', tags='anti_dc02'), vm('v3', 'dc02', 'n2'),
           vm('v4', 'c', 'n1')]
    recs = [_Rec('v1', 'a', 'n1', 'n2'), _Rec('v2', 'dc01', 'n1', 'n2'), _Rec('v4', 'c', 'n1', 'n3', target_ip='10.0.0.243')]
    client, lb = _oracle(nodes, vms, recs, EXCLUDE_NODE_IPS=['10.0.0.243'])
    lb._cycle()
    assert client.migrations == []


def test_a_valid_oracle_move_still_runs():
    nodes = [node('n1', '10.0.0.241', 90.0, 8), node('n2', '10.0.0.242', 10.0, 8)]
    vms = [vm('v1', 'a', 'n1')]
    client, lb = _oracle(nodes, vms, [_Rec('v1', 'a', 'n1', 'n2')])
    lb._cycle()
    assert client.migrations == [('v1', 'n2')]


def test_a_clean_forecast_clears_the_queue():
    """The queue was only replaced when the new forecast had recommendations: stale ones survived a clean one."""
    nodes = [node('n1', '10.0.0.241', 90.0, 8), node('n2', '10.0.0.242', 10.0, 8)]
    vms = [vm('v1', 'a', 'n1')]
    client, lb = _oracle(nodes, vms, [])
    lb._pending_predictive_recs = [_Rec('v1', 'a', 'n1', 'n2', in_s=7200)]   # outside the lead time
    lb._cycle()
    assert lb._pending_predictive_recs == []


def test_the_forecast_gets_only_usable_nodes(monkeypatch):
    nodes = [node('n1', '10.0.0.241', 50.0, 8), node('n2', '10.0.0.242', 0.0, 1, allow=False),
             node('n3', '10.0.0.243', 10.0, 8)]
    client, lb = _oracle(nodes, [vm('v1', 'a', 'n1')], None, EXCLUDE_NODE_IPS=['10.0.0.243'])
    lb._predictive_thread = None
    lb.last_predictive_run = 0
    started = {}

    class Thread:
        def __init__(self, target, args, **kw): started['nodes'] = args[1]
        def start(self): pass
        def is_alive(self): return True
    monkeypatch.setattr(hb.threading, 'Thread', Thread)
    lb._cycle()
    assert [n['uuid'] for n in started['nodes']] == ['n1']
