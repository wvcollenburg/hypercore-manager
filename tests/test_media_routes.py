"""The Media routes and the VM dialogs follow the media rules (dashboard/media_api.py, media.py).

Tree: MSP M, customers A and B, folder A1 under A with cluster C (the selected cluster). The database and the
backup service are fakes: config.media rows in a list, HyperCore's media in a dict.
"""
from __future__ import annotations

import contextlib
import json
import re

import pytest

import rbac

M, A, B, A1 = (f'aaaaaaaa-0000-0000-0000-00000000000{i}' for i in range(1, 5))
C = 'cccccccc-0000-0000-0000-000000000001'
ISO_TOP, ISO_A, ISO_M = (f'11111111-0000-0000-0000-00000000000{i}' for i in range(1, 4))
ENG = rbac.BUILTIN_ROLES['engineer']['perms']
VIEWER = rbac.BUILTIN_ROLES['viewer']['perms']
ADMIN = rbac.BUILTIN_ROLES['admin']['perms']


def _tree():
    t = rbac.Tree()
    for n, p, lvl in ((M, None, 'msp'), (A, M, 'customer'), (B, M, 'customer'), (A1, A, 'folder')):
        t.parent[n], t.level[n], t.name[n] = p, lvl, n[-1]
    t.cluster_folder[C] = A1
    return t


class _Cur:
    def __init__(self, db): self.db, self._rows = db, []
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, args=()):
        self.db['sql'].append((sql.split()[0], args))
        if sql.startswith('SELECT'):
            self._rows = [dict(r, assigned=json.dumps(r['assigned'])) for r in self.db['media']]
        elif sql.startswith('INSERT'):
            self.db['media'] = [r for r in self.db['media'] if r['hc_uuid'] != args[2]]
            self.db['media'].append({'kind': args[1], 'hc_uuid': args[2], 'name': args[3], 'owner': args[4],
                                     'assigned': list(args[5]) if len(args) > 5 and isinstance(args[5], list) else [],
                                     'uploaded_by': None, 'uploaded_at': None})
    def fetchall(self): return self._rows


class _Conn:
    def __init__(self, db): self.db = db
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def cursor(self): return _Cur(self.db)


@pytest.fixture
def env(dash, monkeypatch):
    db = {'media': [{'kind': 'iso', 'hc_uuid': ISO_A, 'name': 'a.iso', 'owner': A, 'assigned': [], 'uploaded_by': 'x', 'uploaded_at': None},
                    {'kind': 'iso', 'hc_uuid': ISO_M, 'name': 'm.iso', 'owner': M, 'assigned': [], 'uploaded_by': 'y', 'uploaded_at': None}],
          'sql': []}
    cluster = {'isos': [{'uuid': u, 'name': n, 'size': 1, 'ready': True, 'in_use': []}
                        for u, n in ((ISO_TOP, 'top.iso'), (ISO_A, 'a.iso'), (ISO_M, 'm.iso'))], 'disks': []}
    state = {'grants': [], 'backup': []}
    monkeypatch.setattr(dash.access_db, 'access_for', lambda uid, superuser=False: rbac.Access(state['grants'], _tree(), user_id=uid))
    monkeypatch.setattr(dash.access_db, 'user_state', lambda uid: {'username': 'u@x.example', 'enabled': True})
    monkeypatch.setattr(dash, '_pg', lambda: _Conn(db))
    monkeypatch.setattr(dash, '_audit', lambda *a, **k: None)
    monkeypatch.setattr(dash, '_enabled_clusters', lambda: [type('Cl', (), {'cluster_id': C, 'name': 'C', 'enabled': True})()])
    monkeypatch.setattr(dash, '_backup_env', lambda: ('http://backup:8082', 't'))

    class _Resp:
        def __init__(self, status, body): self.status, self._b = status, json.dumps(body).encode()
        def read(self): return self._b
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(rq, timeout=None):
        state['backup'].append((rq.get_method(), rq.full_url))
        if '/api/v1/media/upload' in rq.full_url:
            return _Resp(201, {'ok': True, 'uuid': 'new-uuid', 'name': 'n'})
        if '/api/v1/media' in rq.full_url:
            return _Resp(200, cluster)
        if '/api/v1/vmedit/options' in rq.full_url:
            return _Resp(200, {'isos': cluster['isos'], 'images': [], 'nodes': [], 'schedules': []})
        return _Resp(200, {'id': 1, 'summary': []})
    monkeypatch.setattr('urllib.request.urlopen', fake_urlopen)
    client = dash.app.test_client()
    with client.session_transaction() as s:
        s['user_id'], s['username'] = 'bbbbbbbb-0000-0000-0000-000000000001', 'u@x.example'

    def as_(*grants):
        state['grants'] = list(grants)
        state['backup'].clear()
        return client
    return type('Env', (), {'as_': staticmethod(as_), 'db': db, 'state': state})


def _names(r):
    return sorted(i['name'] for i in r.get_json()['isos'])


