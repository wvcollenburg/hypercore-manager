"""
flr.py — file-level recovery: browse a restore point and get files out of it.

The proxy mounts the point read-only; this service only routes: it remembers which proxy holds
which mount (in memory, mounts are temporary) and streams files and zips through. Getting files
*into* a guest happens through the guest's own front door (download, or an ISO attached to the
VM), never through the hypervisor.
"""
from __future__ import annotations

import logging
import threading
import time

import db
import incremental
from hypercore import HyperCoreError
from incremental import IncrementalError, pick_proxy, proxy_client_for
from proxy_client import ProxyError

log = logging.getLogger('backup.flr')
_MOUNTS: dict[str, dict] = {}          # mount id -> {proxy_id, point_id, chain_id, cluster_id, created, filesystems}
_LOCK = threading.Lock()


def mount_point(point_id: int, requested_by: str | None = None) -> dict:
    point = db.get_point(point_id)
    chain = db.get_chain(point['chain_id']) if point else None
    if not point or not chain or point.get('status') != 'ok':
        raise IncrementalError('restore point not found')
    share = db.get_share(chain['target_id'], with_secret=True) if chain.get('target_id') else None
    if not share:
        raise IncrementalError('the backup target of this chain no longer exists')
    proxy = pick_proxy(chain['cluster_id'])
    pc = proxy_client_for(proxy)
    body = {'target': {'uri': share['uri_base'], 'username': share.get('username'), 'password': share.get('password')},
            'chain_dir': chain['chain_dir'], 'seq': point['seq']}
    m = pc._request('POST', '/mounts', json=body, timeout=300)
    entry = {'id': m['id'], 'proxy_id': proxy['id'], 'point_id': point_id, 'chain_id': chain['id'], 'cluster_id': chain['cluster_id'],
             'vm_name': chain['vm_name'], 'seq': point['seq'], 'filesystems': m.get('filesystems', []), 'created': time.time(),
             'requested_by': requested_by}
    with _LOCK:
        _MOUNTS[m['id']] = entry
    log.info('point mounted for file recovery', extra={'mount': m['id'], 'vm': chain['vm_name'], 'seq': point['seq']})
    return entry


def get_mount(mount_id: str) -> dict:
    m = _MOUNTS.get(mount_id)
    if not m:
        raise IncrementalError('mount not found (it may have been unmounted after being idle)')
    return m


def client_for_mount(mount_id: str):
    m = get_mount(mount_id)
    proxy = db.get_proxy(m['proxy_id'], with_secret=True)
    if not proxy:
        raise IncrementalError('the proxy of this mount is gone')
    return m, proxy_client_for(proxy)


def ls(mount_id: str, path: str) -> dict:
    m, pc = client_for_mount(mount_id)
    return pc._request('GET', f'/mounts/{mount_id}/ls', params={'path': path or '/'})


def stream(mount_id: str, kind: str, params: dict):
    """(requests.Response streaming) for file or zip downloads."""
    import requests
    m, pc = client_for_mount(mount_id)
    headers = {'Authorization': f'Bearer {pc.token}'} if pc.token else {}
    verify = pc.tls_verify if pc.url.startswith('https://') and not pc.fingerprint else (False if pc.fingerprint else True)
    try:
        r = requests.get(f'{pc.url}/mounts/{mount_id}/{kind}', params=params, headers=headers, stream=True, timeout=(10, 600), verify=verify)
    except requests.exceptions.RequestException as e:
        raise ProxyError(f'proxy unreachable: {e}') from e
    if r.status_code >= 400:
        raise ProxyError(f'proxy returned {r.status_code}')
    return r


def unmount(mount_id: str) -> None:
    with _LOCK:
        m = _MOUNTS.pop(mount_id, None)
    if not m:
        return
    proxy = db.get_proxy(m['proxy_id'], with_secret=True)
    if proxy:
        try:
            proxy_client_for(proxy)._request('DELETE', f'/mounts/{mount_id}')
        except ProxyError as e:
            log.warning('unmount %s failed: %s', mount_id, e)


def list_mounts(cluster_id: str | None = None) -> list[dict]:
    return [dict(m) for m in _MOUNTS.values() if cluster_id is None or m['cluster_id'] == cluster_id]


# ------------------------------------------------------------------ ISO delivery
ISO_MAX_BYTES = 8 << 30


def iso_name(vm_name: str) -> str:
    safe = ''.join(c if c.isalnum() or c in '._-' else '_' for c in vm_name)[:40] or 'vm'
    return f'hcm-recovery-{safe}-{time.strftime("%Y%m%d-%H%M%S")}.iso'


