"""exports/jobs.py and exports/hypercore.py — pure helpers ported from hypercore-export-manager.

exports/ reuses module names that sentinel/ also has (config, db, log), so its modules are
imported here in isolation: exports/ goes first on sys.path only for the duration of the
import, the clashing names are purged from sys.modules before and after, and the imported
objects are kept as module globals."""
import importlib
import os
import sys

import pytest

_EXPORTS = os.path.join(os.path.dirname(__file__), '..', 'exports')
_CLASH = ('config', 'db', 'log', 'api', 'hypercore', 'jobs', 'scheduler')


def _import_exports_modules():
    saved = {n: sys.modules.pop(n) for n in _CLASH if n in sys.modules}
    sys.path.insert(0, _EXPORTS)
    try:
        hc = importlib.import_module('hypercore')
        jb = importlib.import_module('jobs')
    finally:
        sys.path.remove(_EXPORTS)
        for n in _CLASH:
            sys.modules.pop(n, None)
        sys.modules.update(saved)
    return jb, hc


jobs, _hypercore = _import_exports_modules()
first_tag, _vm_sort_key = _hypercore.first_tag, _hypercore._vm_sort_key


@pytest.mark.parametrize('name, expected', [
    ('DC02', 'DC02'), ('web server #1', 'web_server_1'), ('  spaced  ', 'spaced'),
    ('ünïcode', 'n_code'), ('', 'vm'), ('///', 'vm'), ('a.b-c_d', 'a.b-c_d'),
])
def test_safe_name(name, expected):
    assert jobs.safe_name(name) == expected


def test_build_path_uri_injects_and_encodes_credentials():
    assert jobs.build_path_uri('smb://nas.local/share/path', 'backup', 'p@ss:w/rd') == 'smb://backup:p%40ss%3Aw%2Frd@nas.local/share/path'
    assert jobs.build_path_uri('smb://nas.local:4450/share', 'DOMAIN\\user', 'x') == 'smb://DOMAIN%5Cuser:x@nas.local:4450/share'


def test_build_path_uri_normalises_upn_like_the_smb_client():
    assert jobs.build_path_uri('smb://nas/share', 'test123@demolabs.me', 'pw') == 'smb://demolabs.me%5Ctest123:pw@nas/share'
    assert jobs.build_path_uri('smb://nas/share', 'DOMAIN/user', 'pw') == 'smb://DOMAIN%5Cuser:pw@nas/share'


def test_build_path_uri_without_user_is_passthrough():
    assert jobs.build_path_uri('nfs://nas.local/export/vm', None, None) == 'nfs://nas.local/export/vm'
    assert jobs.build_path_uri('smb://u:p@nas/share', '', 'ignored') == 'smb://u:p@nas/share'


def test_build_path_uri_replaces_existing_userinfo():
    assert jobs.build_path_uri('smb://old:creds@nas.local/share', 'new', 'pw') == 'smb://new:pw@nas.local/share'


@pytest.mark.parametrize('user, expected', [
    ('DOMAIN/user', 'DOMAIN\\user'), ('DOMAIN\\user', 'DOMAIN\\user'), ('test123@demolabs.me', 'demolabs.me\\test123'),
    ('plain', 'plain'), ('  padded  ', 'padded'), (None, ''),
])
def test_smb_username_normalisation(user, expected):
    assert jobs.smb_username(user) == expected


def test_select_excess_keeps_newest_and_ignores_siblings():
    names = ['DC02_20260101-030000', 'DC02_20260102-030000', 'DC02_20260103-030000', 'DC02_20260104-030000',
             'DC03_20260101-030000', 'DC02_manual', 'random']
    excess, kept = jobs.select_excess(names, 'DC02', 2)
    assert excess == ['DC02_20260101-030000', 'DC02_20260102-030000']
    assert kept == 2


def test_select_excess_retention_larger_than_copies():
    excess, kept = jobs.select_excess(['x_20260101-000000'], 'x', 7)
    assert excess == [] and kept == 1


def test_select_excess_zero_retention_deletes_nothing():
    assert jobs.select_excess(['x_20260101-000000'], 'x', 0) == ([], 0)


def test_select_excess_uses_safe_name_prefix():
    names = ['web_server_1_20260101-000000', 'web_server_1_20260102-000000']
    excess, kept = jobs.select_excess(names, 'web server #1', 1)
    assert excess == ['web_server_1_20260101-000000'] and kept == 1


@pytest.mark.parametrize('folder, expected', [
    ('DC02_20260101-030000', 'DC02'), ('DC02', 'DC02'), ('', ''), ('a_b_20261231-235959', 'a_b'),
])
def test_derive_name(folder, expected):
    assert jobs.derive_name(folder) == expected


