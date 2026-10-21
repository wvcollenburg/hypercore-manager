"""
incremental.py — orchestration of proxy-based incremental backups.

One run of an incremental plan:
  1. anchor snapshot of the VM (quiesced, no replication), serials per device
  2. clone every disk, the NVRAM and the vTPM read-only onto the proxy's VM
  3. changed-block map per device: snapDiff(previous serial -> anchor serial), or the allocation
     map (from serial 0) for a full
  4. one proxy job writes the point (full or delta) onto the target; progress -> run.message
  5. point recorded, chain advanced, clones removed, previous anchor removed; retention applied
  6. on any failure: clones removed, the new anchor removed, previous anchor kept; run ERROR

A missing previous anchor (deleted by hand, VM re-created) turns the run into a full with a
visible note instead of failing, as Veeam does. A new full starts a new chain; the old chain
retires and its points expire through the same retention.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

import cluster_registry
import config as cfg_module
import db
import jobs
import manifest_names
from hypercore import HyperCoreClient, HyperCoreError
from proxy_client import ProxyClient, ProxyError, match_vm_by_macs

log = logging.getLogger('backup.incremental')
CFG = cfg_module.load()
MiB = 1 << 20
ANCHOR_PREFIX = 'hcm-backup'


class IncrementalError(Exception):
    pass


_PROXY_LOCKS: dict[int, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def proxy_lock(proxy_id: int) -> threading.Lock:
    """One backup or restore at a time per proxy: the proxy VM cannot be snapshotted (restores)
    while read-only clones (backups) are attached to it."""
    with _LOCKS_GUARD:
        return _PROXY_LOCKS.setdefault(proxy_id, threading.Lock())


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------ proxies
def proxy_client_for(proxy: dict) -> ProxyClient:
    return ProxyClient(proxy['url'], proxy.get('token') or '', tls_fingerprint=proxy.get('tls_fingerprint'),
                       tls_verify=bool(proxy.get('tls_verify')))


def pick_proxy(cluster_id: str) -> dict:
    """An enabled proxy of the cluster that knows its own VM. Least recently used first."""
    candidates = [p for p in db.get_proxies(cluster_id, enabled_only=True) if p.get('vm_uuid')]
    if not candidates:
        raise IncrementalError('no enabled backup proxy with a known VM is registered for this cluster')
    candidates.sort(key=lambda p: (p.get('last_seen_at') or datetime.min.replace(tzinfo=timezone.utc)))
    chosen = candidates[0]
    db.update_proxy(chosen['id'], last_seen_at=_now())          # rotate: the next job goes to another proxy
    return db.get_proxy(chosen['id'], with_secret=True)


def probe_proxy(proxy_id: int, hc: HyperCoreClient | None = None) -> dict:
    """Contact the proxy, record its status and version, and (re)detect its VM by MAC address."""
    p = db.get_proxy(proxy_id, with_secret=True)
    if not p:
        raise IncrementalError('proxy not found')
    if p['url'].startswith('https://') and not p.get('tls_fingerprint') and not p.get('tls_verify'):
        # first contact: pin the certificate the proxy presents now (trust on first use)
        try:
            from proxy_client import certificate_fingerprint
            fp = certificate_fingerprint(p['url'])
            db.update_proxy(proxy_id, tls_fingerprint=fp)
            p['tls_fingerprint'] = fp
        except Exception as e:                      # noqa: BLE001
            db.update_proxy(proxy_id, status='error', status_detail=f'could not read the proxy certificate: {e}')
            return {'ok': False, 'error': f'could not read the proxy certificate: {e}'}
    try:
        ident = proxy_client_for(p).identity()
    except ProxyError as e:
        db.update_proxy(proxy_id, status='error', status_detail=str(e))
        return {'ok': False, 'error': str(e)}
    fields = {'status': 'ok', 'status_detail': None, 'last_seen_at': _now(), 'version': ident.get('version'),
              'capabilities': ident.get('capabilities')}
    if p.get('tls_fingerprint') and ident.get('tls_fingerprint') and ident['tls_fingerprint'] != p['tls_fingerprint']:
        fields['status'] = 'error'
        fields['status_detail'] = 'the proxy reports a different certificate than the one pinned; re-register it if it was reinstalled'
    detected = None
    macs = [m['mac'] for m in ident.get('macs', [])]
    if macs:
        try:
            hc = hc or jobs.client_for(p['cluster_id'])
            vm = match_vm_by_macs(macs, hc.list_vms_full())
            if vm:
                detected = {'uuid': vm['uuid'], 'name': vm['name']}
                fields['vm_uuid'] = vm['uuid']
        except HyperCoreError as e:
            fields['status_detail'] = f'proxy reachable; VM detection failed: {e}'
    if not detected and not p.get('vm_uuid'):
        fields['status'] = 'error'
        fields['status_detail'] = 'proxy reachable but none of its MAC addresses belongs to a VM on this cluster'
    db.update_proxy(proxy_id, **fields)
    return {'ok': fields['status'] == 'ok', 'identity': ident, 'vm': detected, 'detail': fields['status_detail']}


# ------------------------------------------------------------------ helpers
def wait_task(hc: HyperCoreClient, task_tag: str, what: str, timeout: float = 600) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = hc.task_status(task_tag)
        if st.get('state') == 'COMPLETE':
            return
        if st.get('state') == 'ERROR':
            raise IncrementalError(f'{what} failed: {st.get("formattedMessage") or st.get("formattedDescription") or "task ERROR"}')
        time.sleep(2)
    raise IncrementalError(f'{what} did not finish within {timeout:.0f}s')


def wait_snapshot(hc: HyperCoreClient, snap_uuid: str, timeout: float = 120) -> dict:
    """A snapshot task can complete without producing a snapshot (seen when clones were attached);
    the object must be CREATED before it is usable."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s = hc.get_snapshot(snap_uuid)
        if s and s.get('state') == 'CREATED':
            return s
        time.sleep(1)
    raise IncrementalError('snapshot was not created (the VM may have read-only clones attached, or the account lacks rights)')


