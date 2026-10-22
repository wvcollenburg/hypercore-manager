"""backup/incremental.py, proxy_client.py and the HyperCore client additions — pure logic with fakes."""
import importlib
import os
import re
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

_BACKUP = os.path.join(os.path.dirname(__file__), '..', 'backup')
_CLASH = ('config', 'db', 'log', 'api', 'hypercore', 'jobs', 'scheduler', 'incremental', 'proxy_client', 'manifest_names')


def _import():
    saved = {n: sys.modules.pop(n) for n in _CLASH if n in sys.modules}
    sys.path.insert(0, _BACKUP)
    try:
        inc = importlib.import_module('incremental')
        pc = importlib.import_module('proxy_client')
        hc = importlib.import_module('hypercore')
        mn = importlib.import_module('manifest_names')
    finally:
        sys.path.remove(_BACKUP)
        for n in _CLASH:
            sys.modules.pop(n, None)
        sys.modules.update(saved)
    return inc, pc, hc, mn


inc, pcm, hcm, mn = _import()
NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)


def _pt(seq, days_ago, chain_id=1):
    return {'id': 100 + seq, 'chain_id': chain_id, 'seq': seq, 'created_at': NOW - timedelta(days=days_ago)}


def test_plan_point_decisions():
    plan = {'full_every_n': 3}
    assert inc.plan_point(None, plan, None)[:3] == ('full', 0, None)
    chain = {'anchor_snapshot_uuid': 'a', 'runs_since_full': 1, 'next_seq': 2}
    assert inc.plan_point(chain, plan, {'seq': 1})[:3] == ('delta', 2, 1)
    kind, _, _, note = inc.plan_point(dict(chain, anchor_snapshot_uuid=None), plan, {'seq': 1})
    assert kind == 'full' and 'reference snapshot missing' in note
    kind, _, _, note = inc.plan_point(dict(chain, runs_since_full=3), plan, {'seq': 3})
    assert kind == 'full' and 'scheduled full' in note


def test_retention_by_count_and_age_never_drops_the_newest():
    pts = [_pt(0, 10), _pt(1, 9), _pt(2, 8), _pt(3, 1), _pt(4, 0)]
    assert [p['seq'] for p in inc.select_points_to_drop(pts, 3, None, NOW)] == [0, 1]
    assert [p['seq'] for p in inc.select_points_to_drop(pts, None, 5, NOW)] == [0, 1, 2]
    assert [p['seq'] for p in inc.select_points_to_drop(pts, 2, 5, NOW)] == [0, 1, 2]
    assert inc.select_points_to_drop([_pt(0, 40)], None, 30, NOW) == [], 'the only point is always kept'
    assert inc.select_points_to_drop(pts, 14, None, NOW) == []


def test_device_capacity_and_definition_subset():
    assert inc.device_capacity({'type': 'VIRTIO_DISK', 'capacity': 15000000000}) == 15000000000
    assert inc.device_capacity({'type': 'NVRAM', 'capacity': 540672}) == 1 << 20
    assert inc.device_capacity({'type': 'VTPM', 'capacity': 0}) == 1 << 20
    client = hcm.HyperCoreClient.__new__(hcm.HyperCoreClient)
    vm = {'uuid': 'u', 'name': 'exptest', 'mem': 4, 'numVCPU': 2, 'machineType': 'scale-uefi-tpm-9.5', 'operatingSystem': 'os_other',
          'cpuType': 'x', 'tags': 'a,b', 'bootDevices': ['d'], 'stats': [1, 2, 3], 'snapshotList': ['x'],
          'blockDevs': [{'uuid': 'c', 'type': 'IDE_CDROM', 'capacity': 0}, {'uuid': 'd', 'type': 'VIRTIO_DISK', 'capacity': 10, 'slot': 0},
                        {'uuid': 'n', 'type': 'NVRAM', 'capacity': 540672}, {'uuid': 't', 'type': 'VTPM', 'capacity': 0}],
          'netDevs': [{'uuid': 'nic', 'type': 'VIRTIO', 'macAddress': '7C:4C:58:4B:5C:5E', 'vlan': 0, 'connected': True, 'ipv4Addresses': []}]}
    d = client.definition_of(vm)
    assert 'stats' not in d and d['netDevs'][0]['macAddress'] == '7C:4C:58:4B:5C:5E' and 'ipv4Addresses' not in d['netDevs'][0]
    assert [b['uuid'] for b in client.backup_devices(vm)] == ['d', 'n', 't'], 'CD-ROM excluded, utility devices included'
    assert client.snapshot_serials({'deviceSnapshots': [{'uuid': 'd', 'serialNumber': 7}, {'uuid': 'n', 'serialNumber': 7}]}) == {'d': 7, 'n': 7}


