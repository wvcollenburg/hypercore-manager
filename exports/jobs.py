"""
jobs.py — the export and import jobs: trigger on HyperCore, poll the task, prune
retention, recover after a restart. Ported from hypercore-export-manager onto the
cluster registry and PostgreSQL. Jobs never raise into the scheduler thread.
"""
from __future__ import annotations

import logging
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import cluster_registry
import config as cfg_module
import db
from hypercore import HyperCoreClient, HyperCoreError

log = logging.getLogger('exports.jobs')
CFG = cfg_module.load()

TIMESTAMP_FMT = '%Y%m%d-%H%M%S'


def _now() -> datetime:
    return datetime.now(timezone.utc)


def safe_name(name: str) -> str:
    """VM names can contain characters that are awkward in folder names."""
    return re.sub(r'[^A-Za-z0-9._-]+', '_', name or '').strip('_') or 'vm'


def build_path_uri(base: str, user: str | None, password: str | None) -> str:
    """Inject share credentials into a credential-free base URI.

    Users type a clean destination (smb://nas.local/share/path) plus username and
    password in separate fields; HyperCore's pathURI wants smb://user:pass@host/...
    with special characters percent-encoded. With no username the base is returned
    unchanged (NFS, anonymous shares, or a URI that already carries credentials)."""
    if not user:
        return base
    parts = urlsplit(base)
    userinfo = quote(user, safe='')
    if password:
        userinfo += ':' + quote(password, safe='')
    host = parts.hostname or parts.netloc
    netloc = f'{userinfo}@{host}'
    if parts.port:
        netloc += f':{parts.port}'
    return urlunsplit(parts._replace(netloc=netloc))


def client_for(cluster_id: str) -> HyperCoreClient:
    c = cluster_registry.get(cluster_id)
    if c is None:
        raise HyperCoreError('cluster is no longer registered')
    username, password = cluster_registry.credentials(cluster_id)
    return HyperCoreClient(host=c.host, username=username, password=password, verify_tls=bool(c.verify_tls))


def derive_name(folder: str) -> str:
    """Strip a `_YYYYMMDD-HHMMSS` export suffix to guess the original VM name."""
    m = re.match(r'^(.*)_\d{8}-\d{6}$', folder or '')
    return m.group(1) if m else (folder or '')


# ------------------------------------------------------------------ task poll
def wait_for_task(hc: HyperCoreClient, task_tag: str, on_progress) -> None:
    """Poll TaskTag until COMPLETE; raise on ERROR or timeout. on_progress(state, pct)
    is called whenever the percentage changes."""
    deadline = time.monotonic() + CFG.task_timeout_hours * 3600
    last_pct = -1
    while time.monotonic() < deadline:
        status = hc.task_status(task_tag)
        state = status.get('state', 'UNINITIALIZED')
        pct = status.get('progressPercent', 0)
        if pct != last_pct:
            on_progress(state, pct)
            last_pct = pct
        if state == 'COMPLETE':
            return
        if state == 'ERROR':
            detail = status.get('formattedMessage') or status.get('formattedDescription') or 'task reported ERROR'
            raise HyperCoreError(f'Task failed: {detail}')
        time.sleep(CFG.poll_interval_seconds)
    raise HyperCoreError(f'Task {task_tag} did not finish within {CFG.task_timeout_hours}h')


# --------------------------------------------------------------------- export
def run_export(schedule_id: int) -> None:
    """Entry point for the scheduler and the 'Run now' button."""
    sched = db.get_schedule(schedule_id)
    if sched is None:
        log.warning('Schedule %s no longer exists, skipping', schedule_id)
        return
    if not sched['enabled']:
        log.info('Schedule %s (%s) is disabled, skipping', schedule_id, sched['vm_name'])
        return
    if not sched.get('cluster_enabled', True):
        log.info('Schedule %s (%s): cluster is disabled, skipping', schedule_id, sched['vm_name'])
        return
    if db.is_run_active(schedule_id):
        log.warning('Schedule %s (%s) still has a run in progress, skipping', schedule_id, sched['vm_name'])
        return

    run_id = db.start_run(schedule_id)
    folder = f"{safe_name(sched['vm_name'])}_{datetime.now().strftime(TIMESTAMP_FMT)}"
    smb_pass = db.decrypt_password(sched['smb_pass_enc']) if sched['smb_pass_enc'] else None
    path_uri = build_path_uri(f"{sched['path_uri_base']}/{folder}", sched['smb_user'], smb_pass)
    db.update_run(run_id, export_path=folder)
    log.info('[run %s] Exporting %s to %s', run_id, sched['vm_name'], folder)

    try:
        hc = client_for(sched['cluster_id'])
        task_tag = hc.export_vm(sched['vm_uuid'], path_uri, compress=bool(sched['compress']))
        db.update_run(run_id, task_tag=task_tag)
        wait_for_task(hc, task_tag, lambda state, pct: db.update_run(run_id, message=f'{state} {pct}%'))
    except HyperCoreError as e:
        db.update_run(run_id, status='ERROR', message=str(e), finished_at=_now())
        log.error('[run %s] Export failed: %s', run_id, e)
        return
    except Exception as e:  # never let a job crash the scheduler thread
        db.update_run(run_id, status='ERROR', message=f'Unexpected error: {e}', finished_at=_now())
        log.exception('[run %s] Unexpected error', run_id)
        return

    prune_msg = prune_old_exports(sched)
    db.update_run(run_id, status='COMPLETE', finished_at=_now(), message=f'Export complete. {prune_msg}')
    log.info('[run %s] Done. %s', run_id, prune_msg)