def _safe_delete_snapshot(hc, snap_uuid, what='anchor'):
    if not snap_uuid:
        return
    try:
        tag = hc.delete_snapshot(snap_uuid)
        if tag:
            wait_task(hc, tag, f'delete {what} snapshot', 300)
    except (HyperCoreError, IncrementalError) as e:
        log.warning('could not delete %s snapshot %s: %s', what, snap_uuid, e)


def _delete_clones(hc, clone_uuids: list[str], attempts: int = 4) -> list[str]:
    """Detach clones from the proxy VM. HyperCore occasionally answers "timed out waiting for CV
    refresh" right after heavy I/O on a device; a short retry clears it. Returns the uuids that
    could not be removed (kept in chain.active_clones for the sweep)."""
    failed = []
    for u in clone_uuids:
        last = None
        for i in range(attempts):
            try:
                tag = hc.delete_block_device(u)
                if tag:
                    wait_task(hc, tag, 'detach clone', 300)
                last = None
                break
            except (HyperCoreError, IncrementalError) as e:
                last = e
                time.sleep(5 * (i + 1))
        if last is not None:
            log.warning('could not detach clone %s after %d attempts: %s', u, attempts, last)
            failed.append(u)
    return failed


def _target_for(plan) -> tuple[dict, dict]:
    """(share row with secret, location) for the plan's cluster, or raise."""
    share = db.share_for_cluster(plan['cluster_id'])
    if not share:
        raise IncrementalError("no backup target is assigned to this cluster's folder")
    loc = jobs.location_for_cluster(plan['cluster_id'])
    return share, loc


def image_size(dev: dict) -> int:
    """Virtual size the backed-up image must have. Disks: their capacity. NVRAM: its real capacity
    (OVMF refuses a variable store of another size); vTPM: its physical size."""
    if dev['type'] in HyperCoreClient.DISK_TYPES:
        return int(dev['capacity'])
    return int(dev.get('capacity') or 0) or int(dev.get('physical') or 0) or MiB


