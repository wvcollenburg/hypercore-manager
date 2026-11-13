"""backup/vmedit.py — which VM edits need a shutdown cycle, what is refused, and how a change set runs
(shutdown, apply, start; abort or force off when the guest will not stop; restart after a failure)."""
import importlib
import os
import sys

import pytest

_BACKUP = os.path.join(os.path.dirname(__file__), '..', 'backup')
_CLASH = ('config', 'db', 'log', 'api', 'hypercore', 'jobs', 'scheduler', 'incremental', 'proxy_client', 'manifest_names',
          'restore', 'vm_definition', 'vmedit')


def _import():
    saved = {n: sys.modules.pop(n) for n in _CLASH if n in sys.modules}
    sys.path.insert(0, _BACKUP)
    try:
        ve = importlib.import_module('vmedit')
    finally:
        sys.path.remove(_BACKUP)
        for n in _CLASH:
            sys.modules.pop(n, None)
        sys.modules.update(saved)
    return ve


ve = _import()
GIB = 1024 ** 3

VM = {'uuid': 'vm-1', 'name': 'web01', 'state': 'RUNNING', 'mem': 4 * GIB, 'numVCPU': 2,
      'blockDevs': [{'uuid': 'd1', 'type': 'VIRTIO_DISK', 'capacity': 50 * GIB, 'slot': 0, 'cacheMode': 'WRITETHROUGH'},
                    {'uuid': 'cd', 'type': 'IDE_CDROM', 'capacity': 0, 'slot': 0}],
      'netDevs': [{'uuid': 'n1', 'type': 'VIRTIO', 'macAddress': '7C:4C:58:00:00:01', 'vlan': 0, 'connected': True}]}


def _plan(*ops, kind='edit'):
    return ve.plan(kind, list(ops), VM)


@pytest.mark.parametrize('op, cycle', [
    ({'op': 'vm', 'fields': {'name': 'web02', 'description': 'x', 'tags': 'a,b'}}, False),
    ({'op': 'vm', 'fields': {'mem': 8 * GIB}}, True),
    ({'op': 'vm', 'fields': {'numVCPU': 4}}, True),
    ({'op': 'vm', 'fields': {'bootDevices': ['cd', 'd1']}}, True),
    ({'op': 'disk_resize', 'device': 'd1', 'capacity': 60 * GIB}, False),                  # William: live
    ({'op': 'disk_add', 'type': 'VIRTIO_DISK', 'capacity': 10 * GIB}, False),              # William: live
    ({'op': 'iso', 'device': 'cd', 'iso_uuid': 'iso-1'}, False),                            # William: live
    ({'op': 'disk_set', 'device': 'd1', 'fields': {'cacheMode': 'WRITEBACK'}}, True),
    ({'op': 'disk_set', 'device': 'd1', 'fields': {'tieringPriorityFactor': 4}}, False),
    ({'op': 'nic_set', 'device': 'n1', 'fields': {'vlan': 102}}, True),                     # William: VLAN needs the cycle
    ({'op': 'nic_set', 'device': 'n1', 'fields': {'connected': False}}, False),
    ({'op': 'nic_add', 'vlan': 5}, True),
])
def test_which_changes_need_the_shutdown_cycle(op, cycle):
    assert _plan(op)['cycle'] is cycle


CONNS = [{'uuid': 'c-dr', 'remoteClusterInfo': {'clusterName': 'WDvCN11'}}, {'uuid': 'c-2', 'remoteClusterInfo': {'clusterName': 'Other'}}]


REP = [{'uuid': 'r1', 'connectionUUID': 'c-dr', 'enable': True}]


@pytest.mark.parametrize('reps, o, action', [
    ([], {'target': 'c-dr'}, 'create'),                                            # first replication
    ([], {'target': ''}, None),
    (REP, {'pause': True}, 'disable'),                                             # pause transmissions
    ([dict(REP[0], enable=False)], {'pause': False}, 'enable'),                    # resume
    (REP, {'pause': False}, None),                                                 # no change
])
def test_replication_create_pause_resume(reps, o, action):
    r = ve.replication_op(o, reps, CONNS)
    assert (r or {}).get('action') == action
    if action:
        p = ve.plan('edit', [dict(o, op='replication')], VM, {'replications': reps, 'connections': CONNS})
        assert p['cycle'] is False and p['ops'][0]['action'] == action