def test_proxy_client_fingerprint_and_mac_match():
    assert pcm.normalise_fingerprint('AB:CD ef') == 'abcdef' and pcm.normalise_fingerprint(None) is None
    vms = [{'uuid': '1', 'name': 'a', 'netDevs': [{'macAddress': '7c:4c:58:4b:5c:5e'}]}, {'uuid': '2', 'name': 'b', 'netDevs': []}]
    assert pcm.match_vm_by_macs(['7C:4C:58:4B:5C:5E'], vms)['uuid'] == '1'
    assert pcm.match_vm_by_macs(['00:11:22:33:44:55'], vms) is None
    with pytest.raises(pcm.ProxyError):
        pcm.certificate_fingerprint('http://proxy:8083')


def test_chain_naming_matches_the_proxy():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'proxy'))
    try:
        saved = sys.modules.pop('manifest', None)
        pm = importlib.import_module('manifest')
    finally:
        sys.path.pop(0)
        sys.modules.pop('manifest', None)
        if saved:
            sys.modules['manifest'] = saved
    for name, uuid in (('exptest', '3cf115a9-8208-41b2-a67d-0ed5421441c2'), ('My VM (prod)', 'abcdef12-0000-0000-0000-000000000000')):
        assert mn.chain_dirname(name, uuid) == pm.chain_dirname(name, uuid)
        assert mn.chain_dirname(name, uuid, '20261021-190000') == pm.chain_dirname(name, uuid, '20261021-190000')
    assert mn.chain_dirname('exptest', '3cf115a9-x', '20261021-190000') == 'exptest_3cf115a9_20261021-190000.chain'
    for kind, seq in (('full', 0), ('delta', 3), ('full', 14)):
        assert mn.point_dirname(kind, seq) == pm.point_dirname(kind, seq)


class _FakeHC:
    """Enough of the HyperCore client to drive one incremental run."""
    DISK_TYPES = hcm.HyperCoreClient.DISK_TYPES
    UTILITY_TYPES = hcm.HyperCoreClient.UTILITY_TYPES

    def __init__(self):
        self.calls = []; self.snaps = {}; self.devices = {}; self.n = 0
        self.vm = {'uuid': 'vm-1', 'name': 'exptest', 'mem': 1, 'numVCPU': 1, 'machineType': 'scale-uefi-tpm-9.5', 'operatingSystem': 'os_other',
                   'cpuType': 'c', 'tags': 't', 'bootDevices': ['disk-1'], 'netDevs': [{'macAddress': 'AA'}],
                   'blockDevs': [{'uuid': 'disk-1', 'type': 'VIRTIO_DISK', 'capacity': 4 << 20}, {'uuid': 'nv-1', 'type': 'NVRAM', 'capacity': 540672}]}

    def get_vm(self, u): return self.vm
    def backup_devices(self, vm): return hcm.HyperCoreClient.backup_devices(self, vm)
    def definition_of(self, vm): return hcm.HyperCoreClient.definition_of(self, vm)
    def snapshot_serials(self, s): return hcm.HyperCoreClient.snapshot_serials(s)
    def task_status(self, tag): return {'state': 'COMPLETE'}
    def update_status(self): return {'updateStatus': {'masterState': 'COMPLETE'}}
    def list_nodes(self): return [{'uuid': 'n1', 'lanIP': '172.16.0.241', 'networkStatus': 'ONLINE'}]
    quiesced = True
    def create_snapshot(self, vm, label, quiesce=True, retain_days=None):
        self.n += 1; u = f'snap-{self.n}'; self.snaps[u] = {'uuid': u, 'state': 'CREATED', 'label': label, 'quiesced': self.quiesced, 'retain_days': retain_days,
                                                            'deviceSnapshots': [{'uuid': 'disk-1', 'serialNumber': 10 + self.n}, {'uuid': 'nv-1', 'serialNumber': 10 + self.n}]}
        self.calls.append(('snapshot', label)); return f'task-{u}', u
    def get_snapshot(self, u): return self.snaps.get(u)
    def delete_snapshot(self, u): self.snaps.pop(u, None); self.calls.append(('delete_snapshot', u)); return 'task'
    def clone_device(self, dev, snap, target, cap, read_only=True, dev_type='VIRTIO_DISK', tiering=None):
        self.n += 1; u = f'clone-{self.n}'; self.devices[u] = (dev, target, cap, read_only); self.calls.append(('clone', dev, target, dev_type)); return 'task', u
    def delete_block_device(self, u): self.devices.pop(u, None); self.calls.append(('delete_clone', u)); return 'task'
    def snap_diff(self, dev, a, b):
        self.calls.append(('snapdiff', dev, a, b)); return [{'lower': 0, 'upper': 3}] if a == 0 else [{'lower': 1, 'upper': 1}]