def resume_export(run_id: int) -> None:
    """Re-attach to an export whose monitoring thread died on a restart. The HyperCore
    task keeps running on the cluster independently of this service."""
    run = db.get_run(run_id)
    if run is None or run['status'] != 'RUNNING':
        return
    sched = db.get_schedule(run['schedule_id'])
    if sched is None:
        db.update_run(run_id, status='ERROR', finished_at=_now(), message='Interrupted by a restart; schedule no longer exists.')
        return
    try:
        hc = client_for(sched['cluster_id'])
        state = hc.task_status(run['task_tag']).get('state', 'UNINITIALIZED')
        if state == 'ERROR':
            db.update_run(run_id, status='ERROR', finished_at=_now(), message='Export task reported ERROR (detected after restart).')
            return
        if state == 'UNINITIALIZED':
            prune_msg = prune_old_exports(sched)
            db.update_run(run_id, status='COMPLETE', finished_at=_now(),
                          message='Resumed after restart; the cluster no longer tracks the task: assumed complete, '
                                  f'verify the export on the share. {prune_msg}')
            return
        if state in ('QUEUED', 'RUNNING'):
            wait_for_task(hc, run['task_tag'], lambda s, p: db.update_run(run_id, message=f'{s} {p}%'))
    except HyperCoreError as e:
        db.update_run(run_id, status='ERROR', message=str(e), finished_at=_now())
        return
    except Exception as e:
        db.update_run(run_id, status='ERROR', message=f'Unexpected error: {e}', finished_at=_now())
        log.exception('[run %s] Resume error', run_id)
        return
    prune_msg = prune_old_exports(sched)
    db.update_run(run_id, status='COMPLETE', finished_at=_now(), message=f'Export complete (resumed after restart). {prune_msg}')


# ------------------------------------------------------------------------ SMB
def smb_username(user: str | None) -> str:
    """Normalise a share username for the SMB client.

    Users save DOMAIN/user (HyperCore's form) or user@domain (UPN). smbprotocol wants
    DOMAIN\\user; some servers refuse the UPN form outright (seen on a Windows DC share),
    so both are rewritten to domain\\user. A name that already has a backslash is kept."""
    user = (user or '').strip()
    if '\\' in user:
        return user
    if '@' in user:
        name, _, domain = user.partition('@')
        return f'{domain}\\{name}' if domain else name
    if '/' in user:
        user = user.replace('/', '\\', 1)
    return user


def _smb_unc(base_uri: str) -> tuple[str, int]:
    parts = urlsplit(base_uri)
    server = parts.hostname
    port = parts.port or 445
    segments = [s for s in parts.path.split('/') if s]
    if not server or not segments:
        raise HyperCoreError(f"'{base_uri}' is not a valid SMB share path.")
    return '\\\\' + server + '\\' + '\\'.join(segments), port


def smb_list_dirs(base_uri: str, user: str, password: str | None) -> list[str]:
    """Subdirectory names of an SMB path, newest-name first (import browser)."""
    import smbclient
    unc, port = _smb_unc(base_uri)
    conn = {'username': smb_username(user), 'password': password, 'port': port}
    try:
        names = [e.name for e in smbclient.scandir(unc, **conn) if e.is_dir()]
    except Exception as e:  # noqa: BLE001 — any SMB error becomes a UI message
        raise HyperCoreError(f'Could not list {unc}: {e}') from e
    return sorted(names, reverse=True)


def select_excess(dir_names, vm_name: str, retention: int):
    """Which folders to delete: only this VM's `{safe_name}_YYYYMMDD-HHMMSS` folders are
    candidates, sibling VMs are never touched; the newest `retention` are kept."""
    prefix = safe_name(vm_name) + '_'
    pattern = re.compile(re.escape(prefix) + r'\d{8}-\d{6}$')
    copies = sorted(n for n in dir_names if pattern.match(n))
    excess = copies[:-retention] if retention > 0 else []
    kept = min(len(copies), retention)
    return excess, kept


