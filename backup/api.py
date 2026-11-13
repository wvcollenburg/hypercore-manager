"""
api.py — REST API of the Backup service (port 8082), consumed by the dashboard proxy.

Bearer token (BACKUP_API_TOKEN); empty token = no auth (dev). Every list call takes
?cluster=<uuid>; mutations name the cluster in the body or via the schedule.
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import date, datetime, timezone
from functools import wraps

from flask import Flask, jsonify, request

import cluster_registry
import config as cfg_module
import db
import jobs
import flr
import incremental
import policies
import proxy_image
import recurrence
import scheduler
import vmedit
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
        out[k] = v.isoformat() if isinstance(v, (datetime, date)) else v
    return out


def _plain(v):
    """Nested rows as JSON: datetimes and dates as ISO text."""
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    return v


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
    pf = None
    if cid:
        try:
            pf = jobs.preflight(jobs.client_for(cid), cid, max_age=60)
            if not pf['ok']:
                pf = dict(pf, hint=jobs.DEFERRED_HINT.get(pf['kind'], ''))
        except HyperCoreError as e:
            pf = {'ok': True, 'kind': None, 'detail': '', 'nodes': [], 'unavailable': str(e)}
    return jsonify({
        'service': 'backup',
        # update in progress / node missing: the dashboard shows a banner, runs are deferred
        'preflight': pf,
        'default_retention': cfg.default_retention,
        'default_run_time': cfg.default_run_time,
        # plan run times are read in the cluster's zone (HyperCore's /TimeZone, or the stack's TZ)
        'cluster_timezone': (lambda c: c.effective_timezone if c else None)(cluster_registry.get(cid)) if cid else None,
        'task_timeout_hours': cfg.task_timeout_hours,
        'plans': len(db.get_schedules(cid)),
        'proxies': len([p for p in db.get_proxies(cid, enabled_only=True) if p.get('vm_uuid')]) if cid else 0,
        'default_mode': 'incremental' if cid and any(p.get('vm_uuid') for p in db.get_proxies(cid, enabled_only=True)) else 'export',
        # where this cluster's exports go: the share assigned to its folder, or null
        'location': {'share_name': loc['share_name'], 'base': loc['base'], 'tenant': loc['tenant']} if loc else None,
    })


# ------------------------------------------------------------------ VM edits
# Edits and power actions for one VM, now or at run_at (see vmedit.py). The dashboard lets only
# admins through and names the user in created_by / cancelled_by.
def _vmedit_row(r):
    return _plain({k: v for k, v in r.items()})


@app.get('/api/v1/vmedit/vm')
@_require_token
def vmedit_vm():
    """Everything the edit window needs: the VM, the cluster's nodes, ISOs, snapshot schedules and
    this VM's replications, plus its change sets."""
    cid, vm_uuid = _cluster_arg(), (request.args.get('vm') or '').strip()
    if not cid or not vm_uuid:
        return _err('cluster and vm required')
    try:
        hc = jobs.client_for(cid)
        vm = hc.get_vm(vm_uuid)
        nodes = hc.list_nodes()
        isos = hc.list_isos()
        schedules = hc._request('GET', '/VirDomainSnapshotSchedule') or []
        reps = [r for r in (hc._request('GET', '/VirDomainReplication') or []) if r.get('sourceDomainUUID') == vm_uuid]
        # the target cluster's name comes from the replication's connection (a replication's own label is usually empty)
        conn_list = hc._request('GET', '/RemoteClusterConnection') or []
        conns = {c.get('uuid'): c for c in conn_list}
        snaps = hc.list_snapshots(vm_uuid)
    except HyperCoreError as e:
        return _err(str(e), 502)
    vm.pop('cloudInitData', None)                    # often holds passwords or keys
    return jsonify({
        'vm': vm,
        'nodes': [{'uuid': n.get('uuid'), 'lan_ip': n.get('lanIP'), 'peer_id': n.get('peerID')} for n in nodes],
        'isos': [{'uuid': i.get('uuid'), 'name': i.get('name'), 'size': i.get('size'), 'ready': i.get('readyForInsert')}
                 for i in isos],
        'schedules': [{'uuid': x.get('uuid'), 'name': x.get('name')} for x in schedules],
        'replications': [{'uuid': r.get('uuid'), 'label': r.get('label'), 'enable': r.get('enable'),
                          'target': r.get('targetDomainUUID'),
                          'connection': r.get('connectionUUID'),
                          'target_cluster': ((conns.get(r.get('connectionUUID')) or {}).get('remoteClusterInfo') or {}).get('clusterName') or '',
                          'connection_status': (conns.get(r.get('connectionUUID')) or {}).get('connectionStatus') or ''}
                         for r in reps],
        'connections': [{'uuid': c.get('uuid'), 'cluster': (c.get('remoteClusterInfo') or {}).get('clusterName') or c.get('uuid'),
                         'status': c.get('connectionStatus') or ''} for c in conn_list],
        'changes': [_vmedit_row(r) for r in vmedit.list_changes(cid, vm_uuid, 20)],
        'snapshots': sorted([{'uuid': x.get('uuid'), 'label': x.get('label'), 'timestamp': x.get('timestamp'), 'type': x.get('type'),
                              'retain_until': x.get('localRetainUntilTimestamp')} for x in snaps], key=lambda x: -(x['timestamp'] or 0)),
    })


