"""backup/api.py — request validation of the backup service's REST API (the dashboard checks rights, this
service checks that what it is asked for exists and stays inside the caller's folder)."""
import importlib
import os
import sys
import types

import pytest

_BACKUP = os.path.join(os.path.dirname(__file__), '..', 'backup')
_CLASH = ('config', 'db', 'log', 'api', 'hypercore', 'jobs', 'scheduler', 'incremental', 'proxy_client', 'manifest_names',
          'policies', 'recurrence')


def _import():
    saved = {n: sys.modules.pop(n) for n in _CLASH if n in sys.modules}
    sys.path.insert(0, _BACKUP)
    try:
        api = importlib.import_module('api')
    finally:
        sys.path.remove(_BACKUP)
        for n in _CLASH:
            sys.modules.pop(n, None)
        sys.modules.update(saved)
    return api


api = _import()
jobs = api.jobs
CID = '3090c567-4393-454d-ae79-cf98fe087949'
OTHER = '6a1d1d5e-0d7e-4c55-9a43-1f3e1c3b9b10'
FOLDER = 'aaaaaaaa-0000-4000-8000-000000000001'


class _HC:
    def __init__(self, vms): self.vms = vms
    def get_vm(self, u):
        if u not in self.vms:
            raise api.HyperCoreError(f'VM {u} not found on the cluster')
        return self.vms[u]
    def list_vms(self): return list(self.vms.values())


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setitem(api.app.config, 'API_TOKEN', '')
    cluster = types.SimpleNamespace(cluster_id=CID, folder_id=FOLDER, folder_name='Prod', name='prod')
    monkeypatch.setattr(api.cluster_registry, 'get', lambda cid: cluster if cid == CID else None)
    monkeypatch.setattr(api.jobs, 'client_for', lambda cid: _HC({'vm-1': {'uuid': 'vm-1', 'name': 'dc01'}}))
    monkeypatch.setattr(api.jobs, 'location_for_cluster',
                        lambda cid: {'base': 'smb://nas/backups/Prod', 'user': 'svc', 'password': 'pw', 'share_name': 'NAS',
                                     'tenant': 'Prod', 'prune_path': None})
    monkeypatch.setattr(api.scheduler, 'sync_jobs', lambda: 0)
    monkeypatch.setattr(api.scheduler, 'run_import', lambda iid: None)
    added = []
    fake = types.SimpleNamespace(
        get_proxies=lambda cid, enabled_only=False: [{'vm_uuid': 'px'}],
        add_schedule=lambda **k: added.append(k) or 7,
        folder_of_cluster=lambda cid: (True, FOLDER),
        folder_clusters=lambda f: [{'cluster_id': CID}],
        get_policy=lambda pid: {'id': pid, 'folder_id': FOLDER, 'kind': 'incremental', 'retention': 7, 'compress': False,
                                'full_every_n': 14, 'retention_days': None},
        plan_for_vm=lambda cid, u: None, assign_plan=lambda sid, pid: None,
        get_schedule=lambda sid: {'id': sid, 'cluster_id': OTHER, 'path_uri_base': 'smb://othernas/share', 'smb_user': 'them',
                                  'smb_pass_enc': 'x'},
        decrypt_password=lambda t: 'their-secret',
        add_import=lambda *a: added.append(('import', a)) or 3,
    )
    monkeypatch.setattr(api, 'db', fake)
    c = api.app.test_client()
    c.added = added
    return c


def test_a_plan_for_a_vm_that_does_not_exist_is_refused(client):
    r = client.post('/api/v1/plans', json={'cluster_id': CID, 'vm_uuid': 'nope', 'vm_name': 'x', 'mode': 'incremental'})
    assert r.status_code == 404 and client.added == []


def test_a_plan_takes_the_vm_name_from_hypercore_not_from_the_request(client):
    r = client.post('/api/v1/plans', json={'cluster_id': CID, 'vm_uuid': 'vm-1', 'vm_name': '../other', 'mode': 'incremental'})
    assert r.status_code == 201
    assert client.added[0]['vm_name'] == 'dc01' and client.added[0]['vm_uuid'] == 'vm-1'


