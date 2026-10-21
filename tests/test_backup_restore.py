"""backup/restore.py — the fast restore path with fakes (diskless import, staging, clone-in, policies)."""
import importlib
import os
import sys
import types
import xml.etree.ElementTree as ET

import pytest

_BACKUP = os.path.join(os.path.dirname(__file__), '..', 'backup')
_CLASH = ('config', 'db', 'log', 'api', 'hypercore', 'jobs', 'scheduler', 'incremental', 'proxy_client', 'manifest_names', 'restore', 'vm_definition')


def _import():
    saved = {n: sys.modules.pop(n) for n in _CLASH if n in sys.modules}
    sys.path.insert(0, _BACKUP)
    try:
        rs = importlib.import_module('restore')
        inc = importlib.import_module('incremental')
        hc = importlib.import_module('hypercore')
    finally:
        sys.path.remove(_BACKUP)
        for n in _CLASH:
            sys.modules.pop(n, None)
        sys.modules.update(saved)
    return rs, inc, hc


rs, inc, hcm = _import()

DEF = {'uuid': 'src-vm', 'name': 'exptest', 'description': 'd', 'mem': 4294967296, 'numVCPU': 2, 'machineType': 'scale-uefi-tpm-9.5',
       'operatingSystem': 'os_other', 'cpuType': 'c', 'tags': 'test,dnd', 'bootDevices': ['disk-1', 'cd-1'],
       'blockDevs': [{'uuid': 'cd-1', 'type': 'IDE_CDROM', 'capacity': 0}, {'uuid': 'disk-1', 'type': 'VIRTIO_DISK', 'capacity': 15000000000},
                     {'uuid': 'nv-1', 'type': 'NVRAM', 'capacity': 540672}, {'uuid': 'tpm-1', 'type': 'VTPM', 'capacity': 0}],
       'netDevs': [{'uuid': 'nic-1', 'type': 'VIRTIO', 'macAddress': '7C:4C:58:4B:5C:5E', 'vlan': 7, 'connected': True}]}


def test_policies_and_boot_order():
    assert rs.mac_decision('auto', source_vm_exists=False) is True
    assert rs.mac_decision('auto', source_vm_exists=True) is False
    assert rs.mac_decision('original', True) is True and rs.mac_decision('new', False) is False
    disks, utils = rs.plan_devices(DEF)
    assert [d['uuid'] for d in disks] == ['disk-1'] and [u['uuid'] for u in utils] == ['nv-1', 'tpm-1']
    new_vm = {'blockDevs': [{'uuid': 'new-cd', 'type': 'IDE_CDROM'}, {'uuid': 'new-disk', 'type': 'VIRTIO_DISK'}]}
    assert rs.boot_order(DEF, {'disk-1': 'new-disk'}, new_vm) == ['new-disk', 'new-cd']


def test_render_definition_is_diskless(monkeypatch):
    monkeypatch.setattr(rs.db, 'get_os_template', lambda mt, prof: None)
    xml = rs.render_definition(DEF, 'new-uuid', {'nv-1': 'NV', 'tpm-1': 'TPM'})
    root = ET.fromstring(xml)
    assert root.findtext('uuid') == 'new-uuid' and root.findtext('os/nvram') == 'scribe:scribe/NV'
    assert not [d for d in root.iter('disk') if d.get('device') == 'disk'], 'no data disks in a diskless definition'
    assert root.find('.//disk[@device="cdrom"]') is not None and root.find('.//interface/mac').get('address') == '7C:4C:58:4B:5C:5E'
    ns = {'s': 'http://www.scalecomputing.com/scaled'}
    assert root.find('.//s:vtpm', ns).get('uuid') == 'TPM'