def _new_vm_options(hc):
    return {'nodes': [{'uuid': n.get('uuid'), 'lan_ip': n.get('lanIP'), 'peer_id': n.get('peerID')} for n in hc.list_nodes()],
            'isos': [{'uuid': i.get('uuid'), 'name': i.get('name'), 'size': i.get('size')} for i in hc.list_isos() if i.get('readyForInsert') is not False],
            'images': [{'uuid': d.get('uuid'), 'name': d.get('name'), 'capacity': d.get('capacityBytes') or d.get('capacity')}
                       for d in (hc._request('GET', '/VirtualDisk') or [])],
            'schedules': [{'uuid': x.get('uuid'), 'name': x.get('name')} for x in (hc._request('GET', '/VirDomainSnapshotSchedule') or [])]}


@app.get('/api/v1/vmedit/options')
@_require_token
def vmedit_options():
    """What the New VM form offers: ISOs, images (VirtualDisk), snapshot schedules, nodes."""
    cid = _cluster_arg()
    if not cid:
        return _err('cluster required')
    try:
        return jsonify(_new_vm_options(jobs.client_for(cid)))
    except HyperCoreError as e:
        return _err(str(e), 502)


@app.post('/api/v1/vmedit/new')
@_require_token
def vmedit_new():
    """{cluster_id, spec, run_at, created_by}: a new VM, now or at run_at."""
    b = request.get_json(silent=True) or {}
    cid = str(b.get('cluster_id') or '')
    if not cluster_registry.is_uuid(cid):
        return _err('cluster_id required')
    run_at = None
    if b.get('run_at'):
        try:
            run_at = datetime.fromisoformat(str(b['run_at']).replace('Z', '+00:00'))
        except ValueError:
            return _err('run_at must be an ISO date and time')
        if run_at.tzinfo is None or run_at < datetime.now(timezone.utc):
            return _err('run_at must be a future time with a time zone')
    try:
        opts = _new_vm_options(jobs.client_for(cid))
        p = vmedit.plan_create(b.get('spec') or {}, opts['isos'], opts['images'], opts['schedules'])
        change_id = vmedit.create(cid, {'uuid': '', 'name': p['name']}, 'create', p, run_at, False, 10, b.get('created_by'))
    except HyperCoreError as e:
        return _err(str(e), 502)
    except vmedit.EditError as e:
        return _err(str(e), 409)
    if run_at is None:
        scheduler.enqueue_vm_change(change_id)
    return jsonify({'id': change_id, 'summary': p['summary']})


@app.get('/api/v1/vmedit/changes')
@_require_token
def vmedit_changes():
    cid = _cluster_arg()
    if not cid:
        return _err('cluster required')
    return jsonify({'changes': [_vmedit_row(r) for r in vmedit.list_changes(cid, request.args.get('vm') or None,
                                                                           int(request.args.get('limit') or 50))]})


