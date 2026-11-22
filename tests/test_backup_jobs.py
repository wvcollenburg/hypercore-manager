"""backup/jobs.py and backup/hypercore.py — pure helpers ported from hypercore-export-manager.

backup/ reuses module names that sentinel/ also has (config, db, log), so its modules are
imported here in isolation: backup/ goes first on sys.path only for the duration of the
import, the clashing names are purged from sys.modules before and after, and the imported
objects are kept as module globals."""
import importlib
import os
import sys
import time

import pytest

_BACKUP = os.path.join(os.path.dirname(__file__), '..', 'backup')
_CLASH = ('config', 'db', 'log', 'api', 'hypercore', 'jobs', 'scheduler')


def _import_exports_modules():
    saved = {n: sys.modules.pop(n) for n in _CLASH if n in sys.modules}
    sys.path.insert(0, _BACKUP)
    try:
        hc = importlib.import_module('hypercore')
        jb = importlib.import_module('jobs')
    finally:
        sys.path.remove(_BACKUP)
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
    assert jobs.build_path_uri('smb://nas/share', 'alice@example.org', 'pw') == 'smb://example.org%5Calice:pw@nas/share'
    assert jobs.build_path_uri('smb://nas/share', 'DOMAIN/user', 'pw') == 'smb://DOMAIN%5Cuser:pw@nas/share'


def test_build_path_uri_without_user_is_passthrough():
    assert jobs.build_path_uri('nfs://nas.local/export/vm', None, None) == 'nfs://nas.local/export/vm'
    assert jobs.build_path_uri('smb://u:p@nas/share', '', 'ignored') == 'smb://u:p@nas/share'


def test_build_path_uri_replaces_existing_userinfo():
    assert jobs.build_path_uri('smb://old:creds@nas.local/share', 'new', 'pw') == 'smb://new:pw@nas.local/share'


@pytest.mark.parametrize('user, expected', [
    ('DOMAIN/user', 'DOMAIN\\user'), ('DOMAIN\\user', 'DOMAIN\\user'), ('alice@example.org', 'example.org\\alice'),
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


class _F:
    def __init__(self, folder_name, folder_id): self.folder_name, self.folder_id = folder_name, folder_id


def _fake_backup_dirs(monkeypatch, stored):
    """db.folder_backup_dir over a dict folder_id -> backup_dir (None = column still empty)."""
    def folder_backup_dir(fid, new_dir):
        if fid not in stored:
            return None
        if not stored[fid]:
            stored[fid] = new_dir
        return stored[fid]
    monkeypatch.setattr(jobs.db, 'folder_backup_dir', folder_backup_dir)


def test_tenant_dir_is_stored_on_the_folder_not_derived_from_its_name(monkeypatch):
    """Two customers' folders named "Production" must not share one directory on a target, and a rename must not
    move where a folder's exports are looked for."""
    a, b = '11111111-aaaa-4aaa-8aaa-000000000001', '22222222-bbbb-4bbb-8bbb-000000000002'
    stored = {a: None, b: None}
    _fake_backup_dirs(monkeypatch, stored)
    da, db_ = jobs.tenant_slug(_F('Production', a)), jobs.tenant_slug(_F('Production', b))
    assert da != db_, 'same name under two customers: two directories'
    assert da == 'Production-11111111' and db_ == 'Production-22222222'
    assert jobs.tenant_slug(_F('Renamed', a)) == 'Production-11111111', 'a rename keeps the directory'


def test_tenant_dir_of_a_folder_from_before_055_is_its_old_slug(monkeypatch):
    stored = {'f1': 'MSP_customers'}            # migration 055 filled it with the old slug
    _fake_backup_dirs(monkeypatch, stored)
    assert jobs.tenant_slug(_F('MSP customers', 'f1')) == 'MSP_customers'


def test_tenant_dir_falls_back_to_the_name_before_the_migration(monkeypatch):
    def broken(*_a):
        raise RuntimeError('column "backup_dir" does not exist')
    monkeypatch.setattr(jobs.db, 'folder_backup_dir', broken)
    assert jobs.tenant_slug(_F('Amsterdam', 'f1')) == 'Amsterdam'


def test_migration_055_fills_existing_folders_with_the_old_slug():
    """The SQL in 055 must compute exactly what safe_name did, or existing exports and chains are looked for in
    the wrong directory. The character class and trimming are replayed here in Python."""
    import re
    sql = open(os.path.join(os.path.dirname(__file__), '..', 'postgres', 'init', '055_folder_backup_dir.sql')).read()
    cls = re.search(r"'(\[\^[^']+\]\+)'", sql).group(1)
    assert "btrim(" in sql and "'vm'" in sql and 'WHERE backup_dir IS NULL' in sql

    def sql_slug(name):
        return re.sub(cls, '_', name).strip('_') or 'vm'
    for name in ('Amsterdam', 'MSP customers / West', 'ünïcode', '///', 'a.b-c_d', '  spaced  ', 'Prod (EU) #2'):
        assert sql_slug(name) == jobs.safe_name(name), name


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


def test_plan_runs_in_the_cluster_zone(monkeypatch):
    """03:00 on a US/Eastern cluster fires at 07:00 UTC in October (EDT), 08:00 UTC in winter."""
    import importlib, os, sys
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    from apscheduler.triggers.cron import CronTrigger
    sys.path.insert(0, _BACKUP)
    try:
        saved = sys.modules.pop('scheduler', None)
        sch = importlib.import_module('scheduler')
    finally:
        sys.path.remove(_BACKUP); sys.modules.pop('scheduler', None)
        if saved: sys.modules['scheduler'] = saved
    monkeypatch.setenv('TZ', 'Europe/Amsterdam')
    assert sch.plan_timezone({'cluster_timezone': 'US/Eastern'}) == 'US/Eastern'
    assert sch.plan_timezone({'cluster_timezone': None}) == 'Europe/Amsterdam'
    assert sch.plan_timezone({'cluster_timezone': 'Mars/Olympus'}) == 'Europe/Amsterdam', 'an unknown name falls back, never fails'
    trig = CronTrigger(hour=3, minute=0, timezone=ZoneInfo('US/Eastern'))
    nxt = trig.get_next_fire_time(None, datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc))
    assert nxt.astimezone(timezone.utc) == datetime(2026, 10, 23, 7, 0, tzinfo=timezone.utc)
    nxt = trig.get_next_fire_time(None, datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc))
    assert nxt.astimezone(timezone.utc) == datetime(2026, 12, 2, 8, 0, tzinfo=timezone.utc)