class _FakePC:
    def __init__(self): self.jobs = []
    def submit(self, kind, body): self.jobs.append((kind, body)); return f'job-{len(self.jobs)}'
    def wait(self, job_id, on_progress=None, timeout=0, poll=0):
        kind, body = self.jobs[int(job_id.split('-')[1]) - 1]
        if on_progress: on_progress({'blocks_total': 4, 'blocks_done': 4, 'devices_done': 1, 'devices_total': 2})
        if kind == 'backup':
            return {'state': 'done', 'result': {'point_dir': 'base' if body['point']['kind'] == 'full' else f"delta-{body['point']['seq']:04d}",
                                                'bytes_written': 4 << 20, 'duration_s': 1.5, 'devices': [], 'chain_dir': 'x'}}
        if kind == 'verify':
            return {'state': 'done', 'result': dict(self.verify_result)}
        return {'state': 'done', 'result': {'dirs': {}}}
    verify_result = {'ok': True, 'points': {'base': {'ok': True, 'mismatches': []}}, 'bytes': 4 << 20, 'duration_s': 0.1}


class _FakeDB:
    def __init__(self):
        self.runs = {}; self.chains = {}; self.points = []; self.next = 1
        self.plan = {'id': 4, 'cluster_id': 'c1', 'vm_uuid': 'vm-1', 'vm_name': 'exptest', 'enabled': True, 'cluster_enabled': True,
                     'mode': 'incremental', 'retention': 14, 'retention_days': None, 'full_every_n': 14}
    def get_schedule(self, pid): return self.plan
    def is_run_active(self, pid): return False
    def start_run(self, pid): self.runs[1] = {'id': 1}; return 1
    def update_run(self, rid, **f): self.runs[rid].update(f)
    def share_for_cluster(self, cid): return {'id': 9, 'uri_base': 'smb://nas/share', 'username': 'u', 'password': 'p'}
    def get_active_chain(self, pid):
        act = [c for c in self.chains.values() if c['status'] == 'active' and c['plan_id'] == pid]; return act[-1] if act else None
    def last_point(self, cid):
        pts = [p for p in self.points if p['chain_id'] == cid and p['status'] == 'ok']; return pts[-1] if pts else None
    def update_chain(self, cid, **f): self.chains[cid].update(f)
    def add_chain(self, plan, target_id, slug, chain_dir):
        cid = self.next; self.next += 1
        self.chains[cid] = {'id': cid, 'plan_id': plan['id'], 'cluster_id': plan['cluster_id'], 'status': 'active', 'chain_dir': chain_dir, 'next_seq': 0, 'runs_since_full': 0,
                            'anchor_snapshot_uuid': None, 'last_serials': None, 'target_id': target_id}; return cid
    def get_chain(self, cid): return self.chains.get(cid)
    def get_chains(self, cid, include_deleted=False): return [c for c in self.chains.values() if c['cluster_id'] == cid]
    def get_share(self, sid, with_secret=False): return {'id': 9, 'uri_base': 'smb://nas/share', 'username': 'u', 'password': 'p'}
    def add_point(self, chain_id, seq, kind, dir_, serials, blocks, bw, dur, files, definition, run_id, quiesced=None):
        pid = 100 + len(self.points); self.points.append({'id': pid, 'chain_id': chain_id, 'seq': seq, 'kind': kind, 'dir': dir_, 'status': 'ok',
                                                          'created_at': NOW, 'serials': serials, 'definition': definition, 'quiesced': quiesced}); return pid
    def points_for_plan_retention(self, pid):
        return [dict(p, chain_dir=self.chains[p['chain_id']]['chain_dir'], target_id=9) for p in self.points if p['status'] == 'ok']
    def get_points(self, cid, include_pruned=False): return [p for p in self.points if p['chain_id'] == cid and (include_pruned or p['status'] == 'ok')]
    def update_point(self, pid, **f): [p.update(f) for p in self.points if p['id'] == pid]
    def update_proxy(self, pid, **f): pass
    def get_proxies(self, cid, enabled_only=False): return [{'id': 7, 'vm_uuid': 'proxy-vm', 'last_seen_at': None}]
    def get_proxy(self, pid, with_secret=False): return {'id': 7, 'name': 'px', 'url': 'http://px:8083', 'token': 't', 'vm_uuid': 'proxy-vm', 'tls_fingerprint': None, 'tls_verify': False}