def test_a_schedule_member_that_does_not_exist_is_refused(client):
    r = client.post(f'/api/v1/policies/1/members?cluster={CID}', json={'cluster_id': CID, 'vm_uuid': 'nope', 'vm_name': 'x'})
    assert r.status_code == 404 and client.added == []
    r = client.post(f'/api/v1/policies/1/members?cluster={CID}', json={'cluster_id': CID, 'vm_uuid': 'vm-1', 'vm_name': 'spoofed'})
    assert r.status_code == 201 and client.added[0]['vm_name'] == 'dc01'


def test_an_import_cannot_use_a_saved_location_of_another_folder(client):
    """known_id named any plan install-wide, and its stored SMB credentials were used."""
    for path in ('/api/v1/import/browse?cluster=' + CID, '/api/v1/import/start'):
        r = client.post(path, json={'cluster_id': CID, 'known_id': 99, 'source_folder': 'dc01_20260101-030000'})
        assert r.status_code == 400, path
    assert client.added == []


@pytest.mark.parametrize('folder', ['../Other', 'a/b', 'a\\b', '..'])
def test_an_import_folder_must_be_one_name(client, folder):
    r = client.post('/api/v1/import/start', json={'cluster_id': CID, 'source_folder': folder})
    assert r.status_code == 400 and client.added == []


def test_a_typed_source_outside_the_folder_target_needs_its_own_credentials(client):
    r = client.post('/api/v1/import/start', json={'cluster_id': CID, 'source_uri': 'smb://othernas/share/Other/dc01_x'})
    assert r.status_code == 400 and client.added == []
    r = client.post('/api/v1/import/start', json={'cluster_id': CID, 'source_folder': 'web01_20260101-030000'})
    assert r.status_code == 201, 'the folder target itself needs nothing extra'


def test_a_target_that_still_holds_chains_cannot_be_deleted(monkeypatch):
    """Deleting it set chains.target_id NULL: the catalogue lost where the data was, the data stayed."""
    monkeypatch.setitem(api.app.config, 'API_TOKEN', '')
    deleted = []
    monkeypatch.setattr(api, 'db', types.SimpleNamespace(chains_on_target=lambda sid: {3: 2}.get(sid, 0),
                                                         delete_share=deleted.append))
    c = api.app.test_client()
    r = c.delete('/api/v1/targets/3')
    assert r.status_code == 409 and '2 backup chain' in r.get_json()['error'] and deleted == []
    assert c.delete('/api/v1/targets/4').status_code == 200 and deleted == [4]


@pytest.mark.parametrize('busy, word', [('run', 'backup'), ('plan', 'backup'), ('restore', 'restore'), ('mount', 'file recovery')])
def test_a_chain_in_use_cannot_be_deleted(monkeypatch, busy, word):
    monkeypatch.setitem(api.app.config, 'API_TOKEN', '')
    calls = []
    monkeypatch.setattr(api, 'db', types.SimpleNamespace(
        get_chain=lambda cid: {'id': cid, 'status': 'active', 'plan_id': 4, 'cluster_id': CID, 'target_id': None},
        chain_work=lambda cid: {'runs': int(busy == 'run'), 'restores': int(busy == 'restore')},
        plan_run_active=lambda pid: busy == 'plan',
        get_points=lambda cid: [], update_chain=lambda *a, **k: calls.append('deleted')))
    monkeypatch.setattr(api.flr, 'list_mounts', lambda cid=None: [{'chain_id': 5}] if busy == 'mount' else [])
    r = api.app.test_client().delete('/api/v1/chains/5')
    assert r.status_code == 409 and word in r.get_json()['error'] and calls == []


