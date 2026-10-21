"""
api.py — REST API of the Backup service (port 8082), consumed by the dashboard proxy.

Bearer token (BACKUP_API_TOKEN); empty token = no auth (dev). Every list call takes
?cluster=<uuid>; mutations name the cluster in the body or via the schedule.
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime
from functools import wraps

from flask import Flask, jsonify, request

import cluster_registry
import config as cfg_module
import db
import jobs
import flr
import incremental
import scheduler
from hypercore import HyperCoreError
from proxy_client import ProxyError, certificate_fingerprint, normalise_fingerprint

log = logging.getLogger('backup.api')
app = Flask('backup.api')


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
    return jsonify({'status': 'ok', 'service': 'backup'}), 200


@app.get('/api/v1/status')
@_require_token
def status():
    cfg = cfg_module.load()
    cid = _cluster_arg()
    loc = jobs.location_for_cluster(cid) if cid else None
    return jsonify({
        'service': 'backup',
        'default_retention': cfg.default_retention,
        'default_run_time': cfg.default_run_time,
        'task_timeout_hours': cfg.task_timeout_hours,
        'plans': len(db.get_schedules(cid)),
        'proxies': len([p for p in db.get_proxies(cid, enabled_only=True) if p.get('vm_uuid')]) if cid else 0,
        'default_mode': 'incremental' if cid and any(p.get('vm_uuid') for p in db.get_proxies(cid, enabled_only=True)) else 'export',
        # where this cluster's exports go: the share assigned to its folder, or null
        'location': {'share_name': loc['share_name'], 'base': loc['base'], 'tenant': loc['tenant']} if loc else None,
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
    churn = {}
    try:
        # the newest snapshot of a VM carries blockCountDiff (1 MiB blocks) against the previous one:
        # a free indication of how much a VM changes, from HyperCore's own schedules or our anchors
        for snap in jobs.client_for(cid).list_snapshots():
            u = snap.get('domainUUID')
            if u and (u not in churn or (snap.get('timestamp') or 0) > churn[u]['at']):
                churn[u] = {'mib': snap.get('blockCountDiff'), 'at': snap.get('timestamp') or 0, 'label': snap.get('label')}
    except HyperCoreError:
        pass
    for vm in vm_list:
        vm['schedule'] = scheduled.get(vm['uuid'])
        vm['safe_name'] = jobs.safe_name(vm['name'] or '')
        vm['churn'] = churn.get(vm['uuid'])
    return jsonify(vm_list)


# ---------------------------------------------------------------- schedules
@app.get('/api/v1/plans')
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


@app.post('/api/v1/plans')
@_require_token
def schedules_add():
    b = request.get_json(silent=True) or {}
    cid = (b.get('cluster_id') or '').strip()
    if not cluster_registry.is_uuid(cid) or not cluster_registry.get(cid):
        return _err('unknown cluster')
    run_time = _valid_time(b.get('run_time') or cfg_module.load().default_run_time)
    if not run_time:
        return _err('run_time must be HH:MM (24h)')
    for k in ('vm_uuid', 'vm_name'):
        if not (b.get(k) or '').strip():
            return _err(f'{k} is required')
    if not (b.get('path_uri_base') or '').strip() and not jobs.location_for_cluster(cid):
        return _err("no share is assigned to this cluster's folder; an admin can add one under Settings → Shares and assign it from the folder menu")
    try:
        retention = max(1, int(b.get('retention') or cfg_module.load().default_retention))
        full_every_n = max(1, int(b.get('full_every_n') or 14))
        retention_days = int(b['retention_days']) if b.get('retention_days') not in (None, '', 0, '0') else None
    except (TypeError, ValueError):
        return _err('retention, full_every_n and retention_days must be numbers')
    mode = (b.get('mode') or '').strip() or ('incremental' if any(p.get('vm_uuid') for p in db.get_proxies(cid, enabled_only=True)) else 'export')
    if mode not in ('export', 'incremental'):
        return _err("mode must be 'export' or 'incremental'")
    if mode == 'incremental' and not any(p.get('vm_uuid') for p in db.get_proxies(cid, enabled_only=True)):
        return _err('incremental plans need a registered backup proxy for this cluster (Settings → Clusters → Proxies)')
    try:
        sid = db.add_schedule(cluster_id=cid, vm_uuid=b['vm_uuid'], vm_name=b['vm_name'],
                              path_uri_base=(b.get('path_uri_base') or '').strip(), smb_user=b.get('smb_user'),
                              smb_password=b.get('smb_password'), prune_path=b.get('prune_path'),
                              retention=retention, run_time=run_time, compress=bool(b.get('compress')),
                              mode=mode, full_every_n=full_every_n, retention_days=retention_days)
    except Exception as e:
        if 'duplicate key' in str(e):
            return _err('this VM already has a schedule on this cluster', 409)
        return _err(str(e), 500)
    scheduler.sync_jobs()
    return jsonify({'ok': True, 'id': sid}), 201


@app.put('/api/v1/plans/<int:sid>')
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
    if 'mode' in b:
        if b['mode'] not in ('export', 'incremental'):
            return _err("mode must be 'export' or 'incremental'")
        fields['mode'] = b['mode']
    if 'full_every_n' in b:
        try:
            fields['full_every_n'] = max(1, int(b['full_every_n']))
        except (TypeError, ValueError):
            return _err('full_every_n must be a number')
    if 'retention_days' in b:
        try:
            fields['retention_days'] = int(b['retention_days']) if b['retention_days'] not in (None, '', 0, '0') else None
        except (TypeError, ValueError):
            return _err('retention_days must be a number')
    db.update_schedule(sid, **fields)
    scheduler.sync_jobs()
    return jsonify({'ok': True})


@app.post('/api/v1/plans/<int:sid>/toggle')
@_require_token
def schedules_toggle(sid):
    if not db.get_schedule(sid):
        return _err('schedule not found', 404)
    db.toggle_schedule(sid)
    scheduler.sync_jobs()
    return jsonify({'ok': True})


@app.delete('/api/v1/plans/<int:sid>')
@_require_token
def schedules_delete(sid):
    db.delete_schedule(sid)
    scheduler.sync_jobs()
    return jsonify({'ok': True})


@app.post('/api/v1/plans/<int:sid>/run')
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
        'runs':     [_ser(r) for r in db.get_runs(_cluster_arg(), sid, limit)],
        'imports':  [] if sid else [_ser(i) for i in db.get_imports(_cluster_arg(), limit)],
        'restores': [] if sid else [_ser(r) for r in db.get_restores(_cluster_arg(), limit)],
    })


@app.get('/api/v1/runs/status')
@_require_token
def runs_status():
    """Light snapshot the dashboard polls while anything is running."""
    sid = request.args.get('schedule_id', type=int)
    runs_ = [{'id': r['id'], 'status': r['status'], 'message': r['message'] or ''} for r in db.get_runs(_cluster_arg(), sid, 50)]
    imps = [] if sid else [{'id': i['id'], 'status': i['status'], 'message': i['message'] or ''} for i in db.get_imports(_cluster_arg(), 50)]
    rsts = [] if sid else [{'id': i['id'], 'status': i['status'], 'message': i['message'] or ''} for i in db.get_restores(_cluster_arg(), 50)]
    return jsonify({'runs': runs_, 'imports': imps, 'restores': rsts, 'active': any(x['status'] == 'RUNNING' for x in (*runs_, *imps, *rsts))})


# ------------------------------------------------------------------ imports
def _folder_source(target_cluster):
    """(base, user, password) of the share assigned to the target cluster's folder, or None."""
    loc = jobs.location_for_cluster(target_cluster.cluster_id) if target_cluster else None
    return (loc['base'], loc['user'], loc['password']) if loc else None