@pytest.fixture
def world(monkeypatch):
    fdb, fhc, fpc = _FakeDB(), _FakeHC(), _FakePC()
    monkeypatch.setattr(inc, 'db', fdb)
    monkeypatch.setattr(inc.jobs, 'client_for', lambda cid: fhc)
    monkeypatch.setattr(inc.jobs, 'location_for_cluster', lambda cid: {'tenant': 'Amsterdam', 'base': 'smb://nas/share/Amsterdam'})
    monkeypatch.setattr(inc, 'proxy_client_for', lambda p: fpc)
    monkeypatch.setattr(inc.time, 'sleep', lambda s: None)
    monkeypatch.setattr(inc.jobs.time, 'sleep', lambda s: None)
    monkeypatch.setattr(inc.jobs, 'CFG', types.SimpleNamespace(preflight_wait_hours=0, preflight_poll_seconds=0, task_timeout_hours=8, poll_interval_seconds=0))
    inc.jobs._PREFLIGHT_CACHE.clear()
    return types.SimpleNamespace(db=fdb, hc=fhc, pc=fpc)


def test_first_run_is_a_full_and_second_a_delta(world):
    inc.run_incremental(4)
    run = world.db.runs[1]
    assert run['status'] == 'COMPLETE', run
    assert run['point_kind'] == 'full' and run['blocks_changed'] == 5, run      # 4 disk blocks + 1 NVRAM block
    kind, body = world.pc.jobs[0]
    assert kind == 'backup' and body['point'] == {'kind': 'full', 'seq': 0, 'previous_seq': None}
    assert body['folder'] == 'Amsterdam' and body['target']['password'] == 'p' and body['definition']['netDevs'][0]['macAddress'] == 'AA'
    assert [d['clone_uuid'] for d in body['devices']] == ['clone-2', 'clone-3'] and body['devices'][1]['size'] == 1 << 20
    chain = world.db.chains[1]
    assert chain['anchor_snapshot_uuid'] == 'snap-1' and chain['next_seq'] == 1 and chain['last_serials'] == {'disk-1': 11, 'nv-1': 11}
    # the chain directory is unique per chain (a later full starts a new chain of the same VM) and the proxy is told its name
    assert re.fullmatch(r'Amsterdam/exptest_vm-1_\d{8}-\d{6}\.chain', chain['chain_dir']), chain['chain_dir']
    assert body['chain_dir'] == chain['chain_dir']
    assert not world.hc.devices, 'clones detached after the run'
    assert ('snapdiff', 'disk-1', 0, 11) in world.hc.calls
    # second run: delta against serial 11, old anchor removed, new anchor kept
    world.db.runs.clear(); world.db.start_run(4)
    inc.run_incremental(4)
    run = world.db.runs[1]
    assert run['status'] == 'COMPLETE' and run['point_kind'] == 'delta', run
    kind, body = world.pc.jobs[1]
    assert body['point'] == {'kind': 'delta', 'seq': 1, 'previous_seq': 0}
    assert ('snapdiff', 'disk-1', 11, 14) in world.hc.calls and ('delete_snapshot', 'snap-1') in world.hc.calls
    assert list(world.hc.snaps) == ['snap-4'] and world.db.chains[1]['runs_since_full'] == 1
    assert world.db.chains[1]['active_clones'] == []


