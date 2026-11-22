"""A folder's backup target is chosen by a root target manager only (1.8.1).

Before, PUT /api/folders/<id> took any share_id from whoever held backup.targets.manage on that folder (an
Admin of the customer above it has it), although the targets are only listed at root: a customer admin could
point their folder's backups at another tenant's target by guessing its id.
"""
from __future__ import annotations

import pytest

import rbac

CUST, FOLD = 'aaaaaaaa-0000-0000-0000-000000000001', 'aaaaaaaa-0000-0000-0000-000000000002'


def _tree():
    t = rbac.Tree()
    t.parent.update({CUST: None, FOLD: CUST})
    t.level.update({CUST: 'customer', FOLD: 'folder'})
    return t


@pytest.fixture
def put_folder(dash, monkeypatch):
    calls = []
    state = {'grants': []}
    monkeypatch.setattr(dash.access_db, 'access_for',
                        lambda uid, superuser=False: rbac.Access(state['grants'], _tree(), user_id=uid))
    monkeypatch.setattr(dash.access_db, 'user_state', lambda uid: {'username': 'u@x.example', 'enabled': True})
    monkeypatch.setattr(dash.cluster_registry, 'update_folder', lambda fid, **kw: calls.append(kw))
    monkeypatch.setattr(dash, '_invalidate_registry_cache', lambda: None)
    client = dash.app.test_client()
    with client.session_transaction() as s:
        s['user_id'], s['username'] = 'bbbbbbbb-0000-0000-0000-000000000001', 'u@x.example'

    def put(grants, body):
        state['grants'] = grants
        calls.clear()
        return client.put(f'/api/folders/{FOLD}', json=body), list(calls)
    return put


ADMIN = rbac.BUILTIN_ROLES['admin']['perms']


def test_a_customer_admin_cannot_pick_a_target(put_folder):
    r, calls = put_folder([rbac.Grant(ADMIN, 'node', node_id=CUST)], {'share_id': 7})
    assert r.status_code == 403 and calls == []
    r, calls = put_folder([rbac.Grant(ADMIN, 'node', node_id=CUST)], {'name': 'Renamed'})
    assert r.status_code == 200 and calls[0]['name'] == 'Renamed', 'renaming stays theirs'


def test_a_root_target_manager_picks_one(put_folder):
    r, calls = put_folder([rbac.Grant(frozenset({'backup.targets.manage', 'tree.manage'}), 'root')], {'share_id': '7'})
    assert r.status_code == 200 and calls[0]['share_id'] == 7
    r, calls = put_folder([rbac.Grant(ADMIN, 'root')], {'share_id': 'x'})
    assert r.status_code == 400 and calls == []
    r, calls = put_folder([rbac.Grant(ADMIN, 'root')], {'share_id': None})
    assert r.status_code == 200 and calls[0]['share_id'] is None


def test_an_unknown_target_is_a_404(put_folder, dash, monkeypatch):
    def fk(fid, **kw):
        raise Exception('insert or update on table "folders" violates foreign key constraint "folders_share_id_fkey"')
    monkeypatch.setattr(dash.cluster_registry, 'update_folder', fk)
    r, _ = put_folder([rbac.Grant(ADMIN, 'root')], {'share_id': 999})
    assert r.status_code == 404