@app.post('/api/v1/vmedit/changes')
@_require_token
def vmedit_create():
    """{cluster_id, vm_uuid, kind: edit|power|delete, ops, run_at: ISO|null, force_off,
    shutdown_timeout_min, confirm_name, created_by}"""
    b = request.get_json(silent=True) or {}
    cid, vm_uuid = str(b.get('cluster_id') or ''), str(b.get('vm_uuid') or '')
    if not cluster_registry.is_uuid(cid) or not vm_uuid:
        return _err('cluster_id and vm_uuid required')
    run_at = None
    if b.get('run_at'):
        try:
            run_at = datetime.fromisoformat(str(b['run_at']).replace('Z', '+00:00'))
        except ValueError:
            return _err('run_at must be an ISO date and time')
        if run_at.tzinfo is None:
            return _err('run_at needs a time zone')
        if run_at < datetime.now(timezone.utc):
            return _err('run_at is in the past')
    try:
        timeout = int(b.get('shutdown_timeout_min') or 10)
        if not 1 <= timeout <= 120:
            raise ValueError
    except (TypeError, ValueError):
        return _err('shutdown_timeout_min must be between 1 and 120')
    try:
        hc = jobs.client_for(cid)
        vm = hc.get_vm(vm_uuid)
        ctx = {}
        if any((o or {}).get('op') == 'replication' for o in b.get('ops') or []):
            ctx = {'replications': [r for r in (hc._request('GET', '/VirDomainReplication') or []) if r.get('sourceDomainUUID') == vm_uuid],
                   'connections': hc._request('GET', '/RemoteClusterConnection') or []}
    except HyperCoreError as e:
        return _err(str(e), 502)
    try:
        p = vmedit.plan(str(b.get('kind') or 'edit'), b.get('ops') or [], vm, ctx)
        if not vmedit.confirm_ok(p['destructive'], b.get('confirm_name'), vm):
            return _err('type the VM\'s name to confirm', 409)
        change_id = vmedit.create(cid, vm, str(b.get('kind') or 'edit'), p, run_at, bool(b.get('force_off')),
                                  timeout, b.get('created_by'))
    except vmedit.EditError as e:
        return _err(str(e), 409)
    if run_at is None:
        scheduler.enqueue_vm_change(change_id)
    return jsonify({'id': change_id, 'cycle': p['cycle'], 'summary': p['summary']})


@app.post('/api/v1/vmedit/changes/<int:change_id>/cancel')
@_require_token
def vmedit_cancel(change_id):
    b = request.get_json(silent=True) or {}
    cid = str(b.get('cluster_id') or _cluster_arg() or '')
    if not vmedit.cancel(change_id, cid, b.get('created_by')):
        return _err('only a scheduled change that has not started can be cancelled', 409)
    return jsonify({'ok': True})


# ---------------------------------------------------------------------- VMs
@app.get('/api/v1/notices')
@_require_token
def notices():
    """Warnings about the cluster that block nothing (a terminated HyperCore update), for the
    warning sign on its card in the dashboard. A muted notice is returned with its mute."""
    cid = _cluster_arg()
    if not cid:
        return _err('cluster required')
    try:
        pf = jobs.preflight(jobs.client_for(cid), cid, max_age=60)
    except HyperCoreError as e:
        return jsonify({'notices': [], 'unavailable': str(e)})
    out = []
    w = pf.get('warning')
    if w:
        mute = db.get_notice_mute(cid, w['key'])
        out.append(dict(w, muted_until=mute['muted_until'].isoformat() if mute else None,
                        muted_by=mute['muted_by'] if mute else None))
    return jsonify({'notices': out})


@app.post('/api/v1/notices/mute')
@_require_token
def mute_notice():
    """{key, days, muted_by}: mute that notice for 1-365 days; days 0 lifts the mute."""
    cid = _cluster_arg()
    b = request.get_json(silent=True) or {}
    key = str(b.get('key') or '').strip()
    if not cid or not key:
        return _err('cluster and key required')
    try:
        days = int(b.get('days'))
    except (TypeError, ValueError):
        return _err('days must be a whole number')
    if days == 0:
        db.clear_notice_mute(cid, key)
    elif 1 <= days <= 365:
        db.set_notice_mute(cid, key, days, (str(b.get('muted_by') or '').strip() or None))
    else:
        return _err('days must be between 1 and 365')
    return jsonify({'ok': True})


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
    scheduled = {s['vm_uuid']: dict(_ser(s), timezone=scheduler.plan_timezone(s)) for s in db.get_schedules(cid)}
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
        d['timezone'] = scheduler.plan_timezone(s)           # the zone run_time is read in
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
    plan = db.get_schedule(sid)
    detached = bool(plan.get('policy_id')) and any(k != 'enabled' for k in fields)
    if detached:
        fields['policy_id'] = None          # editing a member's own schedule takes it out of its backup schedule
    db.update_schedule(sid, **fields)
    scheduler.sync_jobs()
    return jsonify({'ok': True, 'detached': detached})


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