def test_anchor_carries_a_retention_deadline_and_the_point_records_quiesce(world):
    """The SDK rules: every snapshot we leave behind sets localRetainUntilTimestamp (it cannot be
    extended later), and whether the point was quiesced is catalogued because the restore UI is asked."""
    inc.run_incremental(4)
    snap = world.hc.snaps['snap-1']
    assert snap['retain_days'] == inc.CFG.anchor_retain_days == 60
    assert world.db.points[0]['quiesced'] is True
    world.hc.quiesced = False
    world.db.runs.clear(); world.db.start_run(4); inc.run_incremental(4)
    assert world.db.points[1]['quiesced'] is False and 'crash-consistent' in world.db.runs[1]['message']


def test_resized_disk_is_copied_in_full_inside_the_delta(world):
    """snapDiff reports nothing for a grown region, so a disk whose capacity changed since the
    previous point gets the allocation map (from serial 0) and the `full` flag for the proxy."""
    inc.run_incremental(4)
    world.hc.vm['blockDevs'][0]['capacity'] = 8 << 20                       # grown from 4 to 8 MiB
    world.db.runs.clear(); world.db.start_run(4); inc.run_incremental(4)
    run = world.db.runs[1]
    assert run['status'] == 'COMPLETE' and run['point_kind'] == 'delta', run
    assert 'changed size (4 -> 8 MiB)' in run['message'] and 'copied in full' in run['message']
    _, body = world.pc.jobs[1]
    disk = body['devices'][0]
    assert disk['full'] is True and disk['full_map'] == disk['map'] and disk['size'] == 8 << 20
    assert ('snapdiff', 'disk-1', 0, 14) in world.hc.calls and ('snapdiff', 'disk-1', 11, 14) not in world.hc.calls
    assert inc.resized_devices([{'uuid': 'a', 'type': 'VIRTIO_DISK', 'capacity': 5}], {'blockDevs': [{'uuid': 'a', 'capacity': 5}]}) == {}
    assert inc.resized_devices([{'uuid': 'a', 'type': 'VIRTIO_DISK', 'capacity': 5}], None) == {}, 'no previous point: nothing to compare'


def test_transient_500_is_retried_and_other_errors_are_not(world):
    attempts = []

    def flaky(*a, **kw):
        attempts.append(1)
        if len(attempts) < 3:
            raise hcm.HyperCoreError('HyperCore API 500 on /VirDomainSnapshot: An internal error occurred', 500)
        return world.hc.__class__.create_snapshot(world.hc, *a, **kw)
    world.hc.create_snapshot = flaky
    inc.run_incremental(4)
    assert world.db.runs[1]['status'] == 'COMPLETE' and len(attempts) == 3
    with pytest.raises(hcm.HyperCoreError, match='403'):
        inc.jobs.retry_transient(lambda: (_ for _ in ()).throw(hcm.HyperCoreError('Permission denied (403)', 403)), 'x')
    assert hcm.HyperCoreError('x', 500).transient and not hcm.HyperCoreError('x', 400).transient
    assert hcm.HyperCoreError('timed out waiting for CV refresh').transient


