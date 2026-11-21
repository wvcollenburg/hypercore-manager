"""backup/testrestore.py: test restores (plans/PLAN-test-restores.md, T1 + T2) with fakes for the database, the
clusters, the restore path, the console service and the proxy."""
import importlib
import os
import sys
import threading
import types
from datetime import datetime, timedelta, timezone

import pytest

_BACKUP = os.path.join(os.path.dirname(__file__), '..', 'backup')
_CLASH = ('config', 'db', 'log', 'api', 'hypercore', 'jobs', 'scheduler', 'incremental', 'proxy_client', 'manifest_names',
          'recurrence', 'verifier', 'policies', 'planner', 'predict', 'testrestore', 'restore', 'vm_definition')


def _import():
    saved = {n: sys.modules.pop(n) for n in _CLASH if n in sys.modules}
    sys.path.insert(0, _BACKUP)
    try:
        tr = importlib.import_module('testrestore')
        hcm = importlib.import_module('hypercore')
    finally:
        sys.path.remove(_BACKUP)
        for n in _CLASH:
            sys.modules.pop(n, None)
        sys.modules.update(saved)
    return tr, hcm


tr, hcm = _import()
UTC = timezone.utc
NOW = datetime(2026, 11, 22, 2, 0, tzinfo=UTC)


# ------------------------------------------------------------------ pure rules
def test_memory_fits_respects_the_ceiling_and_skips_witness_nodes():
    nodes = [{'memSize': 64 << 30, 'totalMemUsageBytes': 50 << 30, 'allowRunningVMs': True},
             {'memSize': 64 << 30, 'totalMemUsageBytes': 1 << 30, 'allowRunningVMs': False}]
    assert not tr.memory_fits(nodes, 8 << 30, 85)          # 58/64 = 91 % > 85 %; the empty node is a witness
    assert tr.memory_fits(nodes, 4 << 30, 85)              # 54/64 = 84 %
    assert tr.max_free_memory(nodes) == 14 << 30


def test_judge_checks_ports_decide_and_ping_only_without_ports():
    assert tr.judge_checks([{'ip': '10.0.0.5', 'ping': False, 'ports': {3389: True, 443: True}}])[0]
    ok, why = tr.judge_checks([{'ip': '10.0.0.5', 'ping': True, 'ports': {3389: True, 1433: False}}])
    assert not ok and 'port(s) 1433 closed' in why
    assert tr.judge_checks([{'ip': '10.0.0.5', 'ping': True, 'ports': {}}])[0]
    assert not tr.judge_checks([{'ip': '10.0.0.5', 'ping': False, 'ports': {}}])[0]
    assert tr.judge_checks([{'ip': '10.0.0.5', 'ping': False, 'ports': {443: False}},
                            {'ip': '10.0.1.5', 'ping': False, 'ports': {443: True}}])[0], 'one address answering is enough'


class FakeDB:
    def __init__(self):
        self.members = []
        self.backed_up = []
        self.runs = {}
        self.alerts = []
        self.resolved = []
        self.restores = {}
        self.clusters = [{'cluster_id': 'prod', 'cluster_name': 'Prod', 'role': 'production', 'timezone': 'UTC'}]
        self.proxies = {'prod': [{'id': 1, 'name': 'px', 'vm_uuid': 'pxvm', 'cluster_id': 'prod', 'capabilities': {}}]}

    def test_plan_vms(self, pid): return self.members
    def backed_up_vms(self, f): return self.backed_up
    def folder_clusters(self, f): return self.clusters
    def get_proxies(self, cid, enabled_only=False): return self.proxies.get(cid, [])
    def get_proxy(self, pid, with_secret=False): return next(p for ps in self.proxies.values() for p in ps if p['id'] == pid)
    def cluster_role(self, cid): return next((c['role'] for c in self.clusters if c['cluster_id'] == cid), 'production')
    def update_test_run(self, rid, **f): self.runs.setdefault(rid, {}).update(f)
    def ram_limit_percent(self, cid): return 85.0
    def get_chain(self, cid): return {'id': cid, 'cluster_id': 'prod', 'target_id': 9, 'vm_name': 'dc01', 'folder_slug': 'F', 'chain_dir': 'F/dc01.chain'}
    def get_share(self, sid, with_secret=False): return {'uri_base': 'smb://10.0.0.20/backups'}
    def add_restore(self, point, chain, name, params, requested_by=None, cluster_id=None):
        rid = len(self.restores) + 1
        self.restores[rid] = {'id': rid, 'target_name': name, 'params': params, 'cluster_id': cluster_id, 'requested_by': requested_by}
        return rid
    def get_restore(self, rid): return self.restores.get(rid)
    def raise_test_alert(self, kind, folder, cid, vm, name, detail): self.alerts.append((kind, vm, detail)); return True
    def resolve_test_alerts(self, cid, vm, kind=None): self.resolved.append(vm); return 1