def test_first_tag_and_vm_sort_key():
    assert first_tag('prod, web') == 'prod' and first_tag('') == '' and first_tag(None) == ''
    vms = [{'name': 'zeta', 'tags': ''}, {'name': 'beta', 'tags': 'prod'}, {'name': 'alpha', 'tags': 'dev'}, {'name': 'gamma', 'tags': 'prod'}]
    assert [v['name'] for v in sorted(vms, key=_vm_sort_key)] == ['alpha', 'beta', 'gamma', 'zeta'], 'grouped by first tag, untagged last'


def test_smb_unc_parsing():
    assert jobs._smb_unc('smb://nas.local/backups/hypercore') == ('\\\\nas.local\\backups\\hypercore', 445)
    assert jobs._smb_unc('smb://nas.local:4450/share') == ('\\\\nas.local\\share', 4450)
    with pytest.raises(Exception):
        jobs._smb_unc('smb://nas.local/')


# --- tenant scoping: the share mirrors HyperCore Manager folders -------------------------

class _C:
    def __init__(self, folder_name): self.folder_name = folder_name


def test_tenant_slug_from_folder_name():
    assert jobs.tenant_slug(_C('Amsterdam')) == 'Amsterdam'
    assert jobs.tenant_slug(_C('MSP customers / West')) == 'MSP_customers_West'
    assert jobs.tenant_slug(_C(None)) == 'no_folder'
    assert jobs.tenant_slug(None) == 'no_folder'
    assert jobs.tenant_slug(_C('MSP customers')) == 'MSP_customers', 'spaces become underscores on the share'


def test_effective_base_appends_tenant(monkeypatch):
    monkeypatch.setattr(jobs.db, 'share_for_cluster', lambda cid: None)   # legacy path: no folder share
    monkeypatch.setattr(jobs.cluster_registry, 'get', lambda cid: _C('Amsterdam'))
    assert jobs.effective_base({'cluster_id': 'x', 'path_uri_base': 'smb://nas/share/'}) == 'smb://nas/share/Amsterdam'
    monkeypatch.setattr(jobs.cluster_registry, 'get', lambda cid: None)
    assert jobs.effective_base({'cluster_id': 'x', 'path_uri_base': 'nfs://nas/export'}) == 'nfs://nas/export/no_folder'


# --- location resolution: the folder's share wins, legacy schedule fields are a fallback ------

def test_location_from_folder_share(monkeypatch):
    monkeypatch.setattr(jobs.db, 'share_for_cluster', lambda cid: {'name': 'NAS', 'uri_base': 'smb://nas/backups/', 'username': 'DOM\\u', 'password': 'pw', 'prune_path': None})
    monkeypatch.setattr(jobs.cluster_registry, 'get', lambda cid: _C('Amsterdam'))
    loc = jobs.location_for_schedule({'cluster_id': 'x', 'path_uri_base': 'smb://ignored/legacy', 'smb_user': 'old'})
    assert loc['base'] == 'smb://nas/backups/Amsterdam' and loc['user'] == 'DOM\\u' and loc['password'] == 'pw'
    assert loc['share_name'] == 'NAS' and loc['tenant'] == 'Amsterdam' and loc['prune_path'] is None


def test_location_nfs_share_prune_path_gets_tenant(monkeypatch):
    monkeypatch.setattr(jobs.db, 'share_for_cluster', lambda cid: {'name': 'NFS', 'uri_base': 'nfs://nas/export', 'username': None, 'password': '', 'prune_path': '/mnt/nas/'})
    monkeypatch.setattr(jobs.cluster_registry, 'get', lambda cid: _C('Lab'))
    loc = jobs.location_for_cluster('x')
    assert loc['base'] == 'nfs://nas/export/Lab' and loc['prune_path'] == '/mnt/nas/Lab'


def test_location_legacy_schedule_without_folder_share(monkeypatch):
    monkeypatch.setattr(jobs.db, 'share_for_cluster', lambda cid: None)
    monkeypatch.setattr(jobs.db, 'decrypt_password', lambda tok: 'secret')
    monkeypatch.setattr(jobs.cluster_registry, 'get', lambda cid: _C(None))
    loc = jobs.location_for_schedule({'cluster_id': 'x', 'path_uri_base': 'smb://nas/share/', 'smb_user': 'u', 'smb_pass_enc': 'tok', 'prune_path': None})
    assert loc['base'] == 'smb://nas/share/no_folder' and loc['password'] == 'secret' and loc['share_name'] is None


def test_location_missing_everywhere_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(jobs.db, 'share_for_cluster', lambda cid: None)
    monkeypatch.setattr(jobs.cluster_registry, 'get', lambda cid: _C('Amsterdam'))
    with pytest.raises(jobs.HyperCoreError):
        jobs.location_for_schedule({'cluster_id': 'x', 'path_uri_base': None})
    assert jobs.effective_base({'cluster_id': 'x', 'path_uri_base': None}) == ''