def test_preflight_rules_and_a_deferred_run():
    pf = inc.jobs.preflight_of
    assert pf({'updateStatus': {'masterState': 'COMPLETE'}}, [{'networkStatus': 'ONLINE'}])['ok']
    assert pf({}, [])['ok'], 'a cluster that never updated has no status'
    up = pf({'updateStatus': {'masterState': 'RUNNING', 'toVersion': '9.9.0', 'percent': '40'}}, [{'networkStatus': 'ONLINE'}])
    assert not up['ok'] and up['kind'] == 'update' and '9.9.0' in up['detail'] and '40%' in up['detail']
    node = pf({}, [{'lanIP': '172.16.0.242', 'networkStatus': 'OFFLINE', 'uuid': 'n2'}, {'lanIP': '172.16.0.241', 'networkStatus': 'ONLINE'}])
    assert not node['ok'] and node['kind'] == 'node' and node['nodes'] == [{'lan_ip': '172.16.0.242', 'status': 'OFFLINE', 'uuid': 'n2'}]
    assert 'Scale Computing support' in inc.jobs.DEFERRED_HINT['node']


def test_run_is_deferred_while_a_node_is_missing(world):
    world.hc.list_nodes = lambda: [{'uuid': 'n2', 'lanIP': '172.16.0.242', 'networkStatus': 'OFFLINE'}]
    inc.run_incremental(4)
    run = world.db.runs[1]
    assert run['status'] == 'SKIPPED' and run['message'].startswith('Deferred: node not online: 172.16.0.242 (OFFLINE)')
    assert 'evicted' in run['message'] and not world.hc.snaps and not world.pc.jobs, 'nothing touched the cluster'
    # the wait loop reports the reason once and gives up at the deadline; a healthy cluster passes at once
    seen = []
    assert inc.jobs.wait_until_clear(world.hc, seen.append, max_hours=0, poll_seconds=0)['ok'] is False and len(seen) == 1
    world.hc.list_nodes = lambda: [{'networkStatus': 'ONLINE'}]
    assert inc.jobs.wait_until_clear(world.hc, seen.append, max_hours=0, poll_seconds=0)['ok'] and len(seen) == 1


def test_vm_lock_is_one_object_per_vm():
    a, b = inc.vm_lock('c1', 'vm-1'), inc.vm_lock('c1', 'vm-1')
    assert a is b and inc.vm_lock('c1', 'vm-2') is not a and inc.vm_lock('c2', 'vm-1') is not a


def test_missing_anchor_falls_back_to_full_with_a_note(world):
    inc.run_incremental(4)
    world.hc.snaps.clear()                       # someone deleted the reference snapshot
    world.db.runs.clear(); world.db.start_run(4)
    inc.run_incremental(4)
    run = world.db.runs[1]
    assert run['point_kind'] == 'full' and 'reference snapshot missing' in run['message']
    assert world.db.chains[1]['status'] == 'retired' and world.db.chains[2]['status'] == 'active'


def test_proxy_failure_cleans_up_clones_and_new_anchor(world, monkeypatch):
    def boom(job_id, on_progress=None, timeout=0, poll=0):
        raise inc.ProxyError('disk full on target')
    monkeypatch.setattr(world.pc, 'wait', boom)
    inc.run_incremental(4)
    run = world.db.runs[1]
    assert run['status'] == 'ERROR' and 'disk full' in run['message']
    assert not world.hc.devices and not world.hc.snaps, 'clones and the new anchor are gone'
    assert world.db.chains[1]['active_clones'] == [] and world.db.chains[1]['anchor_snapshot_uuid'] is None


def test_scheduled_full_starts_a_new_chain(world):
    world.db.plan['full_every_n'] = 2
    for _ in range(4):
        world.db.runs.clear(); world.db.start_run(4); inc.run_incremental(4)
    kinds = [body['point']['kind'] for _, body in world.pc.jobs if _ == 'backup']
    assert kinds == ['full', 'delta', 'delta', 'full']
    assert [c['status'] for c in world.db.chains.values()] == ['retired', 'active']


def test_delta_with_no_disk_changes_is_skipped(world, monkeypatch):
    inc.run_incremental(4)
    monkeypatch.setattr(world.hc, 'snap_diff', lambda dev, a, b: [{'lower': 0, 'upper': 3}] if a == 0 else [])
    world.db.runs.clear(); world.db.start_run(4)
    inc.run_incremental(4)
    run = world.db.runs[1]
    assert run['status'] == 'COMPLETE' and run['blocks_changed'] == 0 and 'nothing written' in run['message']
    assert len([k for k, _ in world.pc.jobs if k == 'backup']) == 1, 'no proxy job for an unchanged VM'
    assert list(world.hc.snaps) == ['snap-1'], 'the previous anchor is kept, the unneeded one removed'
    assert not world.hc.devices and world.db.chains[1]['next_seq'] == 1