def _resolve_source(b: dict, target_cluster):
    """(base_uri, user, password). A saved location (known_id -> schedule) keeps its credentials
    server-side and is scoped to the target cluster's tenant folder, so an import can only pick
    up exports that belong to the same HyperCore Manager folder. A manually typed source is used
    as typed (admin-only route)."""
    if not b.get('known_id') and not (b.get('source_uri') or '').strip():
        src = _folder_source(target_cluster)
        if src:
            return src
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
    if not base:
        return _err("no share is assigned to this cluster's folder")
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


# ------------------------------------------------------------------- shares
@app.get('/api/v1/targets')
@_require_token
def shares_list():
    return jsonify([_ser(r) for r in db.get_shares()])


def _share_body(b: dict):
    return {k: (b.get(k) or '') for k in ('name', 'uri_base', 'username', 'password', 'prune_path')}


@app.post('/api/v1/targets')
@_require_token
def shares_add():
    f = _share_body(request.get_json(silent=True) or {})
    if not f['name'].strip() or not f['uri_base'].strip():
        return _err('name and uri_base are required')
    if not f['uri_base'].strip().lower().startswith(('smb://', 'nfs://')):
        return _err('uri_base must start with smb:// or nfs://')
    try:
        sid = db.add_share(f['name'], f['uri_base'], f['username'], f['password'], f['prune_path'])
    except Exception as e:
        if 'duplicate key' in str(e):
            return _err('a share with that name already exists', 409)
        return _err(str(e), 500)
    return jsonify({'ok': True, 'id': sid}), 201