def device_capacity(dev: dict) -> int:
    """Clone capacity: disks keep their capacity, NVRAM and vTPM get 1 MiB (their real size is below that)."""
    return int(dev['capacity']) if dev['type'] in HyperCoreClient.DISK_TYPES else MiB


def plan_point(chain: dict | None, plan: dict, prev_point: dict | None) -> tuple[str, int, int | None, str | None]:
    """(kind, seq, previous_seq, note). A full when there is no chain, no previous point, no anchor,
    or the chain reached full_every_n runs since its base."""
    if chain is None or prev_point is None:
        return 'full', 0, None, None
    if not chain.get('anchor_snapshot_uuid'):
        return 'full', 0, None, 'reference snapshot missing: taking a full instead of an incremental'
    if int(chain.get('runs_since_full') or 0) >= max(1, int(plan.get('full_every_n') or 14)):
        return 'full', 0, None, f'scheduled full after {chain.get("runs_since_full")} incrementals'
    return 'delta', int(chain['next_seq']), int(prev_point['seq']), None


# ------------------------------------------------------------------ the run
def run_incremental(plan_id: int) -> None:
    plan = db.get_schedule(plan_id)
    if plan is None or not plan['enabled'] or not plan.get('cluster_enabled', True):
        log.info('plan %s skipped (missing, disabled or cluster disabled)', plan_id)
        return
    if db.is_run_active(plan_id):
        log.warning('plan %s (%s) still has a run in progress, skipping', plan_id, plan['vm_name'])
        return
    run_id = db.start_run(plan_id)
    db.update_run(run_id, mode='incremental')
    try:
        _run(plan, run_id)
    except (IncrementalError, HyperCoreError, ProxyError) as e:
        db.update_run(run_id, status='ERROR', message=str(e), finished_at=_now())
        log.error('[run %s] %s', run_id, e)
    except Exception as e:                              # noqa: BLE001 — never kill the scheduler thread
        db.update_run(run_id, status='ERROR', message=f'Unexpected error: {e}', finished_at=_now())
        log.exception('[run %s] unexpected error', run_id)