# --------------------------------------------------------- backup schedules
# A backup schedule ("policy") belongs to the folder of the selected cluster and lists members from
# every cluster in that folder. Members are added from the cluster they live on.
def _scope():
    """(folder_id, error response) for the selected cluster."""
    cid = _cluster_arg()
    if not cid:
        return None, _err('cluster is required')
    known, folder = db.folder_of_cluster(cid)
    if not known:
        return None, _err('unknown cluster')
    return folder, None


def _policy_in_scope(pid: int):
    folder, err = _scope()
    if err:
        return None, err
    pol = db.get_policy(pid)
    if not pol or pol['folder_id'] != folder:
        return None, _err('schedule not found', 404)
    return pol, None


def _policy_fields(b: dict, creating: bool, kind: str | None = None) -> dict:
    """Validated policy fields from a request body; raises recurrence.RecurrenceError (or ValueError).
    `kind`: the existing policy's kind on an update."""
    f = {}
    if creating or 'name' in b:
        name = (b.get('name') or '').strip()
        if not name or len(name) > 80:
            raise ValueError('name is required (at most 80 characters)')
        f['name'] = name
    if creating:
        if b.get('kind') not in ('incremental', 'export'):
            raise ValueError("kind must be 'incremental' or 'export'")
        f['kind'] = b['kind']
    if 'timezone' in b and b['timezone']:
        recurrence.zone(b['timezone'])
        f['timezone'] = b['timezone']
    if creating or 'rrule' in b:
        f['rrule'] = recurrence.normalise_rule(b.get('rrule') or 'FREQ=DAILY')
    if b.get('starts_on'):
        f['starts_on'] = date.fromisoformat(str(b['starts_on'])[:10])
    for k, default in (('window_open', '22:00'), ('preferred_finish', '06:00')):
        if creating or k in b:
            v = _valid_time(b.get(k) or default)
            if not v:
                raise ValueError(f'{k} must be HH:MM (24h)')
            f[k] = v
    if 'buffer_minutes' in b:
        f['buffer_minutes'] = min(720, max(0, int(b['buffer_minutes'])))
    if 'blackouts' in b:
        f['blackouts'] = recurrence.normalise_blackouts(b['blackouts'])
    if 'retention' in b:
        f['retention'] = max(1, int(b['retention']))
    if 'retention_days' in b:
        f['retention_days'] = int(b['retention_days']) if b['retention_days'] not in (None, '', 0, '0') else None
    if 'full_every_n' in b:
        f['full_every_n'] = max(1, int(b['full_every_n']))
    for k in ('compress', 'enabled'):
        if k in b:
            f[k] = bool(b[k])
    if 'full_rrule' in b:
        f['full_rrule'] = recurrence.normalise_rule(b['full_rrule']) if (b['full_rrule'] or '').strip() else None
    if 'full_kind' in b:
        if b['full_kind'] not in ('real', 'synthetic'):
            raise ValueError("full_kind must be 'real' or 'synthetic'")
        f['full_kind'] = b['full_kind']
    if f.get('full_rrule') and f.get('kind', kind) == 'export':
        raise ValueError('export schedules have no full rule: every export is a full copy')
    return f


def _policy_out(pol: dict, now: datetime) -> dict:
    d = _ser(pol)
    members = db.policy_members(pol['id'])
    d['members'] = []
    for m in members:
        last = db.last_run_for(m['id'])
        d['members'].append({'id': m['id'], 'vm_name': m['vm_name'], 'vm_uuid': m['vm_uuid'], 'cluster_id': m['cluster_id'],
                             'cluster_name': m['cluster_name'], 'enabled': m['enabled'], 'last_run': _ser(last)})
    try:
        d['next'] = recurrence.preview(pol, now, 3)
        w = recurrence.current_window(pol, now)
        d['open_now'] = bool(w) and not recurrence.in_blackout(pol, now)
    except recurrence.RecurrenceError as e:
        d['next'], d['open_now'], d['rule_error'] = [], False, str(e)
    occ = db.last_occurrence(pol['id'])
    d['last_occurrence'] = dict(_ser(db.occurrence_summary(pol['id'], occ)), occurrence=occ.isoformat()) if occ else None
    try:
        night = policies.night_view(pol, now) if d['members'] and not d.get('rule_error') else None
    except Exception:                                   # noqa: BLE001 — a summary is never worth failing the list over
        log.exception('night summary of policy %s failed', pol['id'])
        night = None
    d['night'] = None if night is None else _plain({
        'occurrence': night['occurrence'], 'open': night['open'], 'end': night['end'], 'target': night['target'],
        'finish': night['finish'], 'overrun_seconds': night['overrun_seconds'], 'full': night['full'],
        'learned': sum(1 for i in night['items'] if i['learned']), 'members': len(night['items'])})
    for m in d['members']:
        it = next((i for i in (night or {}).get('items', []) if i['plan_id'] == m['id']), None)
        m['prediction'] = _plain({k: it[k] for k in ('learned', 'seconds', 'samples', 'needed', 'planned_start', 'predicted_finish')}) if it else None
    return d