def test_the_list_shows_each_layer_its_own(env):
    assert _names(env.as_(rbac.Grant(ADMIN, 'root')).get(f'/api/media?cluster={C}')) == ['a.iso', 'm.iso', 'top.iso']
    assert _names(env.as_(rbac.Grant(ENG, 'node', node_id=A)).get(f'/api/media?cluster={C}')) == ['a.iso']
    assert _names(env.as_(rbac.Grant(ENG, 'node', node_id=A1)).get(f'/api/media?cluster={C}')) == ['a.iso']
    assert _names(env.as_(rbac.Grant(ENG, 'node', node_id=M)).get(f'/api/media?cluster={C}')) == ['m.iso'], \
        'the MSP sees its own upload, not its customer\'s'
    assert env.as_(rbac.Grant(VIEWER, 'node', node_id=A)).get(f'/api/media?cluster={C}').status_code == 403


def test_an_upload_records_the_uploaders_layer(env):
    c = env.as_(rbac.Grant(ENG, 'node', node_id=A))
    r = c.put(f'/api/media/upload?cluster={C}&kind=iso&name=win.iso', data=b'x' * 10, headers={'Content-Type': 'application/octet-stream'})
    assert r.status_code == 201, r.get_json()
    row = next(x for x in env.db['media'] if x['hc_uuid'] == 'new-uuid')
    assert row['owner'] == A and row['name'] == 'win.iso'


@pytest.mark.parametrize('grants,url,status', [
    ([rbac.Grant(VIEWER, 'node', node_id=A)], f'/api/media/upload?cluster={C}&kind=iso&name=x.iso', 403),
    ([rbac.Grant(ENG, 'node', node_id=B)], f'/api/media/upload?cluster={C}&kind=iso&name=x.iso', 403),
    ([rbac.Grant(ENG, 'node', node_id=A)], '/api/media/upload?kind=iso&name=x.iso', 400),
    ([rbac.Grant(ENG, 'node', node_id=A)], f'/api/media/upload?cluster={C}&kind=iso&name=../x.iso', 400),
    ([rbac.Grant(ENG, 'node', node_id=A)], f'/api/media/upload?cluster={C}&kind=disk&name=x.iso', 400),
])
def test_uploads_that_are_refused(env, grants, url, status):
    r = env.as_(*grants).put(url, data=b'x', headers={'Content-Type': 'application/octet-stream'})
    assert r.status_code == status, r.get_json()
    assert not any('media/upload' in u for _, u in env.state['backup']), 'nothing reached the backup service'


def test_assign_and_delete_belong_to_the_owner_layer(env):
    folder_eng = env.as_(rbac.Grant(ENG, 'node', node_id=A1))
    assert folder_eng.delete(f'/api/media?cluster={C}&kind=iso&uuid={ISO_A}').status_code == 403
    msp = env.as_(rbac.Grant(ENG, 'node', node_id=M))
    assert msp.delete(f'/api/media?cluster={C}&kind=iso&uuid={ISO_A}').status_code == 404, 'not even seen'
    r = msp.post(f'/api/media/assign?cluster={C}', json={'kind': 'iso', 'uuid': ISO_M, 'node_id': A, 'assign': True})
    assert r.status_code == 200 and r.get_json()['assigned'] == [A]
    r = msp.post(f'/api/media/assign?cluster={C}', json={'kind': 'iso', 'uuid': ISO_M, 'node_id': B, 'assign': True})
    assert r.status_code == 403, 'B is not on this cluster\'s branch'


def test_the_vm_dialogs_offer_and_accept_only_visible_media(env, dash, monkeypatch):
    monkeypatch.setattr(dash, '_tag_guard', lambda *a, **k: None)      # its own test covers it; it reads the database
    c = env.as_(rbac.Grant(ENG, 'node', node_id=A))
    offered = c.get(f'/api/backup/vmedit/options?cluster={C}').get_json()['isos']
    assert sorted(i['name'] for i in offered) == ['a.iso']
    r = c.post(f'/api/backup/vmedit/new?cluster={C}', json={'cluster_id': C, 'spec': {'name': 'v', 'iso_uuid': ISO_TOP}})
    assert r.status_code == 403
    r = c.post(f'/api/backup/vmedit/new?cluster={C}', json={'cluster_id': C, 'spec': {'name': 'v', 'iso_uuid': ISO_A}})
    assert r.status_code == 200, r.get_json()


def test_an_unreachable_backup_service_is_a_503_not_a_500(env, monkeypatch):
    def refuse(rq, timeout=None):
        raise OSError('Connection refused')
    monkeypatch.setattr('urllib.request.urlopen', refuse)
    r = env.as_(rbac.Grant(ADMIN, 'root')).get(f'/api/media?cluster={C}')
    assert r.status_code == 503 and 'unreachable' in r.get_json()['error']


def test_a_browser_that_breaks_off_breaks_the_stream_to_the_backup_service():
    """2026-11-22: a closed browser connection reads as an ordinary end of the body. Passed on like that, the
    backup service waited five minutes for the rest before it voided the half ISO; now the stream breaks at once."""
    import io
    import media_api
    whole = media_api.WholeBody(io.BytesIO(b'x' * 10), 10)
    assert whole.read(4) == b'xxxx' and whole.read() == b'x' * 6 and whole.read() == b'', 'a whole body reads as before'
    cut = media_api.WholeBody(io.BytesIO(b'x' * 4), 10)
    assert cut.read(4) == b'xxxx'
    with pytest.raises(ConnectionError):
        cut.read(4)
    assert media_api.WholeBody(io.BytesIO(b'x' * 20), 10).read() == b'x' * 10, 'never reads past Content-Length'