@app.put('/api/v1/targets/<int:sid>')
@_require_token
def shares_update(sid):
    if not db.get_share(sid):
        return _err('share not found', 404)
    b = request.get_json(silent=True) or {}
    fields = {k: b[k] for k in ('name', 'uri_base', 'username', 'password', 'prune_path') if k in b}
    if 'uri_base' in fields and not str(fields['uri_base']).strip().lower().startswith(('smb://', 'nfs://')):
        return _err('uri_base must start with smb:// or nfs://')
    try:
        if 'default_for_unfiled' in b:
            db.set_default_share(sid if b['default_for_unfiled'] else None)
        db.update_share(sid, **fields)
    except Exception as e:
        if 'duplicate key' in str(e):
            return _err('a share with that name already exists', 409)
        return _err(str(e), 500)
    return jsonify({'ok': True})


@app.delete('/api/v1/targets/<int:sid>')
@_require_token
def shares_delete(sid):
    db.delete_share(sid)          # folders referencing it fall back to no share (ON DELETE SET NULL)
    return jsonify({'ok': True})


@app.post('/api/v1/targets/<int:sid>/test')
@_require_token
def shares_test(sid):
    """List the share root with the stored credentials (SMB); NFS reports the mount path state."""
    share = db.get_share(sid, with_secret=True)
    if not share:
        return _err('share not found', 404)
    base = share['uri_base']
    if base.lower().startswith('smb://'):
        try:
            folders = jobs.smb_list_dirs(base, share.get('username') or '', share.get('password') or None)
        except HyperCoreError as e:
            return jsonify({'ok': False, 'error': str(e)})
        return jsonify({'ok': True, 'folders': folders})
    import os
    pp = share.get('prune_path')
    return jsonify({'ok': bool(pp and os.path.isdir(pp)),
                    'error': None if pp and os.path.isdir(pp) else 'NFS shares need a mount path inside the backup container for pruning; exports still work without it.'})


@app.post('/api/v1/targets/probe')
@_require_token
def shares_probe():
    """Test credentials from the add/edit form before saving."""
    f = _share_body(request.get_json(silent=True) or {})
    if not f['uri_base'].strip().lower().startswith('smb://'):
        return jsonify({'ok': True, 'folders': [], 'note': 'only SMB shares can be probed'})
    try:
        return jsonify({'ok': True, 'folders': jobs.smb_list_dirs(f['uri_base'].strip(), f['username'], f['password'] or None)})
    except HyperCoreError as e:
        return jsonify({'ok': False, 'error': str(e)})


# ------------------------------------------------------------------ proxies
@app.get('/api/v1/proxies')
@_require_token
def proxies_list():
    return jsonify([_ser(p) for p in db.get_proxies(None if request.args.get('all') else _cluster_arg())])