@app.get('/api/v1/policies')
@_require_token
def policies_list():
    folder, err = _scope()
    if err:
        return err
    now = datetime.now(timezone.utc)
    return jsonify({'folder_id': folder, 'folder_name': db.folder_name(folder) if folder else None,
                    'policies': [_policy_out(p, now) for p in db.get_policies(folder)]})


@app.get('/api/v1/policies/<int:pid>/night')
@_require_token
def policies_night(pid):
    """The plan of the open night (or of the next one, ?which=next): per member the slot, planned
    start, predicted finish, whether it is learned, and what ran."""
    pol, err = _policy_in_scope(pid)
    if err:
        return err
    night = policies.night_view(pol, datetime.now(timezone.utc), 'next' if request.args.get('which') == 'next' else 'current')
    return jsonify(_plain(night))


@app.post('/api/v1/policies/preview')
@_require_token
def policies_preview():
    b = request.get_json(silent=True) or {}
    try:
        f = _policy_fields(dict(b, name=b.get('name') or 'preview', kind=b.get('kind') or 'incremental'), True)
    except (ValueError, TypeError) as e:
        return _err(str(e))
    f.setdefault('timezone', b.get('timezone') or 'UTC')
    f.setdefault('blackouts', [])
    return jsonify({'windows': recurrence.preview(f, datetime.now(timezone.utc), int(b.get('count') or 10))})


@app.post('/api/v1/policies')
@_require_token
def policies_add():
    folder, err = _scope()
    if err:
        return err
    b = request.get_json(silent=True) or {}
    try:
        f = _policy_fields(b, True)
    except (ValueError, TypeError) as e:
        return _err(str(e))
    if 'timezone' not in f:
        c = cluster_registry.get(_cluster_arg())
        f['timezone'] = scheduler.plan_timezone({'cluster_timezone': c.effective_timezone if c else None})
    pid = db.add_policy(folder, **f)
    return jsonify({'ok': True, 'id': pid}), 201


@app.put('/api/v1/policies/<int:pid>')
@_require_token
def policies_update(pid):
    pol, err = _policy_in_scope(pid)
    if err:
        return err
    try:
        f = _policy_fields(request.get_json(silent=True) or {}, False, pol['kind'])
    except (ValueError, TypeError) as e:
        return _err(str(e))
    db.update_policy(pid, **f)
    return jsonify({'ok': True})


@app.post('/api/v1/policies/<int:pid>/toggle')
@_require_token
def policies_toggle(pid):
    pol, err = _policy_in_scope(pid)
    if err:
        return err
    db.toggle_policy(pid)
    return jsonify({'ok': True})


@app.post('/api/v1/policies/<int:pid>/run')
@_require_token
def policies_run_now(pid):
    """Run every active member once now, regardless of the window and the blackouts (still one
    run per proxy at a time). The dispatcher picks it up within a minute."""
    pol, err = _policy_in_scope(pid)
    if err:
        return err
    if not policies.active_members(pol):
        return _err('the schedule has no active members')
    if pol.get('manual_at'):
        return _err('a manual run of this schedule is already in progress', 409)
    db.set_manual(pid, datetime.now(timezone.utc))
    return jsonify({'ok': True})


@app.delete('/api/v1/policies/<int:pid>')
@_require_token
def policies_delete(pid):
    pol, err = _policy_in_scope(pid)
    if err:
        return err
    db.delete_policy(pid)
    scheduler.sync_jobs()
    return jsonify({'ok': True})


