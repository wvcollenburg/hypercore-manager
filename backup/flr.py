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