def _run(plan: dict, run_id: int) -> None:
    hc = jobs.client_for(plan['cluster_id'])
    proxy = pick_proxy(plan['cluster_id'])
    pc = proxy_client_for(proxy)
    db.update_run(run_id, proxy_id=proxy['id'])
    share, loc = _target_for(plan)
    vm = hc.get_vm(plan['vm_uuid'])
    devices = hc.backup_devices(vm)
    if not any(d['type'] in HyperCoreClient.DISK_TYPES for d in devices):
        raise IncrementalError('the VM has no disk to back up')
    definition = hc.definition_of(vm)

    chain = db.get_active_chain(plan['id'])
    prev_point = db.last_point(chain['id']) if chain else None
    # the previous anchor must still exist on the cluster, otherwise this becomes a full
    if chain and chain.get('anchor_snapshot_uuid'):
        anchor_obj = hc.get_snapshot(chain['anchor_snapshot_uuid'])
        if not anchor_obj or anchor_obj.get('state') != 'CREATED':
            db.update_chain(chain['id'], anchor_snapshot_uuid=None)
            chain['anchor_snapshot_uuid'] = None
    kind, seq, prev_seq, note = plan_point(chain, plan, prev_point)
    if kind == 'full' and chain is not None:
        # a new full starts a new chain; the old one retires and expires through retention
        db.update_chain(chain['id'], status='retired')
        old_anchor = chain.get('anchor_snapshot_uuid')
        chain = None
        prev_point = None
    else:
        old_anchor = chain.get('anchor_snapshot_uuid') if chain else None
    if chain is None:
        chain_dir = f"{loc['tenant']}/{manifest_names.chain_dirname(vm['name'], vm['uuid'], time.strftime('%Y%m%d-%H%M%S'))}"
        chain_id = db.add_chain(plan, share['id'], loc['tenant'], chain_dir)
        chain = db.get_chain(chain_id)
    db.update_run(run_id, chain_id=chain['id'], point_kind=kind, message=f'{kind}: creating reference snapshot')
    if note:
        log.warning('[run %s] %s', run_id, note)

    # 1. anchor snapshot
    label = f"{ANCHOR_PREFIX}-{plan['id']}-{run_id}"
    tag, snap_uuid = hc.create_snapshot(vm['uuid'], label, quiesce=True)
    wait_task(hc, tag, 'snapshot', 600)
    snap = wait_snapshot(hc, snap_uuid)
    serials = hc.snapshot_serials(snap)
    prev_serials = (chain.get('last_serials') or {}) if kind == 'delta' else {}
    clones: list[str] = []
    lock = proxy_lock(proxy['id'])
    lock.acquire()
    try:
        # 2. clones + 3. maps
        req_devices = []
        for dev in devices:
            cap = device_capacity(dev)
            ctag, clone_uuid = hc.clone_device(dev['uuid'], snap_uuid, proxy['vm_uuid'], cap, read_only=True)
            wait_task(hc, ctag, 'clone', 600)
            clones.append(clone_uuid)
            db.update_chain(chain['id'], active_clones=clones)
            serial = serials.get(dev['uuid'])
            if dev['type'] in HyperCoreClient.DISK_TYPES:
                if kind == 'delta' and dev['uuid'] in prev_serials:
                    dev_map = hc.snap_diff(dev['uuid'], prev_serials[dev['uuid']], serial)
                    entry = {'map': dev_map}
                else:
                    entry = {'map': hc.snap_diff(dev['uuid'], 0, serial)}
                    if kind == 'delta':
                        entry['full_map'] = entry['map']          # new disk inside a delta point
            else:
                entry = {'map': [{'lower': 0, 'upper': 0}]}       # NVRAM / vTPM: 1 MiB, always re-copied
            req_devices.append({'uuid': dev['uuid'], 'type': dev['type'], 'clone_uuid': clone_uuid, 'size': cap,
                                'image_size': image_size(dev), 'serial': serial, **entry})
        blocks = sum(jobs_blocks(d['map']) for d in req_devices)
        disk_blocks = sum(jobs_blocks(d['map']) for d in req_devices if d['type'] in HyperCoreClient.DISK_TYPES)
        if kind == 'delta' and disk_blocks == 0:
            # nothing changed on any disk since the previous point: no new point, keep the old anchor
            db.update_chain(chain['id'], active_clones=_delete_clones(hc, clones))
            lock.release()
            _safe_delete_snapshot(hc, snap_uuid, 'unneeded anchor')
            db.update_run(run_id, status='COMPLETE', finished_at=_now(), blocks_changed=0, bytes_written=0,
                          message='No blocks changed since the previous point; nothing written.')
            log.info('[run %s] no changes, skipped', run_id)
            return
        db.update_run(run_id, blocks_changed=blocks, message=f'{kind}: {blocks} MiB to copy, proxy {proxy["name"]}')

        # 4. proxy job
        body = {'target': {'uri': share['uri_base'], 'username': share.get('username'), 'password': share.get('password')},
                'folder': loc['tenant'], 'vm': {'uuid': vm['uuid'], 'name': vm['name']}, 'chain_dir': chain['chain_dir'],
                'point': {'kind': kind, 'seq': seq, 'previous_seq': prev_seq}, 'devices': req_devices,
                'definition': definition, 'plan_id': plan['id'], 'run_id': run_id}
        job_id = pc.submit('backup', body)

        def on_progress(p):
            if p.get('blocks_total'):
                total_dev = p.get('devices_total', 1)
                db.update_run(run_id, message=f'{kind}: {p.get("blocks_done", 0)}/{p["blocks_total"]} MiB, device {min(p.get("devices_done", 0) + 1, total_dev)}/{total_dev}')
        result = pc.wait(job_id, on_progress, timeout=CFG.task_timeout_hours * 3600)['result']
    except Exception:
        db.update_chain(chain['id'], active_clones=_delete_clones(hc, clones))
        lock.release()
        _safe_delete_snapshot(hc, snap_uuid, 'new anchor')
        raise

    # 5. bookkeeping
    leftover = _delete_clones(hc, clones)
    lock.release()
    point_id = db.add_point(chain['id'], seq, kind, result['point_dir'], serials, blocks, result.get('bytes_written', 0),
                            result.get('duration_s', 0), result.get('devices', []), definition, run_id)
    db.update_chain(chain['id'], anchor_snapshot_uuid=snap_uuid, last_serials=serials, next_seq=seq + 1,
                    runs_since_full=0 if kind == 'full' else int(chain.get('runs_since_full') or 0) + 1, active_clones=leftover)
    _safe_delete_snapshot(hc, old_anchor, 'previous anchor')
    msg = f'{kind} point {result["point_dir"]}: {blocks} MiB changed, {result.get("bytes_written", 0) // MiB} MiB written in {result.get("duration_s", 0):.0f}s'
    if note:
        msg = f'{note}. {msg}'
    if leftover:
        msg += f'; {len(leftover)} clone(s) could not be detached yet (the daily sweep retries)'
    retention_msg = apply_retention(plan, pc, share)
    db.update_run(run_id, status='COMPLETE', finished_at=_now(), point_id=point_id, bytes_written=result.get('bytes_written', 0),
                  message=f'{msg}. {retention_msg}'.strip())
    log.info('[run %s] %s %s', run_id, msg, retention_msg)