def test_ending_or_repointing_a_replication_is_not_offered():
    """Not in the REST API (DELETE answers 405); HyperCore's own UI does it."""
    with pytest.raises(ve.EditError, match='not available in the HyperCore REST API'):
        ve.replication_op({'target': 'c-2'}, REP, CONNS)


def test_unknown_replication_target_refused():
    with pytest.raises(ve.EditError, match='connection'):
        ve.replication_op({'target': 'nope'}, [], CONNS)


def test_removals_are_destructive_and_run_first():
    p = _plan({'op': 'vm', 'fields': {'mem': 8 * GIB}}, {'op': 'disk_remove', 'device': 'd1'}, {'op': 'nic_remove', 'device': 'n1'})
    assert p['destructive'] and p['cycle']
    assert [o['op'] for o in p['ops']] == ['disk_remove', 'nic_remove', 'vm']


def test_confirmation_is_the_vm_name():
    assert ve.confirm_ok(False, None, VM)
    assert not ve.confirm_ok(True, 'web0', VM) and ve.confirm_ok(True, ' web01 ', VM)
    assert ve.plan('delete', [], VM)['destructive']


@pytest.mark.parametrize('op, msg', [
    ({'op': 'disk_resize', 'device': 'd1', 'capacity': 40 * GIB}, 'only grow'),
    ({'op': 'disk_resize', 'device': 'other', 'capacity': 60 * GIB}, 'does not belong'),
    ({'op': 'nic_set', 'device': 'n1', 'fields': {'vlan': 5000}}, 'VLAN'),
    ({'op': 'nic_set', 'device': 'n1', 'fields': {'macAddress': 'nonsense'}}, 'MAC'),
    ({'op': 'vm', 'fields': {'mem': 1}}, 'memory'),
    ({'op': 'vm', 'fields': {'name': '  '}}, 'empty'),
    ({'op': 'vm', 'fields': {'bootDevices': ['someone-elses']}}, 'own devices'),
    ({'op': 'iso', 'device': 'd1', 'iso_uuid': 'x'}, 'CD-ROM'),
    ({'op': 'format_c'}, 'unknown'),
])
def test_refused(op, msg):
    with pytest.raises(ve.EditError, match=msg):
        _plan(op)


def test_power_action_is_validated():
    assert ve.plan('power', [{'op': 'power', 'action': 'SHUTDOWN'}], VM)['ops'] == [{'op': 'power', 'action': 'SHUTDOWN'}]
    with pytest.raises(ve.EditError):
        ve.plan('power', [{'op': 'power', 'action': 'LIVEMIGRATE'}], VM)


# ------------------------------------------------------------------ running
class FakeHC:
    """Records calls; the guest stops after `stops_after` state reads (None = never)."""
    def __init__(self, stops_after=1, fail_on=None):
        self.calls, self.reads, self.state = [], 0, 'RUNNING'
        self.stops_after, self.fail_on = stops_after, fail_on

    def get_vm(self, _):
        self.reads += 1
        if self.state == 'SHUTTING' and self.stops_after is not None and self.reads > self.stops_after:
            self.state = 'SHUTOFF'
        return {'state': self.state}

    def vm_action(self, _, action):
        self.calls.append(action)
        if action == 'SHUTDOWN':
            self.state = 'SHUTTING'
        elif action == 'STOP':
            self.state = 'SHUTOFF'
        elif action == 'START':
            self.state = 'RUNNING'
        return ''

    def patch_vm(self, _, **f):
        if self.fail_on == 'vm':
            raise ve.HyperCoreError('HyperCore API 400 on /VirDomain: nope', 400)
        self.calls.append(('patch_vm', f))
        return ''

    def create_snapshot(self, *a, **k):
        self.calls.append('snapshot')
        return '', 'snap'

    def delete_block_device(self, d):
        self.calls.append(('delete_disk', d))
        return ''