@app.post('/api/v1/proxies')
@_require_token
def proxies_add():
    b = request.get_json(silent=True) or {}
    cid = (b.get('cluster_id') or '').strip()
    if not cluster_registry.is_uuid(cid) or not cluster_registry.get(cid):
        return _err('unknown cluster')
    url = (b.get('url') or '').strip().rstrip('/')
    if not url.startswith(('http://', 'https://')):
        return _err('url must start with http:// or https://')
    name = (b.get('name') or '').strip() or url
    fp = normalise_fingerprint(b.get('tls_fingerprint'))
    if url.startswith('https://') and not fp and not b.get('tls_verify'):
        try:
            fp = certificate_fingerprint(url)          # pin whatever the proxy presents right now (TOFU)
        except Exception as e:                          # noqa: BLE001
            return _err(f'could not read the proxy certificate: {e}', 502)
    try:
        pid = db.add_proxy(cid, name, url, b.get('token'), kind=(b.get('kind') or 'remote'), tls_fingerprint=fp,
                           tls_verify=bool(b.get('tls_verify')))
    except Exception as e:
        if 'duplicate key' in str(e):
            return _err('this proxy URL is already registered for the cluster', 409)
        return _err(str(e), 500)
    result = incremental.probe_proxy(pid)
    return jsonify({'ok': True, 'id': pid, 'probe': result, 'tls_fingerprint': fp}), 201


@app.put('/api/v1/proxies/<int:pid>')
@_require_token
def proxies_update(pid):
    if not db.get_proxy(pid):
        return _err('proxy not found', 404)
    b = request.get_json(silent=True) or {}
    fields = {k: b[k] for k in ('name', 'url', 'kind', 'vm_uuid', 'tls_verify', 'enabled', 'token') if k in b}
    if 'tls_fingerprint' in b:
        fields['tls_fingerprint'] = normalise_fingerprint(b['tls_fingerprint'])
    if 'url' in fields:
        fields['url'] = str(fields['url']).strip().rstrip('/')
    db.update_proxy(pid, **fields)
    return jsonify({'ok': True})


@app.delete('/api/v1/proxies/<int:pid>')
@_require_token
def proxies_delete(pid):
    db.delete_proxy(pid)
    return jsonify({'ok': True})


@app.post('/api/v1/proxies/<int:pid>/test')
@_require_token
def proxies_test(pid):
    if not db.get_proxy(pid):
        return _err('proxy not found', 404)
    try:
        return jsonify(incremental.probe_proxy(pid))
    except (incremental.IncrementalError, ProxyError, HyperCoreError) as e:
        return jsonify({'ok': False, 'error': str(e)})


# ------------------------------------------------------------ chains/points
@app.get('/api/v1/chains')
@_require_token
def chains_list():
    cid = _cluster_arg()
    rows = []
    for ch in db.get_chains(cid, vm_uuid=request.args.get('vm_uuid') or None):
        d = _ser(ch)
        d.pop('active_clones', None)
        rows.append(d)
    return jsonify(rows)


@app.get('/api/v1/chains/<int:chain_id>/points')
@_require_token
def chain_points(chain_id):
    ch = db.get_chain(chain_id)
    if not ch:
        return _err('chain not found', 404)
    return jsonify([_ser(p) for p in db.get_points(chain_id)])


@app.delete('/api/v1/chains/<int:chain_id>')
@_require_token
def chain_delete(chain_id):
    """Delete a chain's data on the target (explicit action; retiring a plan never does this)."""
    ch = db.get_chain(chain_id)
    if not ch:
        return _err('chain not found', 404)
    share = db.get_share(ch['target_id'], with_secret=True) if ch.get('target_id') else None
    if share:
        try:
            proxy = incremental.pick_proxy(ch['cluster_id'])
            pc = incremental.proxy_client_for(proxy)
            pc.wait(pc.submit('delete_chain', {'target': {'uri': share['uri_base'], 'username': share.get('username'),
                                                           'password': share.get('password')}, 'chain_dir': ch['chain_dir']}), timeout=3600)
        except (incremental.IncrementalError, ProxyError) as e:
            return _err(f'could not remove the chain on the target: {e}', 502)
    for p in db.get_points(chain_id):
        db.update_point(p['id'], status='pruned')
    db.update_chain(chain_id, status='deleted')
    return jsonify({'ok': True})