def jobs_blocks(intervals) -> int:
    return sum(int(i['upper']) - int(i['lower']) + 1 for i in intervals or [])


# ------------------------------------------------------------------ retention
def select_points_to_drop(points: list[dict], retention_points: int | None, retention_days: int | None,
                          now: datetime | None = None) -> list[dict]:
    """Points to prune, oldest first: beyond the count, or older than the age, but never the newest
    point of the newest (active) chain. Points are given oldest first."""
    now = now or _now()
    keep_count = max(1, int(retention_points)) if retention_points else None
    drop = []
    alive = list(points)
    if keep_count is not None and len(alive) > keep_count:
        drop.extend(alive[:len(alive) - keep_count])
        alive = alive[len(alive) - keep_count:]
    if retention_days:
        limit = now.timestamp() - 86400 * int(retention_days)
        for p in alive[:-1]:
            ts = p['created_at'].timestamp() if hasattr(p['created_at'], 'timestamp') else float(p['created_at'])
            if ts < limit:
                drop.append(p)
    return drop


def release_anchor(chain: dict, hc: HyperCoreClient | None = None) -> None:
    """Delete the reference snapshot of a chain whose data is gone (deleted by hand or by
    retention); nothing can be delta'd against it any more."""
    snap = chain.get('anchor_snapshot_uuid')
    if not snap:
        return
    try:
        hc = hc or jobs.client_for(chain['cluster_id'])
        _safe_delete_snapshot(hc, snap, 'anchor of deleted chain')
    except (HyperCoreError, IncrementalError) as e:
        log.warning('anchor snapshot %s of chain %s not deleted: %s', snap[:8], chain['id'], e)
    db.update_chain(chain['id'], anchor_snapshot_uuid=None)