class FakeHC:
    def __init__(self, vms=None, nodes=None, boot=None):
        self.vms = {v['uuid']: v for v in (vms or [])}
        self.nodes = nodes or [{'memSize': 64 << 30, 'totalMemUsageBytes': 8 << 30, 'allowRunningVMs': True}]
        self.boot = list(boot or [])            # successive states of the restored VM
        self.actions = []

    def list_vms_full(self): return list(self.vms.values())
    def list_nodes(self): return self.nodes
    def get_vm(self, u):
        if u == 'restored' and self.boot:
            v = self.boot.pop(0) if len(self.boot) > 1 else self.boot[0]
            return dict(v, uuid='restored', name='dc01-hcmtest-1', tags='windows,hcm-testrestore')
        if u not in self.vms:
            raise hcm.HyperCoreError('not found')
        return self.vms[u]
    def vm_action(self, u, a): self.actions.append((a, u)); return ''
    def delete_vm(self, u): self.actions.append(('DELETE', u)); self.vms.pop(u, None); return ''


@pytest.fixture
def world(monkeypatch):
    db = FakeDB()
    src = FakeHC(vms=[{'uuid': 'vm1', 'name': 'dc01', 'state': 'RUNNING', 'guestAgentState': 'AVAILABLE',
                       'netDevs': [{'vlan': 10, 'ipv4Addresses': ['10.0.0.5']}]}])
    tgt = FakeHC(boot=[{'state': 'RUNNING', 'guestAgentState': 'UNAVAILABLE'}, {'state': 'RUNNING', 'guestAgentState': 'AVAILABLE'}])
    clients = {'prod': src, 'dr': tgt}
    monkeypatch.setattr(tr, 'db', db)
    monkeypatch.setattr(tr, 'jobs', types.SimpleNamespace(client_for=lambda c: clients[c], preflight=lambda hc: {'ok': True}))
    monkeypatch.setattr(tr.incremental, 'wait_task', lambda *a, **k: None)
    restore_mod = types.SimpleNamespace(run_restore=lambda rid: db.restores[rid].update(
        status='COMPLETE', created_uuid='restored', message="restored as 'dc01-hcmtest-1' (1 disk(s), 2048 MiB written)"))
    monkeypatch.setitem(sys.modules, 'restore', restore_mod)
    monkeypatch.setattr(tr, 'console_bootcheck', lambda cid, vm: {'verdict': 'wait', 'detail': 'boot logo', 'png': None})
    tr._running.clear()
    return types.SimpleNamespace(db=db, src=src, tgt=tgt, clients=clients)


PLAN = {'id': 3, 'name': 'Weekly', 'folder_id': 'F1', 'level': 'boot', 'timeout_minutes': 10, 'keep_failed_hours': None,
        'test_vlan': None, 'probe_address': None, 'default_ports': [], 'random_count': 0, 'rest_weeks': 4}
RUN = {'id': 1, 'cluster_id': 'prod', 'vm_uuid': 'vm1', 'vm_name': 'dc01'}
POINT = {'id': 50, 'chain_id': 7, 'created_at': NOW - timedelta(hours=5), 'restore_bytes': 2 << 30,
         'definition': {'mem': 4 << 30, 'netDevs': [{'ipv4Addresses': ['10.0.0.5']}]}}


class Clock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t
    def sleep(self, s): self.t += s