@app.post('/api/v1/chains/<int:chain_id>/verify')
@_require_token
def chain_verify(chain_id):
    ch = db.get_chain(chain_id)
    if not ch:
        return _err('chain not found', 404)
    share = db.get_share(ch['target_id'], with_secret=True) if ch.get('target_id') else None
    if not share:
        return _err('the chain has no target any more')
    try:
        proxy = incremental.pick_proxy(ch['cluster_id'])
        pc = incremental.proxy_client_for(proxy)
        res = pc.wait(pc.submit('verify', {'target': {'uri': share['uri_base'], 'username': share.get('username'),
                                                      'password': share.get('password')}, 'chain_dir': ch['chain_dir']}), timeout=6 * 3600)['result']
    except (incremental.IncrementalError, ProxyError) as e:
        return _err(str(e), 502)
    if res.get('ok'):
        for p in db.get_points(chain_id):
            db.update_point(p['id'], verified_at=db.now_utc())
    return jsonify(res)


# ----------------------------------------------------------------- restores
@app.get('/api/v1/restores')
@_require_token
def restores_list():
    limit = max(1, min(request.args.get('limit', 50, type=int), 500))
    return jsonify([_ser(r) for r in db.get_restores(_cluster_arg(), limit)])


@app.post('/api/v1/restores')
@_require_token
def restores_start():
    """Restore a point as a new VM on its cluster. body: {point_id, target_name, mac_policy: auto|original|new,
    nic_connected: bool, start: bool}"""
    b = request.get_json(silent=True) or {}
    point = db.get_point(int(b.get('point_id') or 0)) if str(b.get('point_id') or '').isdigit() else None
    chain = db.get_chain(point['chain_id']) if point else None
    if not point or not chain or point.get('status') != 'ok':
        return _err('restore point not found')
    if chain['status'] == 'deleted':
        return _err('this chain was deleted')
    policy = (b.get('mac_policy') or 'auto')
    if policy not in ('auto', 'original', 'new'):
        return _err('mac_policy must be auto, original or new')
    name = (b.get('target_name') or f"{chain['vm_name']}-restored").strip()
    try:
        existing = {(v['name'] or '').lower() for v in jobs.client_for(chain['cluster_id']).list_vms()}
    except HyperCoreError as e:
        return _err(f'could not verify VM names on the cluster ({e}); restore not started', 502)
    if name.lower() in existing:
        return _err(f"a VM named '{name}' already exists on the cluster; choose another name", 409)
    params = {'mac_policy': policy, 'nic_connected': bool(b.get('nic_connected', False)), 'start': bool(b.get('start', False))}
    rid = db.add_restore(point, chain, name, params, b.get('requested_by'))
    scheduler.run_restore(rid)
    return jsonify({'ok': True, 'id': rid}), 201


# ------------------------------------------------------- file-level recovery
@app.get('/api/v1/flr/mounts')
@_require_token
def flr_mounts():
    return jsonify(flr.list_mounts(_cluster_arg()))


@app.post('/api/v1/flr/mounts')
@_require_token
def flr_mount():
    b = request.get_json(silent=True) or {}
    try:
        return jsonify(flr.mount_point(int(b.get('point_id') or 0), b.get('requested_by'))), 201
    except (incremental.IncrementalError, ProxyError, ValueError) as e:
        return _err(str(e), 502)


@app.get('/api/v1/flr/mounts/<mid>/ls')
@_require_token
def flr_ls(mid):
    try:
        return jsonify(flr.ls(mid, request.args.get('path') or '/'))
    except (incremental.IncrementalError, ProxyError) as e:
        return _err(str(e), 404 if 'not found' in str(e) else 502)


@app.get('/api/v1/flr/mounts/<mid>/<kind>')
@_require_token
def flr_download(mid, kind):
    """Streams a file (kind=file, ?path=) or a zip (kind=zip, ?paths=a,b&name=) from the proxy."""
    if kind not in ('file', 'zip'):
        return _err('unknown download kind', 404)
    from flask import Response, stream_with_context
    try:
        r = flr.stream(mid, kind, dict(request.args))
    except (incremental.IncrementalError, ProxyError) as e:
        return _err(str(e), 404 if 'not found' in str(e) else 502)
    headers = {k: v for k, v in r.headers.items() if k.lower() in ('content-length', 'content-disposition', 'content-type')}
    return Response(stream_with_context(r.iter_content(1 << 20)), status=r.status_code, headers=headers)


