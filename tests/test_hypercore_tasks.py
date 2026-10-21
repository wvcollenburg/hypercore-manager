"""shared/hypercore_tasks.py — parsing of the HyperCore REST v2 task feed."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'shared'))
import hypercore_tasks as ht  # noqa: E402

MOVE = {'taskTag': '1792', 'formattedDescription': 'Move Virtual Machine %@ from %@ to %@', 'descriptionParameters': ['DigitalPlayground', '172.16.0.241', '172.16.0.244'],
        'objectUUID': 'a54f93da', 'nodeUUIDs': [''], 'state': 'COMPLETE', 'progressPercent': 100, 'created': 1767945531, 'completed': 1767945644}
SNAP = {'taskTag': '1796', 'formattedDescription': 'Create Snapshot %@ Virtual Machine %@', 'descriptionParameters': ['', 'MaxDemo'], 'objectUUID': 'dd2628fd',
        'nodeUUIDs': ['f3d2bc6d'], 'state': 'RUNNING', 'progressPercent': 40, 'created': 1767955143, 'completed': 0}
EXPORT = {'taskTag': '2000', 'formattedDescription': "Export Virtual Machine '%@' to '%@'", 'descriptionParameters': ['exptest', 'smb://nas/share'], 'objectUUID': '3cf115a9',
          'state': 'RUNNING', 'progressPercent': 10}
CLONE_DEV = {'taskTag': '1819', 'formattedDescription': 'Create clone of block device %@ from %@ serial %@ for Virtual Machine %@', 'descriptionParameters': ['1040f341', '5e1da7da', '1', 'MaxDemo'],
             'objectUUID': 'b5bcad3c', 'state': 'QUEUED', 'progressPercent': 0}
START = {'taskTag': '1789', 'formattedDescription': 'Start Virtual Machine %@', 'descriptionParameters': ['MineC01'], 'objectUUID': 'd8521a00', 'state': 'RUNNING', 'progressPercent': 50}


def test_kind_description_and_vm_name():
    assert ht.kind_of(MOVE) == 'move' and ht.kind_of(SNAP) == 'snapshot' and ht.kind_of(EXPORT) == 'export' and ht.kind_of(CLONE_DEV) == 'disk'
    assert ht.kind_of({'formattedDescription': 'Force Rollout'}) == 'other'
    assert ht.describe(MOVE) == 'Move Virtual Machine DigitalPlayground from 172.16.0.241 to 172.16.0.244'
    assert ht.describe(SNAP) == 'Create Snapshot  Virtual Machine MaxDemo'
    assert ht.vm_name_of(MOVE) == 'DigitalPlayground' and ht.vm_name_of(SNAP) == 'MaxDemo' and ht.vm_name_of(EXPORT) == 'exptest'
    assert ht.vm_name_of(CLONE_DEV) == 'MaxDemo' and ht.vm_name_of({'formattedDescription': 'Force Rollout'}) is None


def test_normalise_row():
    row = ht.normalise(MOVE)
    assert row['task_tag'] == '1792' and row['kind'] == 'move' and row['vm_name'] == 'DigitalPlayground' and row['progress'] == 100
    assert row['node_uuids'] == [] and row['completed_at'].year == 2026 and row['params'][1] == '172.16.0.241'
    assert ht.normalise(SNAP)['completed_at'] is None


def test_busy_reason_blocks_moves_and_busy_vms_only():
    assert ht.busy_reason([MOVE]) is None, 'a finished move does not block'
    running_move = dict(MOVE, state='RUNNING')
    assert 'migration is in progress' in ht.busy_reason([running_move], vm_uuid='other')
    assert 'VM is busy' in ht.busy_reason([SNAP], vm_uuid='dd2628fd')
    assert 'VM is busy' in ht.busy_reason([EXPORT], vm_name='exptest')
    assert ht.busy_reason([SNAP, EXPORT], vm_uuid='zzz', vm_name='zzz') is None, 'other VMs busy is fine'
    assert ht.busy_reason([START], vm_uuid='d8521a00') is None, 'a start is not a disk operation'


def test_move_event_and_v2_base():
    ev = ht.move_event(MOVE)
    assert ev['vm_name'] == 'DigitalPlayground' and ev['source_ip'] == '172.16.0.241' and ev['target_ip'] == '172.16.0.244' and ev['task_tag'] == '1792'
    assert ev['time'].timestamp() == 1767945644
    assert ht.move_event(SNAP) is None and ht.move_event(dict(MOVE, state='RUNNING')) is None
    assert ht.v2_base('https://172.16.0.241/rest/v1') == 'https://172.16.0.241/rest/v2'
    assert ht.v2_base('https://h:443/rest/v1/') == 'https://h:443/rest/v2'


def test_fetch_and_probe_use_the_session(monkeypatch):
    calls = []

    class Resp:
        def __init__(self, status, data): self.status_code, self._d = status, data
        def raise_for_status(self):
            if self.status_code >= 400: raise RuntimeError(self.status_code)
        def json(self): return self._d

    class S:
        def get(self, url, params=None, timeout=None): calls.append((url, params)); return Resp(200, [MOVE])
    assert ht.fetch_tasks(S(), 'https://c/rest/v1', active_only=True) == [MOVE]
    assert calls[-1][0] == 'https://c/rest/v2/TaskTag' and calls[-1][1]['isActive'] == 'true'
    assert ht.probe_v2(S(), 'https://c/rest/v1') is True

    class S404:
        def get(self, url, params=None, timeout=None): return Resp(404, {'error': 'no'})
    assert ht.probe_v2(S404(), 'https://c/rest/v1') is False


def test_dedupe_external_drops_our_own_moves_only():
    ours = {'time': '2026-10-21T10:00:00+00:00', 'vm_name': 'web01', 'engine': 'reactive'}
    same = {'time': '2026-10-21T10:01:30+00:00', 'vm_name': 'web01', 'engine': 'external'}
    other_vm = {'time': '2026-10-21T10:01:30+00:00', 'vm_name': 'db01', 'engine': 'external'}
    later = {'time': '2026-10-21T10:10:00+00:00', 'vm_name': 'web01', 'engine': 'external'}
    out = ht.dedupe_external([ours, same, other_vm, later])
    assert out == [ours, other_vm, later]