# ------------------------------------------------------------------ rounds
def test_a_round_takes_the_fixed_vms_and_the_longest_untested_from_the_pool(world):
    db = world.db
    db.members = [{'cluster_id': 'prod', 'vm_uuid': 'dc', 'vm_name': 'dc01', 'mode': 'fixed', 'ports': None},
                  {'cluster_id': 'prod', 'vm_uuid': 'noisy', 'vm_name': 'build', 'mode': 'exclude', 'ports': None}]
    db.backed_up = [{'cluster_id': 'prod', 'vm_uuid': 'dc', 'vm_name': 'dc01', 'last_tested': None},
                    {'cluster_id': 'prod', 'vm_uuid': 'noisy', 'vm_name': 'build', 'last_tested': None},
                    {'cluster_id': 'prod', 'vm_uuid': 'a', 'vm_name': 'app', 'last_tested': NOW - timedelta(days=60)},
                    {'cluster_id': 'prod', 'vm_uuid': 'b', 'vm_name': 'sql', 'last_tested': None},
                    {'cluster_id': 'prod', 'vm_uuid': 'c', 'vm_name': 'web', 'last_tested': NOW - timedelta(days=3)}]
    picked = tr.select_vms(dict(PLAN, random_count=2), NOW)
    assert [(v['vm_name'], v['picked']) for v in picked] == [('dc01', 'fixed'), ('sql', 'random'), ('app', 'random')]
    # web was tested 3 days ago: it rests 4 weeks; build is excluded; dc01 is fixed, never also random


def test_the_target_is_the_dr_cluster_when_the_folder_has_one_with_a_proxy(world):
    db = world.db
    assert tr.target_for('F1', 'prod') == ('prod', 'production')
    db.clusters.append({'cluster_id': 'dr', 'cluster_name': 'DR', 'role': 'dr', 'timezone': 'UTC'})
    assert tr.target_for('F1', 'prod') == ('prod', 'production'), 'a DR cluster without a proxy cannot restore'
    db.proxies['dr'] = [{'id': 2, 'name': 'px-dr', 'vm_uuid': 'p2', 'cluster_id': 'dr'}]
    assert tr.target_for('F1', 'prod') == ('dr', 'dr')


# ------------------------------------------------------------------ one test
def test_a_boot_test_restores_disconnected_passes_on_the_agent_and_deletes_the_vm(world):
    c = Clock()
    assert tr.run_test(RUN, PLAN, 'dr', POINT, sleep=c.sleep, clock=c) == 'passed'
    r = world.db.restores[1]
    assert r['cluster_id'] == 'dr' and r['target_name'] == 'dc01-hcmtest-1' and r['requested_by'] == 'test restore'
    assert r['params']['nic_connected'] is False and r['params']['mac_policy'] == 'new' and r['params']['start'] is True
    assert 'hcm-testrestore' in r['params']['extra_tags'] and 'vlan' not in r['params']
    run = world.db.runs[1]
    assert run['status'] == 'passed' and run['verdict'] == 'agent' and run['agent'] == 'available' and run['bytes'] == 2048 << 20
    assert ('STOP', 'restored') in world.tgt.actions and ('DELETE', 'restored') in world.tgt.actions and run['cleaned_at']
    assert world.db.resolved == ['vm1'] and world.db.alerts == []


def test_without_an_agent_the_console_screen_decides(world, monkeypatch):
    world.tgt.boot = [{'state': 'RUNNING', 'guestAgentState': 'UNAVAILABLE'}]
    screens = iter([{'verdict': 'wait', 'detail': 'boot logo', 'png': None},
                    {'verdict': 'pass', 'detail': 'lock screen', 'png': 'iVBORw0K'}])
    monkeypatch.setattr(tr, 'console_bootcheck', lambda cid, vm: next(screens))
    c = Clock()
    assert tr.run_test(RUN, PLAN, 'dr', POINT, sleep=c.sleep, clock=c) == 'passed'
    run = world.db.runs[1]
    assert run['verdict'] == 'screen' and 'lock screen' in run['detail'] and run['screenshot']