class _FakeHC:
    DISK_TYPES = hcm.HyperCoreClient.DISK_TYPES
    UTILITY_TYPES = hcm.HyperCoreClient.UTILITY_TYPES

    def __init__(self, source_exists=False):
        self.calls = []; self.n = 0; self.snaps = {}; self.proxy_devs = []; self.new_devs = []
        self.vms = [{'uuid': 'proxy-vm', 'name': 'proxy'}] + ([{'uuid': 'src-vm', 'name': 'exptest'}] if source_exists else [])
        self.new_vm = None
    def list_vms_full(self): return list(self.vms)
    def task_status(self, tag): return {'state': 'COMPLETE'}
    def import_vm(self, uri, name=None, description=None, tags=None, definition_file=None):
        self.calls.append(('import', definition_file, name, tags))
        self.new_vm = {'uuid': 'new-vm', 'name': name, 'blockDevs': [{'uuid': 'new-nv', 'type': 'NVRAM'}, {'uuid': 'new-tpm', 'type': 'VTPM'}, {'uuid': 'new-cd', 'type': 'IDE_CDROM'}],
                       'netDevs': [{'uuid': 'new-nic', 'macAddress': '7C:4C:58:AA:AA:AA', 'connected': True}]}
        self.vms.append(self.new_vm); return 'task', 'new-vm'
    def get_vm(self, u): return self.new_vm
    def create_block_device(self, vm, cap, dev_type='VIRTIO_DISK'):
        self.n += 1; u = f'stage-{self.n}'; self.proxy_devs.append(u); self.calls.append(('create_staging', vm, cap)); return 'task', u
    def create_snapshot(self, vm, label, quiesce=True):
        self.n += 1; u = f'psnap-{self.n}'; self.snaps[u] = {'uuid': u, 'state': 'CREATED', 'deviceSnapshots': []}; self.calls.append(('snapshot', vm, label)); return 'task', u
    def get_snapshot(self, u): return self.snaps.get(u)
    def delete_snapshot(self, u): self.snaps.pop(u, None); return 'task'
    def clone_device(self, dev, snap, target, cap, read_only=True):
        self.n += 1; u = f'restored-disk-{self.n}'; self.new_devs.append(u); self.new_vm['blockDevs'].append({'uuid': u, 'type': 'VIRTIO_DISK'})
        self.calls.append(('clone', dev, snap, target, read_only)); return 'task', u
    def delete_block_device(self, u): self.proxy_devs.remove(u) if u in self.proxy_devs else None; self.calls.append(('delete_dev', u)); return 'task'
    def patch_vm(self, u, **f): self.calls.append(('patch_vm', u, f)); return 'task'
    def patch_netdev(self, u, **f): self.calls.append(('patch_nic', u, f)); return 'task'
    def vm_action(self, u, a): self.calls.append(('action', u, a)); return 'task'


class _FakePC:
    def __init__(self): self.jobs = []
    def submit(self, kind, body): self.jobs.append((kind, body)); return f'j{len(self.jobs)}'
    def wait(self, jid, on_progress=None, timeout=0, poll=0):
        kind, body = self.jobs[int(jid[1:]) - 1]
        if kind == 'stage' and on_progress: on_progress({'blocks_done': 3, 'blocks_total': 3})
        return {'state': 'done', 'result': {'bytes_written': 3 << 20, 'devices': []} if kind == 'stage' else {'files': [], 'removed': []}}


class _FakeDB:
    def __init__(self):
        self.restore = {'id': 9, 'point_id': 100, 'target_name': 'exptest-copy', 'params': {'mac_policy': 'auto', 'start': True, 'nic_connected': False}}
        self.point = {'id': 100, 'chain_id': 1, 'seq': 1, 'status': 'ok', 'definition': DEF}
        self.chain = {'id': 1, 'cluster_id': 'c1', 'vm_name': 'exptest', 'target_id': 9, 'chain_dir': 'Amsterdam/exptest_src-vm.chain', 'folder_slug': 'Amsterdam', 'status': 'active'}
    def get_restore(self, rid): return dict(self.restore)
    def get_point(self, pid): return self.point
    def get_chain(self, cid): return self.chain
    def get_share(self, sid, with_secret=False): return {'id': 9, 'uri_base': 'smb://nas/share', 'username': 'u', 'password': 'p'}
    def get_os_template(self, mt, prof): return None
    def update_restore(self, rid, **f): self.restore.update(f)
    def get_proxies(self, cid, enabled_only=False): return [{'id': 7, 'vm_uuid': 'proxy-vm', 'last_seen_at': None}]
    def get_proxy(self, pid, with_secret=False): return {'id': 7, 'name': 'px', 'url': 'http://px', 'token': 't', 'vm_uuid': 'proxy-vm', 'tls_fingerprint': None, 'tls_verify': False}