@app.post('/api/v1/policies/<int:pid>/members')
@_require_token
def policies_add_member(pid):
    """Add a VM of the selected cluster. Its existing plan joins the schedule (the chain continues;
    the newest assignment wins, so it leaves any other schedule); otherwise a plan is created."""
    pol, err = _policy_in_scope(pid)
    if err:
        return err
    b = request.get_json(silent=True) or {}
    cid = (b.get('cluster_id') or _cluster_arg() or '').strip()
    if db.folder_of_cluster(cid) != (True, pol['folder_id']):
        return _err("the VM's cluster is not in this schedule's folder")
    vm_uuid, vm_name = (b.get('vm_uuid') or '').strip(), (b.get('vm_name') or '').strip()
    if not vm_uuid or not vm_name:
        return _err('vm_uuid and vm_name are required')
    if not jobs.location_for_cluster(cid):
        return _err("no share is assigned to this cluster's folder; an admin can add one under Settings → Shares and assign it from the folder menu")
    if pol['kind'] == 'incremental' and not any(p.get('vm_uuid') for p in db.get_proxies(cid, enabled_only=True)):
        return _err('backup schedules need a registered backup proxy on the cluster (Settings → Clusters → Proxies)')
    plan = db.plan_for_vm(cid, vm_uuid)
    moved_from = plan.get('policy_name') if plan and plan.get('policy_id') not in (None, pid) else None
    if plan is None:
        sid = db.add_schedule(cluster_id=cid, vm_uuid=vm_uuid, vm_name=vm_name, path_uri_base='', prune_path='',
                              retention=pol['retention'], run_time=cfg_module.load().default_run_time, compress=pol['compress'],
                              mode=pol['kind'], full_every_n=pol['full_every_n'], retention_days=pol['retention_days'])
    else:
        sid = plan['id']
    db.assign_plan(sid, pid)
    scheduler.sync_jobs()
    return jsonify({'ok': True, 'plan_id': sid, 'adopted': plan is not None, 'moved_from': moved_from,
                    'mode_changed': bool(plan and plan.get('mode') != pol['kind'])}), 201


@app.delete('/api/v1/policies/<int:pid>/members/<int:sid>')
@_require_token
def policies_remove_member(pid, sid):
    """The VM keeps its plan, paused, with its chain; it can be resumed as a plan of its own."""
    pol, err = _policy_in_scope(pid)
    if err:
        return err
    plan = db.get_schedule(sid)
    if not plan or plan.get('policy_id') != pid:
        return _err('not a member of this schedule', 404)
    db.release_plan(sid)
    scheduler.sync_jobs()
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
    """Remove a proxy registration; with ?vm=delete also power off and delete the VM it ran on
    (only a VM this service deployed, see incremental.remove_proxy_vm). A proxy of kind 'local'
    runs in the stack itself and never has a VM to delete."""
    p = db.get_proxy(pid)
    if not p:
        return _err('proxy not found', 404)
    vm = 'kept'
    if request.args.get('vm') == 'delete' and p.get('vm_uuid') and p.get('kind') != 'local':
        try:
            vm = incremental.remove_proxy_vm(jobs.client_for(p['cluster_id']), p['vm_uuid'])
        except (incremental.IncrementalError, HyperCoreError) as e:
            db.delete_proxy(pid)
            return _err(f'proxy removed, VM kept: {e}', 502)
    db.delete_proxy(pid)
    return jsonify({'ok': True, 'vm': vm, 'vm_uuid': p.get('vm_uuid')})


@app.get('/api/v1/proxies/image')
@_require_token
def proxies_image():
    """The proxy image this installation can push: {version, tag, available, detail}."""
    return jsonify(proxy_image.stack_image())


@app.post('/api/v1/proxies/<int:pid>/update')
@_require_token
def proxies_update_image(pid):
    """Push the installation's proxy image to a registered proxy; the VM restarts the container on
    it. Runs in the background (the transfer and restart take minutes); the proxy's status shows
    'updating' until it answers on the new version."""
    if not db.get_proxy(pid):
        return _err('proxy not found', 404)
    try:
        pre = proxy_image.prepare(pid, info=lambda: proxy_image.stack_image(max_age=0))
    except ProxyError as e:
        return _err(str(e), 409 if 'job is running' in str(e) else 400)
    if pre.get('skipped'):
        return jsonify({'ok': True, 'skipped': pre['skipped'], 'version': pre.get('version')})
    scheduler.run_proxy_update(pid)
    return jsonify({'ok': True, 'version': pre['stack'].get('version')}), 202


