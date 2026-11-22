"""Only someone who outranks an account changes the account itself (1.8.1).

Before, any root-level role with users.manage (not Admin) could set any account's password, reset its 2FA
or change its login address, and then sign in as a root Admin; disabling an account skipped the guard that
keeps one enabled root Admin (delete and assignment removal had it).
"""
from __future__ import annotations

import pytest

import access_api
import access_db
import rbac

ADMIN = rbac.Grant(rbac.BUILTIN_ROLES['admin']['perms'], 'root')
USER_MANAGER = rbac.Grant(rbac.expand(['users.manage', 'groups.manage']), 'root')
HELPDESK = rbac.Grant(rbac.expand(['users.manage', 'cluster.view']), 'root')


def tree():
    t = rbac.Tree()
    t.parent.update({'msp': None, 'cust': 'msp', 'fold': 'cust'})
    t.level.update({'msp': 'msp', 'cust': 'customer', 'fold': 'folder'})
    t.cluster_folder['c1'] = 'fold'
    return t


def acc(*grants, user_id='me'):
    return rbac.Access(list(grants), tree(), user_id=user_id)


def test_a_root_admin_outranks_everyone():
    assert access_api.outranks(acc(ADMIN), [ADMIN], target_is_admin=True)


def test_a_user_manager_does_not_outrank_an_admin():
    assert not access_api.outranks(acc(USER_MANAGER), [ADMIN])
    assert not access_api.outranks(acc(USER_MANAGER), [], target_is_admin=True), 'a legacy admin row'


def test_a_user_manager_outranks_whoever_holds_only_what_they_hold():
    viewer_of_fold = rbac.Grant(rbac.expand(['cluster.view']), 'node', node_id='fold')
    assert access_api.outranks(acc(HELPDESK), [viewer_of_fold])
    assert access_api.outranks(acc(HELPDESK), []), 'an account without access'
    engineer = rbac.Grant(rbac.BUILTIN_ROLES['engineer']['perms'], 'cluster', cluster_id='c1')
    assert not access_api.outranks(acc(HELPDESK), [viewer_of_fold, engineer])


# ------------------------------------------------------------------ the routes
TARGET = {'user_id': '11111111-1111-1111-1111-111111111111', 'username': 'boss@x.example', 'role': 'user',
          'enabled': True, 'awaiting_email': False, 'awaiting_access': False, 'groups': [], 'legacy_folders': []}


@pytest.fixture
def as_manager(dash, monkeypatch):
    """Signed in as a root user manager without Admin; the target holds Admin on the whole installation."""
    calls = []
    monkeypatch.setattr(access_db, 'access_for', lambda uid, superuser=False: acc(USER_MANAGER, user_id=uid))
    monkeypatch.setattr(access_db, 'user_state', lambda uid: {'username': 'mgr@x.example', 'enabled': True})
    monkeypatch.setattr(access_db, 'user', lambda uid: dict(TARGET) if uid == TARGET['user_id'] else None)
    monkeypatch.setattr(access_db, 'manageable_user_ids', lambda a: None)
    monkeypatch.setattr(access_db, 'grants_of_principal', lambda user_id=None, group_id=None: [ADMIN])
    monkeypatch.setattr(access_db, 'update_user', lambda *a, **k: calls.append(('update', a, k)))
    monkeypatch.setattr(access_db, 'delete_user', lambda *a, **k: calls.append(('delete', a)))
    monkeypatch.setattr(access_db, 'invalidate', lambda: None)
    monkeypatch.setattr(access_db, '_connect', lambda: _Conn())          # the audit row
    import user_db
    monkeypatch.setattr(user_db, 'set_password', lambda *a: calls.append(('password', a)))
    monkeypatch.setattr(user_db, 'disable_otp', lambda *a: calls.append(('otp', a)))
    monkeypatch.setattr(user_db, 'bump_session_gen', lambda *a: 1, raising=False)
    client = dash.app.test_client()
    with client.session_transaction() as s:
        s['user_id'], s['username'] = '22222222-2222-2222-2222-222222222222', 'mgr@x.example'
    return client, calls


def test_a_user_manager_cannot_take_over_an_admin(as_manager):
    client, calls = as_manager
    uid = TARGET['user_id']
    assert client.put(f'/api/access/users/{uid}/password', json={'new_password': 'Taken-over-123'}).status_code == 403
    assert client.post(f'/api/access/users/{uid}/otp-reset').status_code == 403
    assert client.put(f'/api/access/users/{uid}', json={'email': 'me@evil.example', 'password': 'Taken-over-123'}).status_code == 403
    assert client.put(f'/api/access/users/{uid}', json={'enabled': False}).status_code == 403
    assert client.delete(f'/api/access/users/{uid}').status_code == 403
    assert calls == []


def test_a_user_manager_still_changes_an_account_below_them(as_manager, monkeypatch):
    client, calls = as_manager
    monkeypatch.setattr(access_db, 'grants_of_principal', lambda user_id=None, group_id=None: [])
    r = client.put(f"/api/access/users/{TARGET['user_id']}/password", json={'new_password': 'Fine-pass-123'})
    assert r.status_code == 200 and calls[0][0] == 'password'


# ------------------------------------------------------------------ disabling keeps one root admin
class _Cur:
    def __init__(self, conn): self.conn = conn
    def __enter__(self): return self
    def __exit__(self, *a): return False

    def execute(self, sql, params=None):
        self.conn.sql.append(sql)
        self.last = sql

    def fetchall(self):
        if 'builtin_key' in self.last:                       # the last-root-admin guard: only the target is left
            return [{'aid': 'a1', 'uid': TARGET['user_id']}]
        return []

    def fetchone(self):
        return None


class _Conn:
    def __init__(self): self.sql, self.committed = [], False
    def cursor(self): return _Cur(self)
    def commit(self): self.committed = True
    def close(self): pass


def test_disabling_the_last_root_admin_is_refused(monkeypatch):
    conn = _Conn()
    monkeypatch.setattr(access_db, '_connect', lambda: conn)
    monkeypatch.setattr(access_db, 'user', lambda uid: dict(TARGET))
    monkeypatch.setattr(access_db, 'invalidate', lambda: None)
    with pytest.raises(access_db.AccessError) as e:
        access_db.update_user(TARGET['user_id'], 'someone', enabled=False)
    assert e.value.status == 409 and not conn.committed
    assert not any('SET enabled' in s for s in conn.sql)