def test_bad_input_is_a_400_not_a_500(client, monkeypatch):
    for path, body in (('/api/v1/plans', {'cluster_id': 5}), ('/api/v1/plans', {'cluster_id': ['x']}),
                       ('/api/v1/import/start', {'cluster_id': {'a': 1}}), ('/api/v1/proxies', {'cluster_id': 7}),
                       ('/api/v1/policies/preview', {'count': 'many'})):
        r = client.post(path, json=body)
        assert r.status_code == 400, (path, body, r.status_code)
    assert client.get(f'/api/v1/vmedit/changes?cluster={CID}&limit=abc').status_code == 400
    # a restore onto a cluster id that is no uuid, and removing a test VM without a valid cluster
    api.db.get_point = lambda pid: {'id': pid, 'chain_id': 1, 'status': 'ok'}
    api.db.get_chain = lambda cid: {'id': 1, 'status': 'active', 'cluster_id': CID, 'vm_name': 'dc01', 'target_id': None}
    r = client.post('/api/v1/restores', json={'point_id': 4, 'target_cluster_id': "x' OR 1=1"})
    assert r.status_code == 400
    api.db.get_test_plan = lambda pid: {'id': pid, 'folder_id': FOLDER}
    r = client.delete(f'/api/v1/test-plans/1/vms?cluster={CID}', json={'cluster_id': 'nope', 'vm_uuid': 'v'})
    assert r.status_code == 400


def test_a_missing_file_in_file_recovery_is_a_404(monkeypatch):
    monkeypatch.setitem(api.app.config, 'API_TOKEN', '')

    def stream(mid, kind, params):
        raise api.ProxyError('proxy returned 404')
    monkeypatch.setattr(api.flr, 'stream', stream)
    monkeypatch.setattr(api.flr, 'ls', lambda mid, path: (_ for _ in ()).throw(api.ProxyError('proxy returned 404: no such path')))
    c = api.app.test_client()
    r = c.get('/api/v1/flr/mounts/m1/file?path=/nope.txt')
    assert r.status_code == 404 and r.get_json()['error'] == 'file not found in the restore point'
    assert c.get('/api/v1/flr/mounts/m1/ls?path=/nope').status_code == 404


def test_error_texts_name_places_that_exist_in_the_ui():
    """"Settings → Shares" and "Settings → Clusters → Proxies" are gone since 1.4.0; the labels named must exist."""
    import glob
    import re
    root = os.path.join(os.path.dirname(__file__), '..')
    own = ('cluster_registry.py', 'hcm_crypto.py', 'hypercore_session.py')          # synced copies, not ours
    texts = ''.join(open(p, encoding='utf-8').read() for p in glob.glob(os.path.join(root, 'backup', '*.py'))
                    if os.path.basename(p) not in own)
    assert 'Settings → Shares' not in texts and 'Clusters → Proxies' not in texts
    en = open(os.path.join(root, 'dashboard', 'static', 'lang', 'en.js'), encoding='utf-8').read()
    for label in ('Targets', 'Backup target', 'Backup proxies'):
        assert re.search(r':\s*"' + re.escape(label) + '"', en), label
    assert 'Settings → Targets → Backup proxies' in texts and "Backup target in the folder's menu" in texts


def test_run_import_puts_the_folder_credentials_only_into_its_own_directory(monkeypatch):
    seen = []

    class HC:
        def import_vm(self, uri, name=None):
            seen.append(uri)
            return 'tag', 'new'
    monkeypatch.setattr(jobs, 'client_for', lambda cid: HC())
    monkeypatch.setattr(jobs, 'wait_for_task', lambda *a: None)
    monkeypatch.setattr(jobs, 'location_for_cluster', lambda cid: {'base': 'smb://nas/backups/Prod', 'user': 'svc', 'password': 'pw'})
    imports = {1: {'id': 1, 'cluster_id': CID, 'cluster_name': 'prod', 'source_uri': 'smb://nas/backups/Prod/dc01_x',
                   'smb_user': None, 'smb_pass_enc': None, 'target_name': ''},
               2: {'id': 2, 'cluster_id': CID, 'cluster_name': 'prod', 'source_uri': 'smb://evil/share/dc01_x',
                   'smb_user': None, 'smb_pass_enc': None, 'target_name': ''},
               3: {'id': 3, 'cluster_id': CID, 'cluster_name': 'prod', 'source_uri': 'smb://nas/backups/Prod/../Other/dc01_x',
                   'smb_user': None, 'smb_pass_enc': None, 'target_name': ''}}
    monkeypatch.setattr(jobs, 'db', types.SimpleNamespace(get_import=imports.get, update_import=lambda *a, **k: None))
    for i in (1, 2, 3):
        jobs.run_import(i)
    assert seen == ['smb://svc:pw@nas/backups/Prod/dc01_x', 'smb://evil/share/dc01_x', 'smb://nas/backups/Prod/../Other/dc01_x']