@app.post('/api/v1/proxies/<int:pid>/test')
@_require_token
def proxies_test(pid):
    if not db.get_proxy(pid):
        return _err('proxy not found', 404)
    try:
        return jsonify(incremental.probe_proxy(pid))
    except (incremental.IncrementalError, ProxyError, HyperCoreError) as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.get('/api/v1/proxies/<int:pid>/logs')
@_require_token
def proxies_logs(pid):
    """The proxy's support bundle (zip): its log, recent jobs, open mounts and system state."""
    from flask import Response
    proxy = db.get_proxy(pid, with_secret=True)
    if not proxy:
        return _err('proxy not found', 404)
    try:
        data, name = incremental.proxy_client_for(proxy).support_bundle()
    except ProxyError as e:
        return _err(str(e), 502)
    name = ''.join(c for c in name if c.isalnum() or c in '-_.') or 'hcm-proxy-logs.zip'
    return Response(data, mimetype='application/zip', headers={'Content-Disposition': f'attachment; filename="{name}"'})


_BENCH: dict[int, dict] = {}          # proxy id -> {job_id, started_at, target}


@app.post('/api/v1/proxies/<int:pid>/bench')
@_require_token
def proxies_bench(pid):
    """Measure the proxy's throughput onto a target (the one assigned to its cluster, or
    body.target_id). body: {target_id?, size_mib? (default 512), profile?: write|restore, modes?: [...]}.
    Returns the proxy job id;
    GET the same path follows it."""
    p = db.get_proxy(pid, with_secret=True)
    if not p:
        return _err('proxy not found', 404)
    b = request.get_json(silent=True) or {}
    share = db.get_share(int(b['target_id']), with_secret=True) if str(b.get('target_id') or '').isdigit() else db.share_for_cluster(p['cluster_id'])
    if not share:
        return _err("no backup target is assigned to this proxy's cluster; assign one to its folder first")
    try:
        body = {'target': {'uri': share['uri_base'], 'username': share.get('username'), 'password': share.get('password')},
                'size_mib': max(16, min(int(b.get('size_mib') or 512), 8192)), 'profile': b.get('profile') or 'write'}
        if isinstance(b.get('modes'), list):
            body['modes'] = [str(m) for m in b['modes']]
        jid = incremental.proxy_client_for(p).submit('bench', body)
    except (ProxyError, ValueError) as e:
        return _err(str(e), 502)
    _BENCH[pid] = {'job_id': jid, 'started_at': db.now_utc().isoformat(), 'target': share.get('name')}
    return jsonify({'ok': True, 'job_id': jid, 'target': share.get('name')}), 202


@app.get('/api/v1/proxies/<int:pid>/bench')
@_require_token
def proxies_bench_status(pid):
    e = _BENCH.get(pid)
    if not e:
        return jsonify({'state': 'idle'})
    p = db.get_proxy(pid, with_secret=True)
    if not p:
        return _err('proxy not found', 404)
    try:
        j = incremental.proxy_client_for(p).job(e['job_id'])
    except ProxyError as ex:
        return _err(str(ex), 502)
    return jsonify({'state': j.get('state'), 'progress': j.get('progress') or {}, 'result': j.get('result'), 'error': j.get('error'),
                    'target': e['target'], 'started_at': e['started_at']})


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
    return jsonify([dict(_ser(p), short_disks=incremental.short_disks(p)) for p in db.get_points(chain_id)])


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
    incremental.release_anchor(ch)
    return jsonify({'ok': True})


def _verify_view(chain_id, entry):
    out = {'chain_id': chain_id, 'state': entry['state'], 'progress': entry.get('progress') or {}, 'error': entry.get('error'),
           'started_at': entry.get('started_at')}
    if entry.get('result') is not None:
        r = entry['result']
        out['ok'] = r.get('ok'); out['failed'] = r.get('failed') or {}; out['bytes'] = r.get('bytes'); out['duration_s'] = r.get('duration_s')
    return out


@app.post('/api/v1/chains/<int:chain_id>/verify')
@_require_token
def chain_verify(chain_id):
    """Start a background re-hash of every point of the chain; GET the same path follows it.
    A chain of several tens of GB takes longer than one HTTP request."""
    if not db.get_chain(chain_id):
        return _err('chain not found', 404)
    try:
        entry = incremental.start_verify(chain_id)
    except (incremental.IncrementalError, ProxyError) as e:
        return _err(str(e), 502)
    return jsonify(_verify_view(chain_id, entry)), 202