@pytest.fixture
def harness(monkeypatch):
    store = {}

    def make(ops, cycle=True, force=False, kind='edit', hc=None):
        store['ch'] = {'id': 1, 'cluster_id': 'c', 'vm_uuid': 'vm-1', 'kind': kind, 'ops': ops, 'cycle': cycle,
                       'force_off': force, 'shutdown_timeout_min': 1}
        store['hc'] = hc or FakeHC()
        return store

    monkeypatch.setattr(ve, '_claim', lambda i: True)
    monkeypatch.setattr(ve, 'get', lambda i: store['ch'])
    monkeypatch.setattr(ve, '_progress', lambda i, r: None)
    monkeypatch.setattr(ve, '_finish', lambda i, st, r, err: store.update(status=st, result=r, error=err))
    monkeypatch.setattr(ve.jobs, 'client_for', lambda cid: store['hc'])
    monkeypatch.setattr(ve.jobs, 'preflight', lambda hc, cid: {'ok': True})
    monkeypatch.setattr(ve.jobs, 'retry_transient', lambda fn, what, **k: fn())
    monkeypatch.setattr(ve, 'wait_task', lambda *a, **k: None)
    monkeypatch.setattr(ve.time, 'sleep', lambda s: None)
    t = [0.0]
    monkeypatch.setattr(ve.time, 'monotonic', lambda: t.__setitem__(0, t[0] + 10) or t[0])
    return make


def test_cycle_is_shutdown_apply_start(harness):
    st = harness([{'op': 'vm', 'fields': {'mem': 8 * GIB}}])
    ve.run(1)
    assert st['status'] == 'done'
    assert st['hc'].calls == ['SHUTDOWN', ('patch_vm', {'mem': 8 * GIB}), 'START']


def test_guest_that_will_not_stop_aborts_and_keeps_running(harness):
    st = harness([{'op': 'vm', 'fields': {'mem': 8 * GIB}}], hc=FakeHC(stops_after=None))
    ve.run(1)
    assert st['status'] == 'failed' and 'did not shut down' in st['error']
    assert st['hc'].calls == ['SHUTDOWN']                    # nothing changed, no power off, no start needed


def test_force_off_after_the_timeout(harness):
    st = harness([{'op': 'vm', 'fields': {'mem': 8 * GIB}}], force=True, hc=FakeHC(stops_after=None))
    ve.run(1)
    assert st['status'] == 'done'
    assert st['hc'].calls == ['SHUTDOWN', 'STOP', ('patch_vm', {'mem': 8 * GIB}), 'START']


def test_a_failed_change_still_starts_the_vm_again(harness):
    st = harness([{'op': 'vm', 'fields': {'mem': 8 * GIB}}], hc=FakeHC(fail_on='vm'))
    ve.run(1)
    assert st['status'] == 'failed' and st['hc'].calls[-1] == 'START'


def test_snapshot_before_a_disk_is_removed(harness):
    st = harness([{'op': 'disk_remove', 'device': 'd1', 'label': 'd1'}])
    ve.run(1)
    assert st['hc'].calls == ['SHUTDOWN', 'snapshot', ('delete_disk', 'd1'), 'START']


def test_live_only_changes_never_shut_down(harness):
    st = harness([{'op': 'vm', 'fields': {'name': 'x'}}], cycle=False)
    ve.run(1)
    assert st['hc'].calls == [('patch_vm', {'name': 'x'})]


def test_a_stopped_vm_stays_off(harness):
    hc = FakeHC()
    hc.state = 'SHUTOFF'
    st = harness([{'op': 'vm', 'fields': {'mem': 8 * GIB}}], hc=hc)
    ve.run(1)
    assert st['hc'].calls == [('patch_vm', {'mem': 8 * GIB})]


def test_a_lost_shutdown_request_is_sent_again(harness, monkeypatch):
    """A guest that is still booting ignores the power button (Lab 241): the request is repeated."""
    monkeypatch.setattr(ve, 'SHUTDOWN_REPEAT', 20)
    hc = FakeHC(stops_after=None)
    orig = hc.vm_action
    def action(vm, a):
        if a == 'SHUTDOWN' and hc.calls.count('SHUTDOWN') >= 1:
            hc.stops_after = hc.reads              # the second press is heard
        return orig(vm, a)
    hc.vm_action = action
    st = harness([{'op': 'vm', 'fields': {'mem': 8 * GIB}}], hc=hc)
    ve.run(1)
    assert st['status'] == 'done' and hc.calls.count('SHUTDOWN') == 2 and hc.calls[-1] == 'START'