def start_iso_delivery(mount_id: str, paths: list[str], vm_uuid: str, requested_by: str | None = None) -> int:
    """Validate and record a file delivery; the scheduler runs deliver_iso()."""
    import jobs
    m = get_mount(mount_id)
    if not paths:
        raise IncrementalError('select at least one file or directory')
    hc = jobs.client_for(m['cluster_id'])
    vm = hc.get_vm(vm_uuid)
    point = db.get_point(m['point_id']); chain = db.get_chain(m['chain_id'])
    rid = db.add_restore(point, chain, vm.get('name') or vm_uuid, {'kind': 'files', 'mount_id': mount_id, 'paths': paths, 'vm_uuid': vm_uuid}, requested_by)
    db._exec("UPDATE backup.restores SET kind = 'files' WHERE id = %s", (rid,))
    return rid


def deliver_iso(restore_id: int) -> None:
    import jobs
    r = db.get_restore(restore_id)
    if not r:
        return
    params = r.get('params') or {}
    try:
        m, pc = client_for_mount(params['mount_id'])
        hc = jobs.client_for(m['cluster_id'])
        vm = hc.get_vm(params['vm_uuid'])
        paths = ','.join(params['paths'])
        size = pc._request('GET', f"/mounts/{m['id']}/iso", params={'paths': paths, 'size': 1}, timeout=3600)['size']
        if size > ISO_MAX_BYTES:
            raise IncrementalError(f'the selection would make a {size >> 30} GiB ISO; the limit is {ISO_MAX_BYTES >> 30} GiB')
        name = iso_name(vm.get('name') or 'vm')
        db.update_restore(restore_id, message=f'uploading {name} ({size >> 20} MiB) to the cluster')
        iso_uuid = hc.create_iso(name, size)
        try:
            resp = stream(m['id'], 'iso', {'paths': paths})
            hc.upload_iso(iso_uuid, resp.iter_content(1 << 20), size)
            tag = hc.ready_iso(iso_uuid)
            if tag:
                incremental.wait_task(hc, tag, 'ISO ready', 600)
            cdrom = next((b for b in vm.get('blockDevs', []) if b.get('type') == 'IDE_CDROM'), None)
            if not cdrom:
                tag, cd_uuid = hc.create_block_device(vm['uuid'], 0, 'IDE_CDROM')
                incremental.wait_task(hc, tag, 'create CD-ROM', 300)
                cdrom = {'uuid': cd_uuid}
            tag = hc.set_cdrom(cdrom['uuid'], iso_uuid)
            if tag:
                incremental.wait_task(hc, tag, 'insert ISO', 300)
        except Exception:
            try:
                hc.delete_iso(iso_uuid)
            except HyperCoreError:
                pass
            raise
        db.update_restore(restore_id, status='COMPLETE', finished_at=incremental._now(), created_uuid=iso_uuid,
                          message=f"ISO '{name}' ({size >> 20} MiB, {len(params['paths'])} item(s)) inserted into the CD-ROM of '{vm.get('name')}'. "
                                  f"Copy the files inside the guest, then eject.")
        log.info('[restore %s] ISO delivered to %s', restore_id, vm.get('name'))
    except (IncrementalError, HyperCoreError, ProxyError, KeyError) as e:
        db.update_restore(restore_id, status='ERROR', message=str(e), finished_at=incremental._now())
        log.error('[restore %s] ISO delivery failed: %s', restore_id, e)
    except Exception as e:                                  # noqa: BLE001
        db.update_restore(restore_id, status='ERROR', message=f'Unexpected error: {e}', finished_at=incremental._now())
        log.exception('[restore %s] ISO delivery error', restore_id)


def eject_iso(restore_id: int) -> str:
    """Eject the delivered ISO from the VM's CD-ROM and delete it from the cluster."""
    import jobs
    r = db.get_restore(restore_id)
    if not r or (r.get('params') or {}).get('kind') != 'files' or not r.get('created_uuid'):
        raise IncrementalError('no delivered ISO on this entry')
    hc = jobs.client_for(r['cluster_id'])
    iso_uuid = r['created_uuid']
    try:
        vm = hc.get_vm(r['params']['vm_uuid'])
        for b in vm.get('blockDevs', []):
            if b.get('type') == 'IDE_CDROM' and iso_uuid in (b.get('path') or ''):
                tag = hc.set_cdrom(b['uuid'], None)
                if tag:
                    incremental.wait_task(hc, tag, 'eject', 300)
    except HyperCoreError as e:
        log.warning('eject: VM lookup failed (%s); deleting the ISO anyway', e)
    tag = hc.delete_iso(iso_uuid)
    if tag:
        incremental.wait_task(hc, tag, 'delete ISO', 300)
    db.update_restore(restore_id, message=(r.get('message') or '') + ' Ejected and removed.', created_uuid=None)
    return iso_uuid