@app.get('/api/v1/chains/<int:chain_id>/verify')
@_require_token
def chain_verify_status(chain_id):
    entry = incremental.verify_status(chain_id)
    if entry is None:
        return jsonify({'chain_id': chain_id, 'state': 'idle'})
    return jsonify(_verify_view(chain_id, entry))


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
    nic_connected: bool, start: bool, verify: bool (default true: re-hash the backing chain first)}"""
    b = request.get_json(silent=True) or {}
    point = db.get_point(int(b.get('point_id') or 0)) if str(b.get('point_id') or '').isdigit() else None
    chain = db.get_chain(point['chain_id']) if point else None
    if not point or not chain or point.get('status') != 'ok':
        return _err('restore point not found')
    if chain['status'] == 'deleted':
        return _err('this chain was deleted')
    policy = (b.get('mac_policy') or 'new')
    if policy == 'auto':                                   # former default; MAC addresses are kept on request only
        policy = 'new'
    if policy not in ('original', 'new'):
        return _err('mac_policy must be new or original')
    name = (b.get('target_name') or f"{chain['vm_name']}-restored").strip()
    try:
        existing = {(v['name'] or '').lower() for v in jobs.client_for(chain['cluster_id']).list_vms()}
    except HyperCoreError as e:
        return _err(f'could not verify VM names on the cluster ({e}); restore not started', 502)
    if name.lower() in existing:
        return _err(f"a VM named '{name}' already exists on the cluster; choose another name", 409)
    params = {'mac_policy': policy, 'nic_connected': bool(b.get('nic_connected', False)), 'start': bool(b.get('start', False)),
              'verify': bool(b.get('verify', True))}
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
    keys = b.get('keys') or []
    if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
        return _err('keys must be a list of strings')
    try:
        return jsonify(flr.mount_point(int(b.get('point_id') or 0), b.get('requested_by'), keys=keys)), 201
    except ProxyError as e:
        if e.payload.get('locked'):
            # only encrypted volumes and no key that opens them: the dashboard asks for one
            return jsonify({'error': e.payload.get('error') or str(e), 'locked': e.payload['locked']}), 422
        return _err(str(e), 502)
    except (incremental.IncrementalError, ValueError) as e:
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


@app.delete('/api/v1/proxies/deployments')
@_require_token
def deployments_clear():
    """Remove the finished rows of the deployment log (all clusters with ?all=1, else the selected one)."""
    n = db.delete_finished_deployments(None if request.args.get('all') else _cluster_arg())
    return jsonify({'ok': True, 'removed': n})


@app.post('/api/v1/proxies/deploy')
@_require_token
def deployments_start():
    """Create a proxy VM on a cluster from a cloud image. body: {cluster_id, name, vcpu, mem_gb, disk_gb, vlan,
    ip:{mode, address, prefix, gateway, dns}, image_url, image_name, authorized_key, tz}"""
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
        params = {'name': name, 'vcpu': max(1, int(b.get('vcpu') or 2)), 'mem_gb': max(1, int(b.get('mem_gb') or 2)),
                  'disk_gb': max(10, int(b.get('disk_gb') or 20)), 'vlan': int(b.get('vlan') or 0), 'ip': ip,
                  'image_url': (b.get('image_url') or deploy.DEFAULT_IMAGE_URL).strip(), 'image_name': (b.get('image_name') or deploy.DEFAULT_IMAGE_NAME).strip(),
                  'authorized_key': (b.get('authorized_key') or '').strip(), 'tz': (b.get('tz') or os.environ.get('TZ') or 'UTC')}
    except (TypeError, ValueError):
        return _err('sizes and vlan must be numbers')
    info = proxy_image.stack_image(max_age=0)
    if not info.get('available'):
        return _err(f"this installation has no proxy image to push: {info.get('detail') or 'the dashboard did not report one'}", 502)
    if info.get('arch') not in (None, 'amd64'):
        return _err(f"this installation's proxy image is built for {info['arch']}; a HyperCore VM needs amd64. "
                    'Build the stack on an x86_64 host, or build the proxy image with --platform linux/amd64')
    did = db.add_deployment(cid, name, params, b.get('requested_by'))
    scheduler.run_deployment(did)
    return jsonify({'ok': True, 'id': did}), 201


def start(cfg: cfg_module.Config) -> None:
    app.config['API_TOKEN'] = cfg.api_token
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=cfg.api_port, debug=False, use_reloader=False),
                     name='backup-api', daemon=True).start()
    log.info('Backup REST API started', extra={'port': cfg.api_port})