def apply_retention(plan: dict, pc: ProxyClient, share: dict) -> str:
    points = db.points_for_plan_retention(plan['id'])
    drop = select_points_to_drop(points, plan.get('retention'), plan.get('retention_days'))
    if not drop:
        return f'Retention: {len(points)} point(s) kept.'
    by_chain: dict[int, list[dict]] = {}
    for p in drop:
        by_chain.setdefault(p['chain_id'], []).append(p)
    pruned = 0
    for chain_id, pts in by_chain.items():
        chain = db.get_chain(chain_id)
        remaining = [p for p in db.get_points(chain_id) if p['id'] not in {x['id'] for x in pts}]
        body = {'target': {'uri': share['uri_base'], 'username': share.get('username'), 'password': share.get('password')},
                'chain_dir': chain['chain_dir']}
        try:
            if not remaining:
                pc.wait(pc.submit('delete_chain', body), timeout=3600)
                for p in pts:
                    db.update_point(p['id'], status='pruned')
                db.update_chain(chain_id, status='deleted')
                release_anchor(chain)
            else:
                res = pc.wait(pc.submit('prune', dict(body, keep_min_seq=remaining[0]['seq'])), timeout=6 * 3600)['result']
                for p in pts:
                    db.update_point(p['id'], status='pruned')
                # the surviving oldest point now lives in the base directory
                for seq, d in (res.get('dirs') or {}).items():
                    row = next((r for r in remaining if r['seq'] == int(seq)), None)
                    if row and row['dir'] != d:
                        db._exec('UPDATE backup.points SET dir = %s WHERE id = %s', (d, row['id']))
            pruned += len(pts)
        except ProxyError as e:
            return f'Retention: pruning failed on chain {chain_id}: {e}'
    return f'Retention: pruned {pruned} point(s), {len(points) - pruned} kept.'


# ------------------------------------------------------------------ sweeps
def sweep_cluster(cluster_id: str, max_age_days: int = 30) -> dict:
    """Remove our stale anchor snapshots (not referenced by any chain, older than max_age_days) and
    clones left on proxies by interrupted runs. Called daily and on service start."""
    out = {'snapshots': 0, 'clones': 0}
    try:
        hc = jobs.client_for(cluster_id)
    except HyperCoreError as e:
        log.warning('sweep: %s', e)
        return out
    referenced = {c['anchor_snapshot_uuid'] for c in db.get_chains(cluster_id, include_deleted=True) if c.get('anchor_snapshot_uuid')}
    cutoff = time.time() - 86400 * max_age_days
    for s in hc.list_snapshots():
        if (s.get('label') or '').startswith(ANCHOR_PREFIX + '-') and s['uuid'] not in referenced and (s.get('timestamp') or 0) < cutoff:
            _safe_delete_snapshot(hc, s['uuid'], 'stale anchor')
            out['snapshots'] += 1
    for ch in db.chains_with_clones():
        if ch['cluster_id'] != cluster_id:
            continue
        _delete_clones(hc, list(ch.get('active_clones') or []))
        db.update_chain(ch['id'], active_clones=[])
        out['clones'] += len(ch.get('active_clones') or [])
    return out


def verify_active_chains(cluster_id: str) -> dict:
    """Weekly: re-hash every active chain of a cluster through its proxy; records verified_at."""
    out = {'verified': 0, 'failed': 0}
    try:
        proxy = pick_proxy(cluster_id)
    except IncrementalError:
        return out
    pc = proxy_client_for(proxy)
    for ch in db.get_chains(cluster_id):
        if ch['status'] != 'active' or not ch.get('target_id'):
            continue
        share = db.get_share(ch['target_id'], with_secret=True)
        if not share:
            continue
        try:
            res = pc.wait(pc.submit('verify', {'target': {'uri': share['uri_base'], 'username': share.get('username'),
                                                          'password': share.get('password')}, 'chain_dir': ch['chain_dir']}), timeout=6 * 3600)['result']
        except ProxyError as e:
            log.warning('verify of chain %s failed: %s', ch['id'], e)
            out['failed'] += 1
            continue
        if res.get('ok'):
            for pt in db.get_points(ch['id']):
                db.update_point(pt['id'], verified_at=_now())
            out['verified'] += 1
        else:
            log.error('chain %s (%s) has corrupt points: %s', ch['id'], ch['vm_name'], res.get('points'))
            out['failed'] += 1
    return out
