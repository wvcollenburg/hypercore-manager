"""A clone cannot carry an access-granting tag past the tag guard (1.8.1).

The guard took the tags of the LAST op with a `tags` key, while the backup service clones from ops[0]:
{"kind": "clone", "ops": [{"name": "c1", "tags": "<access tag>"}, {"tags": ""}]} passed the guard and made
a clone carrying a tag that hands out access.
"""
from __future__ import annotations

import pytest

import rbac

CID = 'cccccccc-0000-0000-0000-000000000009'


@pytest.fixture
def guard(dash, monkeypatch):
    grants = [rbac.Grant(rbac.expand(['vm.clone', 'vm.snapshot']), 'cluster', cluster_id=CID)]
    monkeypatch.setattr(dash.access_db, 'access_for', lambda uid, superuser=False: rbac.Access(grants, rbac.Tree(), user_id=uid))
    monkeypatch.setattr(dash.access_db, 'scoped_tags', lambda cid: {'finance': ['anna: Viewer']})
    monkeypatch.setattr(dash, '_vm_tags', lambda cid, vm: [])
    return dash


def _check(dash, payload):
    with dash.app.test_request_context('/api/backup/vmedit/changes', method='POST'):
        _, problem = dash._backup_check('POST', 'vmedit/changes', CID, dict(payload, vm_uuid='vm1', cluster_id=CID))
        return None if problem is None else (problem[1], problem[0].get_json()['error'])


def test_the_first_ops_tags_are_checked(guard):
    status, why = _check(guard, {'kind': 'clone', 'ops': [{'name': 'c1', 'tags': 'finance'}]})
    assert status == 403 and 'finance' in why


def test_a_second_op_cannot_hide_the_first(guard):
    status, why = _check(guard, {'kind': 'clone', 'ops': [{'name': 'c1', 'tags': 'finance'}, {'tags': ''}]})
    assert status == 400 and 'one operation' in why
    status, _ = _check(guard, {'kind': 'snapshot', 'ops': [{'label': 'a'}, {'label': 'b'}]})
    assert status == 400


def test_a_plain_clone_passes(guard):
    assert _check(guard, {'kind': 'clone', 'ops': [{'name': 'c1', 'tags': 'web'}]}) is None
