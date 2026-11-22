"""A write acts on the cluster it names, never on the user's first one (1.8.1).

Before, a non-GET cluster-scoped request without ?cluster= silently acted on the user's first visible
cluster, and the backup and console forwards (declared @requires(None)) fell back to the first cluster
also when the request named one the user may not use.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import rbac

A, B, C = ('aaaaaaaa-1111-1111-1111-00000000000' + x for x in 'abc')


@pytest.fixture
def client(dash, monkeypatch):
    grants = [rbac.Grant(rbac.BUILTIN_ROLES['admin']['perms'] - rbac.ROOT_LEVEL - rbac.NODE_LEVEL, 'cluster', cluster_id=A),
              rbac.Grant(rbac.expand(['cluster.view']), 'cluster', cluster_id=B)]
    monkeypatch.setattr(dash.access_db, 'access_for', lambda uid, superuser=False: rbac.Access(grants, rbac.Tree(), user_id=uid))
    monkeypatch.setattr(dash.access_db, 'user_state', lambda uid: {'username': 'u@x.example', 'enabled': True})
    monkeypatch.setattr(dash, '_enabled_clusters', lambda: [SimpleNamespace(cluster_id=c) for c in (A, B, C)])
    forwarded = []
    monkeypatch.setattr(dash, '_backup_check', lambda method, sub, cid, payload: (forwarded.append(cid), (None, None))[1])
    monkeypatch.setattr(dash, '_console_call', lambda *a, **k: (200, b'{}', 'application/json'))
    c = dash.app.test_client()
    with c.session_transaction() as s:
        s['user_id'], s['username'] = 'ffffffff-0000-0000-0000-000000000001', 'u@x.example'
    return c, forwarded


def test_a_write_without_a_cluster_is_refused(client):
    c, forwarded = client
    assert c.post('/api/sentinel/trend/run').status_code == 400
    r = c.post('/api/backup/plans', json={'vm_uuid': 'v'})
    assert r.status_code == 400 and r.get_json()['error'] == 'cluster is required' and forwarded == []
    assert c.post('/api/console/check/run', json={'vm_uuid': 'v'}).status_code == 400


def test_a_write_naming_a_cluster_not_the_users_is_refused(client):
    c, forwarded = client
    assert c.post(f'/api/sentinel/trend/run?cluster={C}').status_code == 403
    assert c.post(f'/api/sentinel/trend/run?cluster={B}').status_code == 403, 'B is only viewed'
    assert c.post(f'/api/backup/plans?cluster={C}', json={'vm_uuid': 'v'}).status_code == 403
    assert c.post('/api/backup/plans?cluster=garbage', json={'vm_uuid': 'v'}).status_code == 403
    assert c.post(f'/api/backup/plans/5/run?cluster={C}').status_code == 403, 'an id route may not name a wrong cluster'
    assert c.post(f'/api/console/check/run?cluster={C}', json={}).status_code == 403
    assert forwarded == []


def test_the_right_cluster_passes_and_reads_still_fall_back(client):
    c, forwarded = client
    c.post(f'/api/backup/plans?cluster={A}', json={'vm_uuid': 'v'})
    c.post('/api/backup/plans/5/run')                     # found by id: checked on its own cluster
    assert forwarded == [A, A]
    assert c.get(f'/api/backup/plans?cluster={C}').status_code != 403