def test_https_proxy_without_pin_or_ca_is_refused():
    pc = pcm.ProxyClient('https://10.0.0.5:8083', 't')
    with pytest.raises(pcm.ProxyError, match='pinned'):
        pc.health()
    assert pcm.ProxyClient('https://10.0.0.5:8083', 't', tls_verify=True).fingerprint is None
    assert pcm.ProxyClient('https://10.0.0.5:8083', 't', tls_fingerprint='AB:CD').fingerprint == 'abcd'


def test_deleting_a_chain_releases_its_anchor_snapshot(world):
    """A deleted chain's reference snapshot has no use; leaving it would pile up snapshots on the VM."""
    inc.run_incremental(4)
    chain = world.db.chains[1]
    assert chain['anchor_snapshot_uuid'] == 'snap-1' and 'snap-1' in world.hc.snaps
    inc.release_anchor(chain)
    assert ('delete_snapshot', 'snap-1') in world.hc.calls and 'snap-1' not in world.hc.snaps
    assert world.db.chains[1]['anchor_snapshot_uuid'] is None
    inc.release_anchor(world.db.chains[1])                        # idempotent
    assert world.hc.calls.count(('delete_snapshot', 'snap-1')) == 1


def test_verify_results_stamp_passing_points_and_flag_corrupt_ones(world):
    db = world.db
    cid = db.add_chain(db.plan, 9, 'Amsterdam', 'Amsterdam/exptest_vm-1.chain')
    db.add_point(cid, 0, 'full', 'base', {}, 4, 4 << 20, 1.0, [], {}, 1)
    db.add_point(cid, 1, 'delta', 'delta-0001', {}, 1, 1 << 20, 0.5, [], {}, 2)
    db.add_point(cid, 2, 'delta', 'delta-0002', {}, 1, 1 << 20, 0.5, [], {}, 3)
    base, d1, d2 = db.points
    # a restore of seq 1 verifies base + delta-0001 only; delta-0002 is not touched
    world.pc.verify_result = {'ok': False, 'points': {'base': {'ok': True, 'mismatches': []},
                                                     'delta-0001': {'ok': False, 'mismatches': ['disk.qcow2: checksum differs', 'nvram-x.qcow2: missing']}}}
    res = inc.verify_chain(db.get_chain(cid), db.get_share(9), world.pc, upto_seq=1)
    assert world.pc.jobs[-1] == ('verify', {'target': {'uri': 'smb://nas/share', 'username': 'u', 'password': 'p'}, 'chain_dir': 'Amsterdam/exptest_vm-1.chain', 'upto_seq': 1})
    assert res['ok'] is False and res['failed'] == {'delta-0001': 'disk.qcow2: checksum differs; nvram-x.qcow2: missing'}
    assert base['verified_at'] and base.get('verify_error') is None
    assert d1['verify_error'] == 'disk.qcow2: checksum differs; nvram-x.qcow2: missing' and 'verified_at' not in d1
    assert 'verified_at' not in d2 and 'verify_error' not in d2
    # a later clean verify of the whole chain clears the error
    world.pc.verify_result = {'ok': True, 'points': {d: {'ok': True, 'mismatches': []} for d in ('base', 'delta-0001', 'delta-0002')}}
    res = inc.verify_chain(db.get_chain(cid), db.get_share(9), world.pc)
    assert res['ok'] and res['failed'] == {} and 'upto_seq' not in world.pc.jobs[-1][1]
    assert all(p['verified_at'] and p['verify_error'] is None for p in db.points)
    assert inc.verify_progress_text({'points_total': 3, 'points_done': 1, 'bytes_total': 6 << 20, 'bytes_done': 3 << 20}) == 'verifying chain: point 2/3, 3/6 MiB'
    assert inc.verify_progress_text({}) == 'verifying chain'