def test_a_stop_screen_or_no_sign_of_life_fails_raises_an_alert_and_can_keep_the_vm(world, monkeypatch):
    world.tgt.boot = [{'state': 'RUNNING', 'guestAgentState': 'UNAVAILABLE'}]
    monkeypatch.setattr(tr, 'console_bootcheck', lambda cid, vm: {'verdict': 'fail', 'detail': 'BitLocker recovery screen', 'png': 'iVBO'})
    c = Clock()
    assert tr.run_test(RUN, dict(PLAN, keep_failed_hours=24), 'dr', POINT, sleep=c.sleep, clock=c) == 'failed'
    run = world.db.runs[1]
    assert run['verdict'] == 'screen' and 'BitLocker' in run['detail'] and run['keep_until']
    assert ('DELETE', 'restored') not in world.tgt.actions, 'kept for inspection'
    assert world.db.alerts and world.db.alerts[0][0] == 'test_failed'
    world.db.runs.clear(); world.db.alerts.clear()
    monkeypatch.setattr(tr, 'console_bootcheck', lambda cid, vm: {'verdict': 'wait', 'detail': 'boot logo', 'png': None})
    c = Clock()
    assert tr.run_test(RUN, PLAN, 'dr', POINT, sleep=c.sleep, clock=c) == 'failed'
    assert world.db.runs[1]['verdict'] == 'timeout' and 'within 10 min' in world.db.runs[1]['detail']
    assert ('DELETE', 'restored') in world.tgt.actions, 'not kept: the plan does not ask for it'


def test_not_enough_memory_skips_without_restoring(world):
    world.tgt.nodes = [{'memSize': 16 << 30, 'totalMemUsageBytes': 13 << 30, 'allowRunningVMs': True}]
    assert tr.run_test(RUN, PLAN, 'dr', POINT) == 'skipped'
    assert world.db.restores == {} and 'not enough free memory' in world.db.runs[1]['detail']


def test_a_failed_restore_is_a_failed_test_and_its_half_made_vm_is_removed(world, monkeypatch):
    db = world.db
    sys.modules['restore'].run_restore = lambda rid: db.restores[rid].update(status='ERROR', created_uuid='restored',
                                                                              message='disk could not be attached')
    assert tr.run_test(RUN, PLAN, 'dr', POINT) == 'failed'
    assert db.runs[1]['verdict'] == 'restore' and 'could not be attached' in db.runs[1]['detail']
    assert ('DELETE', 'restored') in world.tgt.actions


# ------------------------------------------------------------------ network level
NET = dict(PLAN, level='network', test_vlan=999, probe_address='10.0.0.250/24', default_ports=[3389])


def test_the_network_level_refuses_a_vlan_in_use_and_needs_a_capable_proxy(world):
    world.tgt.vms = {'prodvm': {'uuid': 'prodvm', 'name': 'erp', 'tags': '', 'netDevs': [{'vlan': 999}]}}
    world.db.proxies['dr'] = [{'id': 2, 'name': 'px-dr', 'vm_uuid': 'p2', 'cluster_id': 'dr', 'capabilities': {}}]
    assert tr.run_test(RUN, NET, 'dr', POINT) == 'failed'
    assert 'in use on the target cluster by erp' in world.db.runs[1]['detail'] and world.db.restores == {}
    world.tgt.vms = {}
    world.db.runs.clear()
    assert tr.run_test(RUN, NET, 'dr', POINT) == 'failed'
    assert 'no proxy on the target cluster can test VLAN 999' in world.db.runs[1]['detail']


def test_a_network_test_restores_onto_the_test_vlan_and_checks_ports_through_the_proxy(world, monkeypatch):
    world.db.proxies['dr'] = [{'id': 2, 'name': 'px-dr', 'vm_uuid': 'p2', 'cluster_id': 'dr', 'url': 'https://10.0.0.60:8083',
                               'capabilities': {'host_network': True, 'jobs': ['netcheck']}}]
    world.tgt.vms = {'p2': {'uuid': 'p2', 'name': 'px-dr', 'tags': 'hcm-proxy',
                            'netDevs': [{'vlan': 10}, {'vlan': 999, 'macAddress': '7C:4C:58:00:00:99'}]}}
    sent = []

    class PC:
        def submit(self, kind, body): sent.append((kind, body)); return 'j1'
        def wait(self, j, timeout=0):
            return {'result': {'results': {'10.0.0.5': {'ping': False, 'ports': {3389: True}}}}}
    monkeypatch.setattr(tr.incremental, 'proxy_client_for', lambda p: PC())
    world.tgt.boot = [dict(b, netDevs=[{'vlan': 999, 'macAddress': '7C:4C:58:AA:BB:CC'}]) for b in world.tgt.boot]
    c = Clock()
    assert tr.run_test(RUN, NET, 'dr', POINT, sleep=c.sleep, clock=c) == 'passed'
    params = world.db.restores[1]['params']
    assert params['vlan'] == 999 and params['nic_connected'] is True and params['start'] is False, 'started after the DHCP leases'
    assert ('START', 'restored') in world.tgt.actions
    kind, body = sent[0]
    assert kind == 'netcheck' and body['mac'] == '7C:4C:58:00:00:99' and body['probe'] == '10.0.0.250/24'
    assert body['dhcp'] == [{'mac': '7C:4C:58:AA:BB:CC', 'ip': '10.0.0.5'}], 'the test VM gets its source address by DHCP'
    assert body['targets'] == [{'ip': '10.0.0.5', 'ports': [3389]}] and body['wait_s'] == 900
    assert world.db.runs[1]['checks'] == [{'ip': '10.0.0.5', 'ping': False, 'ports': {3389: True}}]