def test_run_timing_splits_active_time():
    from datetime import datetime, timedelta, timezone
    t0 = datetime(2026, 10, 30, 1, 0, tzinfo=timezone.utc)
    t = jobs.run_timing(t0, lock_wait=60, copy=300, end=t0 + timedelta(seconds=420))
    assert t == {'lock_wait_seconds': 60, 'copy_seconds': 300, 'overhead_seconds': 60}
    # a clock that disagrees never produces negative overhead
    assert jobs.run_timing(t0, copy=500, end=t0 + timedelta(seconds=400))['overhead_seconds'] == 0


def test_nfs_is_refused_for_hypercore_import_and_export(monkeypatch):
    """HyperCore imports and exports over SMB only: an export-mode run and an import on an NFS target stop with
    a clear message before HyperCore is asked."""
    assert jobs.is_nfs(' NFS://nas/export') and not jobs.is_nfs('smb://nas/share') and not jobs.is_nfs(None)
    runs, imports, asked = {}, {}, []
    monkeypatch.setattr(jobs.db, 'get_schedule', lambda sid: {'id': sid, 'enabled': True, 'cluster_enabled': True, 'vm_name': 'dc01',
                                                              'vm_uuid': 'v', 'cluster_id': 'x', 'compress': False})
    monkeypatch.setattr(jobs.db, 'is_run_active', lambda sid: False)
    monkeypatch.setattr(jobs.db, 'start_run', lambda *a: 5)
    monkeypatch.setattr(jobs.db, 'update_run', lambda rid, **f: runs.update(f))
    monkeypatch.setattr(jobs.db, 'share_for_cluster', lambda cid: {'name': 'NFS', 'uri_base': 'nfs://nas/export', 'username': None, 'password': '', 'prune_path': None})
    monkeypatch.setattr(jobs.cluster_registry, 'get', lambda cid: _C('Lab'))
    monkeypatch.setattr(jobs, 'client_for', lambda cid: asked.append(cid))
    jobs.run_export(1)
    assert runs['status'] == 'ERROR' and 'SMB only' in runs['message'] and not asked
    monkeypatch.setattr(jobs.db, 'get_import', lambda iid: {'id': iid, 'source_uri': 'nfs://nas/export/Lab/dc01_x', 'cluster_id': 'x', 'cluster_name': 'c'})
    monkeypatch.setattr(jobs.db, 'update_import', lambda iid, **f: imports.update(f))
    jobs.run_import(3)
    assert imports['status'] == 'ERROR' and 'SMB only' in imports['message'] and not asked


def test_preflight_for_the_dashboard_never_waits_for_the_cluster(monkeypatch):
    """/status answers from the last pre-flight; a stale or missing one is refreshed in the background, once per
    cluster at a time, so a slow login (a node down) cannot make the dashboard hide the Backup tab."""
    import threading
    jobs._PREFLIGHT_CACHE.clear(); jobs._PREFLIGHT_REFRESHING.clear()
    gate, calls = threading.Event(), []

    class SlowHC:
        def update_status(self): return {}
        def list_nodes(self):
            calls.append(1); gate.wait(5)
            return [{'lanIP': '10.0.0.1', 'networkStatus': 'ONLINE'}]
    assert jobs.preflight_cached('c1', client_factory=lambda cid: SlowHC()) is None, 'first call: nothing known yet, no wait'
    assert jobs.preflight_cached('c1', client_factory=lambda cid: SlowHC()) is None
    gate.set()
    for _ in range(50):
        if 'c1' in jobs._PREFLIGHT_CACHE and not jobs._PREFLIGHT_REFRESHING: break
        time.sleep(0.02)
    assert len(calls) == 1, 'one refresh at a time per cluster'
    assert jobs.preflight_cached('c1', client_factory=lambda cid: SlowHC())['ok'] is True
    # stale: the old answer comes back at once while a refresh runs; a failing cluster leaves a non-blocking answer
    jobs._PREFLIGHT_CACHE['c1'] = (time.monotonic() - 120, {'ok': False, 'kind': 'node', 'detail': 'old', 'nodes': []})

    def boom(cid): raise jobs.HyperCoreError('login timed out')
    assert jobs.preflight_cached('c1', client_factory=boom)['detail'] == 'old'
    for _ in range(50):
        if not jobs._PREFLIGHT_REFRESHING: break
        time.sleep(0.02)
    pf = jobs.preflight_cached('c1', client_factory=boom)
    assert pf['ok'] is True and 'timed out' in pf['unavailable']