def prune_old_exports(sched) -> str:
    """Delete the oldest export folders beyond the retention count. SMB destinations are
    pruned over the protocol with the stored credentials; NFS (or an explicit prune_path)
    through a local bind mount."""
    if sched.get('prune_path'):
        return _prune_via_mount(sched)
    scheme = urlsplit(sched['path_uri_base']).scheme.lower()
    if scheme == 'smb':
        if not sched.get('smb_user'):
            return 'Pruning skipped: SMB share has no stored username; add one or set a mount path.'
        return _prune_via_smb(sched)
    return 'Pruning skipped (no mount path configured).'


def _prune_via_mount(sched) -> str:
    base = Path(sched['prune_path'])
    if not base.is_dir():
        return f'Pruning skipped: {base} is not accessible from this container.'
    names = [d.name for d in base.iterdir() if d.is_dir()]
    excess, kept = select_excess(names, sched['vm_name'], sched['retention'])
    for name in excess:
        try:
            shutil.rmtree(base / name)
            log.info('Pruned old export %s', base / name)
        except OSError as e:
            return f'Pruning error on {name}: {e}'
    return f'Retention: kept {kept}, removed {len(excess)}.'


def _prune_via_smb(sched) -> str:
    import smbclient
    from smbclient import shutil as smb_shutil
    try:
        unc_base, port = _smb_unc(sched['path_uri_base'])
    except HyperCoreError as e:
        return f'Pruning skipped: {e}'
    password = db.decrypt_password(sched['smb_pass_enc']) if sched.get('smb_pass_enc') else None
    conn = {'username': smb_username(sched['smb_user']), 'password': password, 'port': port}
    try:
        names = [e.name for e in smbclient.scandir(unc_base, **conn) if e.is_dir()]
    except Exception as e:  # noqa: BLE001
        return f'Pruning skipped: could not list {unc_base} over SMB: {e}'
    excess, kept = select_excess(names, sched['vm_name'], sched['retention'])
    for name in excess:
        try:
            smb_shutil.rmtree(unc_base + '\\' + name, **conn)
            log.info('Pruned old export (SMB) %s\\%s', unc_base, name)
        except Exception as e:  # noqa: BLE001
            return f'Pruning error over SMB on {name}: {e}'
    return f'Retention (SMB): kept {kept}, removed {len(excess)}.'


# --------------------------------------------------------------------- import
def run_import(import_id: int) -> None:
    imp = db.get_import(import_id)
    if imp is None:
        log.warning('Import %s no longer exists, skipping', import_id)
        return
    log.info('[import %s] Importing from %s into %s', import_id, imp['source_uri'], imp['cluster_name'])
    try:
        hc = client_for(imp['cluster_id'])
        password = db.decrypt_password(imp['smb_pass_enc']) if imp['smb_pass_enc'] else None
        source_uri = build_path_uri(imp['source_uri'], imp['smb_user'], password)
        task_tag, created = hc.import_vm(source_uri, name=imp['target_name'] or None)
        db.update_import(import_id, task_tag=task_tag, created_uuid=created or None)
        wait_for_task(hc, task_tag, lambda state, pct: db.update_import(import_id, message=f'{state} {pct}%'))
    except HyperCoreError as e:
        db.update_import(import_id, status='ERROR', message=str(e), finished_at=_now())
        log.error('[import %s] Failed: %s', import_id, e)
        return
    except Exception as e:
        db.update_import(import_id, status='ERROR', message=f'Unexpected error: {e}', finished_at=_now())
        log.exception('[import %s] Unexpected error', import_id)
        return
    db.update_import(import_id, status='COMPLETE', finished_at=_now(), message='Import complete.')
    log.info('[import %s] Done', import_id)


def resume_import(import_id: int) -> None:
    imp = db.get_import(import_id)
    if imp is None or imp['status'] != 'RUNNING':
        return
    try:
        hc = client_for(imp['cluster_id'])
        state = hc.task_status(imp['task_tag']).get('state', 'UNINITIALIZED')
        if state == 'ERROR':
            db.update_import(import_id, status='ERROR', finished_at=_now(), message='Import task reported ERROR (detected after restart).')
            return
        if state == 'UNINITIALIZED':
            created = imp['created_uuid']
            exists = bool(created) and any(v['uuid'] == created for v in hc.list_vms())
            if exists:
                db.update_import(import_id, status='COMPLETE', finished_at=_now(), message='Import complete (confirmed by VM presence after restart).')
            else:
                db.update_import(import_id, status='ERROR', finished_at=_now(),
                                 message='Could not confirm import after restart: the task is no longer tracked and the new VM was not found.')
            return
        if state in ('QUEUED', 'RUNNING'):
            wait_for_task(hc, imp['task_tag'], lambda s, p: db.update_import(import_id, message=f'{s} {p}%'))
    except HyperCoreError as e:
        db.update_import(import_id, status='ERROR', message=str(e), finished_at=_now())
        return
    except Exception as e:
        db.update_import(import_id, status='ERROR', message=f'Unexpected error: {e}', finished_at=_now())
        log.exception('[import %s] Resume error', import_id)
        return
    db.update_import(import_id, status='COMPLETE', finished_at=_now(), message='Import complete (resumed after restart).')