@pytest.fixture
def world(monkeypatch):
    fdb, fhc, fpc = _FakeDB(), _FakeHC(), _FakePC()
    monkeypatch.setattr(rs, 'db', fdb); monkeypatch.setattr(inc, 'db', fdb)
    monkeypatch.setattr(rs.jobs, 'client_for', lambda cid: fhc)
    monkeypatch.setattr(rs, 'proxy_client_for', lambda p: fpc)
    monkeypatch.setattr(rs.time, 'sleep', lambda s: None); monkeypatch.setattr(inc.time, 'sleep', lambda s: None)
    return types.SimpleNamespace(db=fdb, hc=fhc, pc=fpc)


def test_fast_restore_end_to_end(world):
    rs.run_restore(9)
    r = world.db.restore
    assert r['status'] == 'COMPLETE', r
    kinds = [k for k, _ in world.pc.jobs]
    assert kinds == ['flatten', 'stage', 'sweep']
    flat = world.pc.jobs[0][1]
    assert set(flat['only_uuids']) == {'nv-1', 'tpm-1'} and flat['definition_xml'].startswith('<domain') and flat['out_dir'].startswith('Amsterdam/_restore/')
    assert ET.fromstring(flat['definition_xml']).find('.//disk[@device="disk"]') is None
    imp = [c for c in world.hc.calls if c[0] == 'import'][0]
    assert imp[1] == flat['definition_name'] and imp[2] == 'exptest-copy' and imp[3] == 'test,dnd'
    stage = world.pc.jobs[1][1]
    assert stage['devices'] == [{'uuid': 'disk-1', 'staging_clone_uuid': 'stage-1'}] and stage['seq'] == 1
    clone = [c for c in world.hc.calls if c[0] == 'clone'][0]
    assert clone[1] == 'stage-1' and clone[3] == 'new-vm' and clone[4] is False, 'staging disk cloned read-write onto the restored VM'
    assert world.hc.proxy_devs == [] and world.hc.snaps == {}, 'staging disk and proxy snapshot removed'
    assert ('patch_vm', 'new-vm', {'bootDevices': [world.hc.new_devs[0], 'new-cd']}) in world.hc.calls
    nic = [c for c in world.hc.calls if c[0] == 'patch_nic'][0]
    assert nic[2] == {'connected': False, 'vlan': 7, 'macAddress': '7C:4C:58:4B:5C:5E'}, 'auto policy: source gone -> original MAC'
    assert ('action', 'new-vm', 'START') in world.hc.calls and r['created_uuid'] == 'new-vm' and 'MAC original' in r['message']


def test_restore_keeps_new_mac_when_source_vm_still_exists(world, monkeypatch):
    world.hc.vms.append({'uuid': 'src-vm', 'name': 'exptest'})
    world.db.restore['params'] = {'mac_policy': 'auto', 'start': False, 'nic_connected': True}
    rs.run_restore(9)
    nic = [c for c in world.hc.calls if c[0] == 'patch_nic'][0]
    assert 'macAddress' not in nic[2] and nic[2]['connected'] is True
    assert not [c for c in world.hc.calls if c[0] == 'action']


def test_restore_refuses_existing_name_and_cleans_up_on_failure(world, monkeypatch):
    world.hc.vms.append({'uuid': 'x', 'name': 'exptest-copy'})
    rs.run_restore(9)
    assert world.db.restore['status'] == 'ERROR' and 'already exists' in world.db.restore['message']
    world.hc.vms.pop()
    def boom(jid, on_progress=None, timeout=0, poll=0):
        kind, _ = world.pc.jobs[int(jid[1:]) - 1]
        if kind == 'stage':
            raise rs.ProxyError('target unreachable')
        return {'state': 'done', 'result': {'files': []}}
    monkeypatch.setattr(world.pc, 'wait', boom)
    world.db.restore.update(status='RUNNING')
    rs.run_restore(9)
    assert world.db.restore['status'] == 'ERROR' and 'target unreachable' in world.db.restore['message']
    assert world.hc.proxy_devs == [], 'staging disks removed after a failed stage'