# ------------------------------------------------------------------ new VMs
IMGS = [{'uuid': 'img-1', 'name': 'noble.img', 'capacity': 3 * GIB}]
ISOS = [{'uuid': 'iso-1', 'name': 'install.iso'}]


def test_new_vm_spec_is_normalised():
    p = ve.plan_create({'name': ' test01 ', 'firmware': 'tpm', 'secure_boot': True, 'numVCPU': 2, 'mem': 2 * GIB,
                        'disks': [{'capacity': 1 * GIB, 'image_uuid': 'img-1'}, {'type': 'SCSI_DISK', 'capacity': 5 * GIB}],
                        'nics': [{'vlan': 102}], 'iso_uuid': 'iso-1', 'start': True}, ISOS, IMGS, [])
    sp = p['ops'][0]['spec']
    assert sp['name'] == 'test01' and sp['firmware'] == 'tpm' and sp['secure_boot']
    assert sp['disks'][0]['capacity'] == 3 * GIB, 'an image disk is at least as big as the image'
    assert sp['nics'] == [{'type': 'VIRTIO', 'vlan': 102, 'connected': True}] and sp['iso_name'] == 'install.iso'


def test_bios_never_gets_secure_boot():
    assert not ve.plan_create({'name': 'x', 'firmware': 'bios', 'secure_boot': True}, [], [], [])['ops'][0]['spec']['secure_boot']


@pytest.mark.parametrize('spec, msg', [
    ({'name': ''}, 'name'),
    ({'name': 'x', 'firmware': 'coreboot'}, 'firmware'),
    ({'name': 'x', 'disks': [{'capacity': GIB}, {'capacity': GIB, 'image_uuid': 'img-1'}]}, 'first disk'),
    ({'name': 'x', 'disks': [{'capacity': GIB, 'image_uuid': 'nope'}]}, 'image'),
    ({'name': 'x', 'iso_uuid': 'nope'}, 'ISO'),
    ({'name': 'x', 'nics': [{'vlan': 9999}]}, 'VLAN'),
])
def test_new_vm_refused(spec, msg):
    with pytest.raises(ve.EditError, match=msg):
        ve.plan_create(spec, ISOS, IMGS, [])


def test_snapshot_and_clone_specs():
    p = ve.plan('snapshot', [{'label': ' before update ', 'quiesce': True, 'retain_days': 7}], VM)
    assert p['ops'][0] == {'op': 'snapshot', 'label': 'before update', 'quiesce': True, 'replicate': False, 'retain_days': 7}
    vm = dict(VM, snapUUIDs=['s1'])
    c = ve.plan('clone', [{'name': 'web01-clone', 'snap_uuid': 's1', 'start': True}], vm)['ops'][0]
    assert c['name'] == 'web01-clone' and c['snap_uuid'] == 's1' and c['start']
    with pytest.raises(ve.EditError, match='does not belong'):
        ve.plan('clone', [{'name': 'x', 'snap_uuid': 'other'}], vm)
    with pytest.raises(ve.EditError, match='label'):
        ve.plan('snapshot', [{'label': ''}], VM)


def test_task_messages_get_their_parameters():
    import importlib, sys as _s
    st = {'state': 'ERROR', 'formattedMessage': 'Unable to replicate vm %@ to remote connection %@ due to a pre-existing replication',
          'messageParameters': ['MineC01', 'WDvCN11']}
    inc = _s.modules.get('incremental') or ve.wait_task.__globals__
    task_message = ve.wait_task.__globals__['task_message']
    assert task_message(st) == 'Unable to replicate vm MineC01 to remote connection WDvCN11 due to a pre-existing replication'
    assert task_message({'formattedMessage': 'x %@ %@', 'messageParameters': ['a']}) == 'x a ?'


def test_a_leftover_replica_is_explained():
    o = {'op': 'replication', 'action': 'create', 'text': 'start replicating to WDvCN11'}
    err = ('start replicating to WDvCN11 failed: Unable to replicate vm MineC01 to remote connection d841 due to a '
           'pre-existing replication on the remote connection')
    msg = ve.explain(o, err)
    assert msg.startswith('WDvCN11 still holds the replica') and 'administrator of WDvCN11' in msg and 'pre-existing' in msg
    assert ve.explain({'op': 'vm'}, 'boom') == 'boom'