def test_manual_verify_runs_in_the_background_and_the_weekly_counts(world):
    import time as _t
    db = world.db
    cid = db.add_chain(db.plan, 9, 'Amsterdam', 'Amsterdam/exptest_vm-1.chain')
    db.add_point(cid, 0, 'full', 'base', {}, 4, 4 << 20, 1.0, [], {}, 1)
    entry = inc.start_verify(cid)
    for _ in range(200):
        if entry['state'] != 'running':
            break
        _t.sleep(0.01)
    assert entry['state'] == 'done' and entry['result']['ok'] and inc.verify_status(cid) is entry
    assert db.points[0]['verified_at']
    with pytest.raises(inc.IncrementalError):
        inc.start_verify(999)
    world.pc.verify_result = {'ok': False, 'points': {'base': {'ok': False, 'mismatches': ['disk.qcow2: checksum differs']}}}
    assert inc.verify_active_chains('c1') == {'verified': 0, 'failed': 1}
    assert db.points[0]['verify_error'] == 'disk.qcow2: checksum differs'


class _CdHC:
    """A proxy VM with its cloud-init ISO still attached; delete is refused while it runs."""
    def __init__(self, running=True):
        self.running = running; self.calls = []
        self.vm = {'uuid': 'proxy-vm', 'state': 'RUNNING' if running else 'SHUTOFF',
                   'blockDevs': [{'uuid': 'nv', 'type': 'NVRAM', 'path': 'scribe/nv'},
                                 {'uuid': 'cd', 'type': 'IDE_CDROM', 'name': 'cloud-init-30115aab.iso', 'path': 'scribe/iso'},
                                 {'uuid': 'cd2', 'type': 'IDE_CDROM', 'name': '', 'path': ''},
                                 {'uuid': 'disk', 'type': 'VIRTIO_DISK', 'path': 'scribe/disk'}]}
    def get_vm(self, u): return self.vm if u == 'proxy-vm' else None
    def task_status(self, tag): return {'state': 'COMPLETE'}
    def delete_block_device(self, u):
        self.calls.append(('delete', u))
        if self.running:
            raise hcm.HyperCoreError("HyperCore API 500 on /VirDomainBlockDevice/cd: Unable to delete block device from VM '%@': Still in use", 500)
        self.vm['blockDevs'] = [b for b in self.vm['blockDevs'] if b['uuid'] != u]; return 'task'
    def patch_block_device(self, u, **f):
        self.calls.append(('patch', u, f)); [b.update(f, name='') for b in self.vm['blockDevs'] if b['uuid'] == u]; return 'task'
    def list_snapshots(self): return []


def test_cloud_init_iso_is_ejected_while_running_and_deleted_when_off(monkeypatch):
    monkeypatch.setattr(inc.time, 'sleep', lambda s: None)
    hc = _CdHC(running=True)
    assert inc.remove_cloud_init_disk(hc, 'proxy-vm') == 'ejected'
    assert hc.calls == [('delete', 'cd'), ('patch', 'cd', {'path': ''})], 'only the cloud-init CD-ROM is touched, the empty one is left alone'
    assert inc.remove_cloud_init_disk(hc, 'proxy-vm') is None, 'nothing left to do once ejected'
    hc = _CdHC(running=False)
    assert inc.remove_cloud_init_disk(hc, 'proxy-vm') == 'deleted' and [b['uuid'] for b in hc.vm['blockDevs']] == ['nv', 'cd2', 'disk']
    assert inc.remove_cloud_init_disk(hc, 'missing') is None


def test_daily_sweep_removes_cloud_init_isos_of_registered_proxies(world, monkeypatch):
    hc = _CdHC(running=True)
    monkeypatch.setattr(inc.jobs, 'client_for', lambda cid: hc)
    monkeypatch.setattr(world.db, 'chains_with_clones', lambda: [], raising=False)
    out = inc.sweep_cluster('c1')
    assert out == {'snapshots': 0, 'clones': 0, 'cloud_init': 1} and ('patch', 'cd', {'path': ''}) in hc.calls
    assert inc.sweep_cluster('c1')['cloud_init'] == 0, 'idempotent'
