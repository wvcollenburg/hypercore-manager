"""What the dashboard forwards when a backup plan is created or changed (1.8.1).

Before, `vm_name` from the browser went through: it is the export prefix and the key a plan's pruning
deletes by, so a user allowed one VM could name another VM and have its exports pruned. And path_uri_base,
smb_user, smb_password and prune_path were accepted from anyone with backup.schedule: when the folder had no
target, they could send a VM's export to an SMB server of their own.
"""
from __future__ import annotations

import io
import json

import pytest

import rbac

CID = 'cccccccc-0000-0000-0000-000000000001'
VM_MINE, VM_OTHER = 'vm-mine', 'vm-other'


class _Resp(io.BytesIO):
    status = 200
    def __enter__(self): return self
    def __exit__(self, *a): return False


@pytest.fixture
def forward(dash, monkeypatch):
    sent = []
    state = {'grants': [rbac.Grant(rbac.expand(['backup.schedule']), 'vm', cluster_id=CID, vm_uuid=VM_MINE)]}
    monkeypatch.setattr(dash.access_db, 'access_for', lambda uid, superuser=False: rbac.Access(state['grants'], rbac.Tree(), user_id=uid))
    monkeypatch.setattr(dash.access_db, 'user_state', lambda uid: {'username': 'u@x.example', 'enabled': True})
    monkeypatch.setattr(dash, '_cluster_id', lambda: CID)
    monkeypatch.setattr(dash, '_visible_clusters', lambda perm=None: [type('C', (), {'cluster_id': CID})()])
    monkeypatch.setattr(dash, '_backup_check', lambda method, sub, cid, payload: (None, None))
    monkeypatch.setattr(dash, '_vm_index_rows', lambda cid, fresh=False: [{'uuid': VM_MINE, 'name': 'web01'},
                                                                         {'uuid': VM_OTHER, 'name': 'sql01'}])

    def urlopen(rq, timeout=None):
        sent.append((rq.full_url, json.loads(rq.data.decode()) if rq.data else None))
        return _Resp(b'{"ok": true}')
    monkeypatch.setattr(dash.urllib.request, 'urlopen', urlopen)
    client = dash.app.test_client()
    with client.session_transaction() as s:
        s['user_id'], s['username'] = 'dddddddd-0000-0000-0000-000000000001', 'u@x.example'
    return client, sent, state


def test_the_vm_name_is_the_clusters_not_the_browsers(forward):
    client, sent, _ = forward
    r = client.post(f'/api/backup/plans?cluster={CID}', json={'vm_uuid': VM_MINE, 'vm_name': 'sql01'})
    assert r.status_code == 200
    assert sent[-1][1]['vm_name'] == 'web01'
    client.post(f'/api/backup/policies/3/members?cluster={CID}', json={'vm_uuid': VM_MINE, 'vm_name': 'sql01'})
    assert sent[-1][1]['vm_name'] == 'web01'
    client.put(f'/api/backup/plans/5?cluster={CID}', json={'vm_name': 'sql01', 'retention': 3})
    assert 'vm_name' not in sent[-1][1] and sent[-1][1]['retention'] == 3
    r = client.post(f'/api/backup/plans?cluster={CID}', json={'vm_uuid': 'vm-gone', 'vm_name': 'x'})
    assert r.status_code == 404


def test_a_destination_is_only_taken_from_a_root_target_manager(forward):
    client, sent, state = forward
    dest = {'path_uri_base': 'smb://evil.example/loot', 'smb_user': 'me', 'smb_password': 'pw', 'prune_path': '/x'}
    client.post(f'/api/backup/plans?cluster={CID}', json=dict(dest, vm_uuid=VM_MINE))
    assert not set(dest) & set(sent[-1][1])
    client.put(f'/api/backup/plans/5?cluster={CID}', json=dest)
    assert not set(dest) & set(sent[-1][1])
    state['grants'] = [rbac.Grant(rbac.BUILTIN_ROLES['admin']['perms'], 'root')]
    client.post(f'/api/backup/plans?cluster={CID}', json=dict(dest, vm_uuid=VM_MINE))
    assert sent[-1][1]['path_uri_base'] == 'smb://evil.example/loot'
