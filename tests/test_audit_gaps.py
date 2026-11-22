"""Actions that were missing from the access audit (1.8.1): sign-ins (and failed ones), one's own 2FA on and
off, cluster register/edit/delete/purge, moving a cluster to another folder, a folder's backup target,
Foresight tunables and applying an update."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import rbac
from test_login_limits import FakeFailures

CID = 'cccccccc-0000-0000-0000-0000000000aa'
FOLD = 'aaaaaaaa-0000-0000-0000-0000000000f1'


@pytest.fixture
def audited(dash, monkeypatch):
    rows = []
    monkeypatch.setattr(dash, '_audit', lambda action, target, node_id=None, detail=None, actor=None:
                        rows.append((action, target, node_id, detail, actor)))
    return rows


def test_sign_ins_and_failures_are_audited_with_the_peer_address(dash, monkeypatch, audited):
    db = FakeFailures()
    db.users['anna@x.example']['otp_enabled'] = False
    db.install(monkeypatch, dash, lambda code: False)
    c = dash.app.test_client()
    c.post('/login', data={'username': 'anna@x.example', 'password': 'nope'}, environ_base={'REMOTE_ADDR': '10.1.1.1'},
           headers={'X-Forwarded-For': '6.6.6.6'})
    c.post('/login', data={'username': 'anna@x.example', 'password': 'right-password'}, environ_base={'REMOTE_ADDR': '10.1.1.1'})
    assert [(a, t, d['ip']) for a, t, _, d, _ in audited] == [('login.failed', 'anna@x.example', '10.1.1.1'),
                                                             ('login', 'anna@x.example', '10.1.1.1')]
    assert not any('nope' in str(r) for r in audited), 'never the password'


def test_cluster_changes_are_audited(dash, monkeypatch, audited):
    admin = [rbac.Grant(rbac.BUILTIN_ROLES['admin']['perms'], 'root')]
    monkeypatch.setattr(dash.access_db, 'access_for', lambda uid, superuser=False: rbac.Access(admin, rbac.Tree(), user_id=uid))
    monkeypatch.setattr(dash.access_db, 'user_state', lambda uid: {'username': 'root@x', 'enabled': True})
    cluster = SimpleNamespace(cluster_id=CID, name='Prod', host='10.0.0.1', folder_id=None, to_dict=lambda: {})
    monkeypatch.setattr(dash.cluster_registry, 'get', lambda cid: cluster)
    monkeypatch.setattr(dash.cluster_registry, 'update', lambda cid, **kw: None)
    monkeypatch.setattr(dash.cluster_registry, 'delete', lambda cid, purge=False: None)
    monkeypatch.setattr(dash, '_invalidate_registry_cache', lambda: None)
    monkeypatch.setattr(dash, '_cluster_payload', lambda c, extras=None: {})
    monkeypatch.setattr(dash, '_backup_chain_count', lambda cid: 0)
    c = dash.app.test_client()
    with c.session_transaction() as s:
        s['user_id'], s['username'] = 'u-root', 'root@x'
    c.put(f'/api/clusters/{CID}', json={'name': 'Prod 2', 'password': 'new', 'folder_id': FOLD})
    c.delete(f'/api/clusters/{CID}?purge=1')
    actions = [r[0] for r in audited]
    assert actions == ['cluster.edit', 'cluster.move', 'cluster.purge']
    assert audited[0][3]['fields'] == ['name', 'password'] and 'new' not in str(audited[0])
    assert audited[1][3] == {'cluster_id': CID, 'from': None, 'to': FOLD}


def test_the_other_actions_write_an_audit_row(repo):
    src = (repo / 'dashboard' / 'app.py').read_text()
    for action in ('cluster.register', 'folder.target', 'tunables.foresight', 'install.update_apply',
                   'user.otp_enabled', 'user.otp_disabled', 'login.otp_failed'):
        assert f"_audit('{action}'" in src, action