@app.post('/api/v1/flr/mounts/<mid>/iso')
@_require_token
def flr_iso(mid):
    """Deliver the selected files as an ISO inserted into a VM's CD-ROM. body: {paths:[...], vm_uuid}"""
    b = request.get_json(silent=True) or {}
    try:
        rid = flr.start_iso_delivery(mid, [p for p in (b.get('paths') or []) if p], (b.get('vm_uuid') or '').strip(), b.get('requested_by'))
    except (incremental.IncrementalError, ProxyError, HyperCoreError) as e:
        return _err(str(e), 502)
    scheduler.run_iso_delivery(rid)
    return jsonify({'ok': True, 'id': rid}), 201


@app.post('/api/v1/flr/iso/<int:rid>/eject')
@_require_token
def flr_eject(rid):
    try:
        return jsonify({'ok': True, 'iso': flr.eject_iso(rid)})
    except (incremental.IncrementalError, HyperCoreError) as e:
        return _err(str(e), 502)


@app.delete('/api/v1/flr/mounts/<mid>')
@_require_token
def flr_unmount(mid):
    flr.unmount(mid)
    return jsonify({'ok': True})


# ---------------------------------------------------------- proxy deployment
@app.get('/api/v1/proxies/deployments')
@_require_token
def deployments_list():
    return jsonify([_ser(d) for d in db.get_deployments(None if request.args.get('all') else _cluster_arg())])


@app.post('/api/v1/proxies/deploy')
@_require_token
def deployments_start():
    """Create a proxy VM on a cluster from a cloud image. body: {cluster_id, name, vcpu, mem_gb, disk_gb, vlan,
    ip:{mode, address, prefix, gateway, dns}, image_url, image_name, proxy_image, proxy_image_tar_url, authorized_key, tz}"""
    import deploy
    b = request.get_json(silent=True) or {}
    cid = (b.get('cluster_id') or '').strip()
    if not cluster_registry.is_uuid(cid) or not cluster_registry.get(cid):
        return _err('unknown cluster')
    name = (b.get('name') or '').strip()
    if not name or not jobs.safe_name(name) == name:
        return _err('name is required and may only contain letters, digits, dots, dashes and underscores')
    ip = b.get('ip') or {}
    if ip.get('mode') == 'static' and not ip.get('address'):
        return _err('a static address needs ip.address')
    try:
        params = {'name': name, 'vcpu': max(1, int(b.get('vcpu') or 1)), 'mem_gb': max(1, int(b.get('mem_gb') or 2)),
                  'disk_gb': max(10, int(b.get('disk_gb') or 20)), 'vlan': int(b.get('vlan') or 0), 'ip': ip,
                  'image_url': (b.get('image_url') or deploy.DEFAULT_IMAGE_URL).strip(), 'image_name': (b.get('image_name') or deploy.DEFAULT_IMAGE_NAME).strip(),
                  'proxy_image': (b.get('proxy_image') or deploy.DEFAULT_PROXY_IMAGE).strip(), 'proxy_image_tar_url': (b.get('proxy_image_tar_url') or '').strip(),
                  'authorized_key': (b.get('authorized_key') or '').strip(), 'tz': (b.get('tz') or os.environ.get('TZ') or 'UTC')}
    except (TypeError, ValueError):
        return _err('sizes and vlan must be numbers')
    did = db.add_deployment(cid, name, params, b.get('requested_by'))
    scheduler.run_deployment(did)
    return jsonify({'ok': True, 'id': did}), 201


def start(cfg: cfg_module.Config) -> None:
    app.config['API_TOKEN'] = cfg.api_token
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=cfg.api_port, debug=False, use_reloader=False),
                     name='backup-api', daemon=True).start()
    log.info('Backup REST API started', extra={'port': cfg.api_port})
