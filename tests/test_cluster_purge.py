"""Purging a cluster keeps the backup catalogue, and a removed cluster is not edited back to life (1.8.1)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import rbac

CID = 'cccccccc-0000-0000-0000-0000000000bb'


@pytest.fixture
def admin(dash, monkeypatch):
    grants = [rbac.Grant(rbac.BUILTIN_ROLES['admin']['perms'], 'root')]
    monkeypatch.setattr(dash.access_db, 'access_for', lambda uid, superuser=False: rbac.Access(grants, rbac.Tree(), user_id=uid))
    monkeypatch.setattr(dash.access_db, 'user_state', lambda uid: {'username': 'root@x', 'enabled': True})
    monkeypatch.setattr(dash.cluster_registry, 'get', lambda cid: SimpleNamespace(cluster_id=CID, name='Old', host='h',
                                                                                   folder_id=None))
    monkeypatch.setattr(dash, '_audit', lambda *a, **k: None)
    monkeypatch.setattr(dash, '_invalidate_registry_cache', lambda: None)
    deleted = []
    monkeypatch.setattr(dash.cluster_registry, 'delete', lambda cid, purge=False: deleted.append(purge))
    c = dash.app.test_client()
    with c.session_transaction() as s:
        s['user_id'], s['username'] = 'u-root', 'root@x'
    return c, deleted


def test_purge_is_refused_while_backup_chains_exist(dash, monkeypatch, admin):
    c, deleted = admin
    monkeypatch.setattr(dash, '_backup_chain_count', lambda cid: 3)
    r = c.delete(f'/api/clusters/{CID}?purge=1')
    assert r.status_code == 409 and 'backup chains' in r.get_json()['error'] and deleted == []
    assert c.delete(f'/api/clusters/{CID}?purge=0').status_code == 200 and deleted == [False], 'remove without purge'


def test_editing_a_removed_cluster_is_a_404(dash, monkeypatch, admin):
    c, _ = admin
    monkeypatch.setattr(dash.cluster_registry, 'update', lambda cid, **kw: 0)
    assert c.put(f'/api/clusters/{CID}', json={'enabled': True}).status_code == 404
