"""Names and places above the reader stay hidden (1.8.1).

Before: who muted an alert (/api/cluster-alerts muted_by) and who created, requested or updated something
in the backup service's answers were shown to every reader, also when that person sits above them (an MSP's
staff named to a customer); roles defined above the reader showed where and by whom; and plans and known
imports showed `effective_base`, the target path, to users without backup.targets.manage.
"""
from __future__ import annotations

import pytest

import rbac

CUST, FOLD = 'aaaaaaaa-0000-0000-0000-0000000000c1', 'aaaaaaaa-0000-0000-0000-0000000000f1'
MSP = 'aaaaaaaa-0000-0000-0000-0000000000a1'


def test_names_in_backup_answers_are_hidden_recursively(dash):
    data = {'plans': [{'id': 1, 'created_by': 'msp-staff@x', 'runs': [{'requested_by': 'me@x'}]}],
            'notice': {'muted_by': 'guest:Bob'}}
    dash._hide_backup_names(data, {'me@x'})
    assert data['plans'][0]['created_by'] is None and data['plans'][0]['runs'][0]['requested_by'] == 'me@x'
    assert data['notice']['muted_by'] == 'guest:Bob', 'a guest is no account and stays named'


def test_the_target_path_is_stripped(dash):
    assert dash._strip_sensitive({'effective_base': 'smb://nas/tenant', 'name': 'p'}) == {'name': 'p'}


def _tree():
    t = rbac.Tree()
    t.parent.update({MSP: None, CUST: MSP, FOLD: CUST})
    t.level.update({MSP: 'msp', CUST: 'customer', FOLD: 'folder'})
    t.name.update({MSP: 'MSP', CUST: 'Contoso', FOLD: 'Prod'})
    return t


def test_roles_defined_above_the_reader_do_not_say_where_or_by_whom(dash, monkeypatch):
    grants = [rbac.Grant(rbac.BUILTIN_ROLES['admin']['perms'], 'node', node_id=CUST)]
    acc = rbac.Access(grants, _tree(), user_id='u1')
    db = dash.access_db
    monkeypatch.setattr(db, 'access_for', lambda uid, superuser=False: acc)
    monkeypatch.setattr(db, 'user_state', lambda uid: {'username': 'ca@x', 'enabled': True})
    monkeypatch.setattr(db, 'roles', lambda: [
        {'role_id': 'r1', 'name': 'MSP helpdesk', 'node_id': MSP, 'created_by': 'msp-boss@x', 'copied_from': 'Engineer',
         'in_use': 40, 'builtin_key': None, 'permissions': ['vm.view']},
        {'role_id': 'r2', 'name': 'Contoso ops', 'node_id': CUST, 'created_by': 'ca@x', 'copied_from': None,
         'in_use': 2, 'builtin_key': None, 'permissions': ['vm.view']}])
    for name in ('groups', 'users', 'unacknowledged_permissions'):
        monkeypatch.setattr(db, name, lambda: [])
    monkeypatch.setattr(db, 'nodes', lambda: [])
    monkeypatch.setattr(db, 'manageable_user_ids', lambda a: set())
    monkeypatch.setattr(dash.cluster_registry, 'list_all', lambda: [])
    c = dash.app.test_client()
    with c.session_transaction() as s:
        s['user_id'], s['username'] = 'u1', 'ca@x'
    roles = {r['role_id']: r for r in c.get('/api/access/overview').get_json()['roles']}
    assert roles['r1']['node_id'] is None and roles['r1']['created_by'] is None and roles['r1']['in_use'] is None
    assert roles['r1']['defined_above'] is True
    assert roles['r2']['node_id'] == CUST and roles['r2']['created_by'] == 'ca@x'