def test_infrastructure_addresses_are_never_probed(world):
    proxy = {'url': 'https://10.0.0.60:8083'}
    assert tr._infrastructure(proxy, POINT) == {'10.0.0.60', '10.0.0.20'}


# ------------------------------------------------------------------ clean-up
def test_cleanup_only_ever_deletes_a_tagged_test_vm(world):
    world.tgt.vms = {'x': {'uuid': 'x', 'name': 'erp', 'tags': 'hcm-testrestore', 'state': 'RUNNING'},
                     'y': {'uuid': 'y', 'name': 'erp-hcmtest-5', 'tags': '', 'state': 'RUNNING'}}
    assert tr.cleanup('dr', 'x') is False and tr.cleanup('dr', 'y') is False
    assert world.tgt.actions == [], 'tag and name mark both required'


def test_the_sweep_spares_running_and_kept_tests(world, monkeypatch):
    world.tgt.vms = {'a': {'uuid': 'a', 'name': 'dc01-hcmtest-7', 'tags': 'hcm-testrestore', 'state': 'SHUTOFF'},
                     'b': {'uuid': 'b', 'name': 'sql-hcmtest-8', 'tags': 'hcm-testrestore', 'state': 'RUNNING'},
                     'c': {'uuid': 'c', 'name': 'web-hcmtest-9', 'tags': 'hcm-testrestore', 'state': 'RUNNING'}}
    db = world.db
    db.test_vms_to_clean = lambda now: []
    db.test_vms_in_use = lambda now: {'c'}                 # kept for inspection
    db.running_test_ids = lambda: [8]                      # still restoring: its uuid is not recorded yet
    monkeypatch.setitem(sys.modules, 'cluster_registry', types.SimpleNamespace(list_enabled=lambda: [types.SimpleNamespace(cluster_id='dr')]))
    assert tr.sweep(NOW) == 1
    assert [a for a in world.tgt.actions if a[0] == 'DELETE'] == [('DELETE', 'a')]


# ------------------------------------------------------------------ the tick
def test_the_tick_runs_one_test_per_target_and_respects_the_byte_budget(world, monkeypatch):
    db = world.db
    db.clusters.append({'cluster_id': 'dr', 'cluster_name': 'DR', 'role': 'dr', 'timezone': 'UTC'})
    db.proxies['dr'] = [{'id': 2, 'name': 'px-dr', 'vm_uuid': 'p2', 'cluster_id': 'dr'}]
    plan = dict(PLAN, enabled=True, interval_days=7, last_run_at=NOW, byte_budget_gb=3, hours_from='01:00', hours_until='06:00')
    db.get_test_plans = lambda folder_id=None, all_folders=False: [plan]
    db.queued_test_runs = lambda: [dict(RUN, id=1, plan_id=3, round_at=NOW), dict(RUN, id=2, plan_id=3, round_at=NOW, vm_uuid='vm2')]
    db.newest_point_for_vm = lambda cid, vm: POINT
    db.round_bytes = lambda pid, r: 2 << 30              # 2 GB used + 2 GB > 3 GB budget
    monkeypatch.setattr(tr.verifier, 'backup_busy', lambda *a, **k: None)
    started = []
    assert tr.tick(NOW, start=lambda run, plan, target, point: started.append((run['id'], target))) == 0
    assert db.runs[1]['status'] == 'skipped' and 'byte budget' in db.runs[1]['detail']
    db.round_bytes = lambda pid, r: 0
    db.runs.clear()
    assert tr.tick(NOW, start=lambda run, plan, target, point: started.append((run['id'], target))) == 1
    assert started == [(1, 'dr')], 'the second test waits: one test per target cluster at a time'
