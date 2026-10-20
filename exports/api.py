"""
api.py — REST API of the Exports service (port 8082), consumed by the dashboard proxy.

Bearer token (EXPORTS_API_TOKEN); empty token = no auth (dev). Every list call takes
?cluster=<uuid>; mutations name the cluster in the body or via the schedule.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from functools import wraps

from flask import Flask, jsonify, request

import cluster_registry
import config as cfg_module
import db
import jobs
import scheduler
from hypercore import HyperCoreError

log = logging.getLogger('exports.api')
app = Flask('exports.api')


def _require_token(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = app.config.get('API_TOKEN', '')
        if token:
            auth = request.headers.get('Authorization', '')
            if not auth.startswith('Bearer ') or auth[7:] != token:
                return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated


def _ser(row):
    if row is None:
        return None
    out = {}
    for k, v in dict(row).items():
        if k == 'smb_pass_enc':
            out['has_smb_password'] = bool(v)
            continue
        out[k] = v.isoformat() if isinstance(v, datetime) else v
    return out


def _cluster_arg():
    cid = (request.args.get('cluster') or '').strip()
    return cid if cluster_registry.is_uuid(cid) else None


def _err(msg, code=400):
    return jsonify({'error': msg}), code


@app.get('/health')
def health():
    return jsonify({'status': 'ok', 'service': 'exports'}), 200


@app.get('/api/v1/status')
@_require_token
def status():
    cfg = cfg_module.load()
    return jsonify({
        'service': 'exports',
        'default_retention': cfg.default_retention,
        'default_run_time': cfg.default_run_time,
        'task_timeout_hours': cfg.task_timeout_hours,
        'schedules': len(db.get_schedules(_cluster_arg())),
    })


# ---------------------------------------------------------------------- VMs
@app.get('/api/v1/vms')
@_require_token
def vms():
    cid = _cluster_arg()
    if not cid:
        return _err('cluster is required')
    try:
        vm_list = jobs.client_for(cid).list_vms()
    except HyperCoreError as e:
        return _err(str(e), 502)
    scheduled = {s['vm_uuid']: _ser(s) for s in db.get_schedules(cid)}
    for vm in vm_list:
        vm['schedule'] = scheduled.get(vm['uuid'])
        vm['safe_name'] = jobs.safe_name(vm['name'] or '')
    return jsonify(vm_list)


# ---------------------------------------------------------------- schedules
@app.get('/api/v1/schedules')
@_require_token
def schedules():
    rows = []
    for s in db.get_schedules(_cluster_arg()):
        d = _ser(s)
        d['effective_base'] = jobs.effective_base(s)
        d['last_run'] = _ser(db.last_run_for(s['id']))
        nxt = scheduler.next_run_time(s['id'])
        d['next_run'] = nxt.isoformat() if nxt else None
        rows.append(d)
    return jsonify(rows)


def _valid_time(v: str) -> str | None:
    try:
        hh, mm = (v or '').split(':')
        hh, mm = int(hh), int(mm)
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return f'{hh:02d}:{mm:02d}'
    except ValueError:
        pass
    return None


@app.post('/api/v1/schedules')
@_require_token
def schedules_add():
    b = request.get_json(silent=True) or {}
    cid = (b.get('cluster_id') or '').strip()
    if not cluster_registry.is_uuid(cid) or not cluster_registry.get(cid):
        return _err('unknown cluster')
    run_time = _valid_time(b.get('run_time') or cfg_module.load().default_run_time)
    if not run_time:
        return _err('run_time must be HH:MM (24h)')
    for k in ('vm_uuid', 'vm_name', 'path_uri_base'):
        if not (b.get(k) or '').strip():
            return _err(f'{k} is required')
    try:
        retention = max(1, int(b.get('retention') or cfg_module.load().default_retention))
    except (TypeError, ValueError):
        return _err('retention must be a number')
    try:
        sid = db.add_schedule(cluster_id=cid, vm_uuid=b['vm_uuid'], vm_name=b['vm_name'],
                              path_uri_base=b['path_uri_base'].strip(), smb_user=b.get('smb_user'),
                              smb_password=b.get('smb_password'), prune_path=b.get('prune_path'),
                              retention=retention, run_time=run_time, compress=bool(b.get('compress')))
    except Exception as e:
        if 'duplicate key' in str(e):
            return _err('this VM already has a schedule on this cluster', 409)
        return _err(str(e), 500)
    scheduler.sync_jobs()
    return jsonify({'ok': True, 'id': sid}), 201


@app.put('/api/v1/schedules/<int:sid>')
@_require_token
def schedules_update(sid):
    if not db.get_schedule(sid):
        return _err('schedule not found', 404)
    b = request.get_json(silent=True) or {}
    fields = {}
    if 'run_time' in b:
        rt = _valid_time(b['run_time'])
        if not rt:
            return _err('run_time must be HH:MM (24h)')
        fields['run_time'] = rt
    if 'retention' in b:
        try:
            fields['retention'] = max(1, int(b['retention']))
        except (TypeError, ValueError):
            return _err('retention must be a number')
    for k in ('path_uri_base', 'smb_user', 'prune_path', 'smb_password'):
        if k in b:
            fields[k] = b[k]
    for k in ('compress', 'enabled'):
        if k in b:
            fields[k] = bool(b[k])
    db.update_schedule(sid, **fields)
    scheduler.sync_jobs()
    return jsonify({'ok': True})


@app.post('/api/v1/schedules/<int:sid>/toggle')
@_require_token
def schedules_toggle(sid):
    if not db.get_schedule(sid):
        return _err('schedule not found', 404)
    db.toggle_schedule(sid)
    scheduler.sync_jobs()
    return jsonify({'ok': True})


@app.delete('/api/v1/schedules/<int:sid>')
@_require_token
def schedules_delete(sid):
    db.delete_schedule(sid)
    scheduler.sync_jobs()
    return jsonify({'ok': True})


@app.post('/api/v1/schedules/<int:sid>/run')
@_require_token
def schedules_run(sid):
    if not db.get_schedule(sid):
        return _err('schedule not found', 404)
    scheduler.run_now(sid)
    return jsonify({'ok': True})


# --------------------------------------------------------------------- runs
@app.get('/api/v1/runs')
@_require_token
def runs():
    sid = request.args.get('schedule_id', type=int)
    limit = max(1, min(request.args.get('limit', 50, type=int), 500))
    return jsonify({
        'runs':    [_ser(r) for r in db.get_runs(_cluster_arg(), sid, limit)],
        'imports': [] if sid else [_ser(i) for i in db.get_imports(_cluster_arg(), limit)],
    })


@app.get('/api/v1/runs/status')
@_require_token
def runs_status():
    """Light snapshot the dashboard polls while anything is running."""
    sid = request.args.get('schedule_id', type=int)
    runs_ = [{'id': r['id'], 'status': r['status'], 'message': r['message'] or ''} for r in db.get_runs(_cluster_arg(), sid, 50)]
    imps = [] if sid else [{'id': i['id'], 'status': i['status'], 'message': i['message'] or ''} for i in db.get_imports(_cluster_arg(), 50)]
    return jsonify({'runs': runs_, 'imports': imps, 'active': any(x['status'] == 'RUNNING' for x in (*runs_, *imps))})


# ------------------------------------------------------------------ imports
def _resolve_source(b: dict, target_cluster):
    """(base_uri, user, password). A saved location (known_id -> schedule) keeps its credentials
    server-side and is scoped to the target cluster's tenant folder, so an import can only pick
    up exports that belong to the same HyperCore Manager folder. A manually typed source is used
    as typed (admin-only route)."""
    known_id = b.get('known_id')
    if known_id:
        try:
            sched = db.get_schedule(int(known_id))
        except (TypeError, ValueError):
            sched = None
        if sched and (sched['path_uri_base'] or '').lower().startswith('smb://'):
            password = db.decrypt_password(sched['smb_pass_enc']) if sched['smb_pass_enc'] else ''
            base = f"{sched['path_uri_base'].rstrip('/')}/{jobs.tenant_slug(target_cluster)}"
            return base, sched['smb_user'] or '', password
    return (b.get('source_uri') or '').strip(), (b.get('smb_user') or '').strip(), b.get('smb_password') or ''


@app.get('/api/v1/import/known')
@_require_token
def import_known():
    cid = _cluster_arg()
    cluster = cluster_registry.get(cid) if cid else None
    tenant = jobs.tenant_slug(cluster)
    rows = []
    for r in db.get_known_smb_locations():
        d = _ser(r)
        d['tenant'] = tenant
        d['effective_base'] = f"{r['path_uri_base'].rstrip('/')}/{tenant}"
        rows.append(d)
    return jsonify(rows)


@app.post('/api/v1/import/browse')
@_require_token
def import_browse():
    cid = _cluster_arg()
    base, user, password = _resolve_source(request.get_json(silent=True) or {}, cluster_registry.get(cid) if cid else None)
    if not base.lower().startswith('smb://'):
        return _err('Browsing is only available for smb:// shares; for NFS type the full source path.')
    try:
        return jsonify({'folders': jobs.smb_list_dirs(base, user, password)})
    except HyperCoreError as e:
        return _err(str(e), 502)


@app.post('/api/v1/import/start')
@_require_token
def import_start():
    b = request.get_json(silent=True) or {}
    cid = (b.get('cluster_id') or '').strip()
    cluster = cluster_registry.get(cid) if cluster_registry.is_uuid(cid) else None
    if not cluster:
        return _err('unknown target cluster')
    base, smb_user, smb_password = _resolve_source(b, cluster)
    base = base.rstrip('/')
    folder = (b.get('source_folder') or '').strip().strip('/')
    if not base:
        return _err('a source path is required')
    source_uri = f'{base}/{folder}' if folder else base
    target_name = (b.get('target_name') or '').strip()
    effective = target_name or jobs.derive_name(folder)
    if effective:
        try:
            existing = {(v['name'] or '').lower() for v in jobs.client_for(cid).list_vms()}
        except HyperCoreError as e:
            return _err(f"Could not verify VM names on '{cluster.name}' ({e}); import not started.", 502)
        if effective.lower() in existing:
            return _err(f"'{cluster.name}' already has a VM named '{effective}'. Enter a different target name.", 409)
    iid = db.add_import(cid, source_uri, smb_user, smb_password, target_name)
    scheduler.run_import(iid)
    return jsonify({'ok': True, 'id': iid}), 201


def start(cfg: cfg_module.Config) -> None:
    app.config['API_TOKEN'] = cfg.api_token
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=cfg.api_port, debug=False, use_reloader=False),
                     name='exports-api', daemon=True).start()
    log.info('Exports REST API started', extra={'port': cfg.api_port})
