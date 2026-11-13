"""
hc.py — the console service's HyperCore access: one login session per cluster, reused for every
console and screenshot (logins count against HyperCore's hourly limit).

A console is HyperCore's own websocket on the node the session was issued by:
    wss://<node in use>/node/<VM's nodeUUID>/console?sessionID=<session>&vm=<vm uuid>
One session reaches VMs on every node (HyperCore forwards internally), but the path has to name the
node the VM runs on right now, so it is looked up just before each connect. That REST call also
renews the session after a 401, so the session in the URL is always a live one.

The session ID never leaves this service: the browser talks to the dashboard, the dashboard to
this service, and only this service talks to the cluster.
"""
from __future__ import annotations

import ssl
import threading
import urllib.parse
from dataclasses import dataclass
from typing import Optional

import requests

import cluster_registry
import hypercore_session


class ConsoleError(Exception):
    """A console cannot be opened; the message is fit for the user."""
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


@dataclass
class Target:
    url: str
    ssl: object            # False (no verification) or an ssl.SSLContext
    vm_name: str
    node_uuid: str


_lock = threading.Lock()
_sessions: dict[str, tuple[object, hypercore_session.ClusterSession]] = {}


def session_for(cluster_id: str) -> hypercore_session.ClusterSession:
    """The cluster's session, rebuilt when the registration changed (new credentials or address)."""
    c = cluster_registry.get(cluster_id)
    if c is None or not c.enabled:
        raise ConsoleError('cluster is not registered or disabled', 404)
    with _lock:
        hit = _sessions.get(cluster_id)
        if hit and hit[0] == c.updated_at:
            return hit[1]
        if hit:
            try:
                hit[1].close()
            except Exception:  # noqa: BLE001
                pass
        username, password = cluster_registry.credentials(cluster_id)
        s = hypercore_session.ClusterSession(cluster_id, username, password, bool(c.verify_tls),
                                             service='console', name=c.name, seed=c.host)
        _sessions[cluster_id] = (c.updated_at, s)
        return s


def close_all() -> None:
    with _lock:
        for _, s in _sessions.values():
            try:
                s.close()
            except Exception:  # noqa: BLE001
                pass
        _sessions.clear()


def _get(cluster_id: str, path: str):
    s = session_for(cluster_id)
    try:
        r = s.get(path)
    except hypercore_session.AuthFailed:
        raise ConsoleError('the cluster refused HCM\'s credentials', 502) from None
    except (hypercore_session.ClusterUnreachable, requests.exceptions.RequestException) as e:
        raise ConsoleError(f'cluster unreachable: {e}', 502) from None
    if r.status_code == 404:
        return None
    if r.status_code >= 400:
        raise ConsoleError(f'cluster answered HTTP {r.status_code}', 502)
    return r.json()


def get_vm(cluster_id: str, vm_uuid: str) -> Optional[dict]:
    data = _get(cluster_id, f'/VirDomain/{urllib.parse.quote(vm_uuid)}')
    if isinstance(data, list):
        data = data[0] if data else None
    return data or None


def list_vms(cluster_id: str) -> list[dict]:
    return _get(cluster_id, '/VirDomain') or []


def _ssl_for(verify: bool):
    return ssl.create_default_context() if verify else False


def console_target(cluster_id: str, vm_uuid: str) -> Target:
    """Where to connect for this VM's console right now. Raises ConsoleError when it cannot be
    opened (VM gone, not running, cluster down)."""
    vm = get_vm(cluster_id, vm_uuid)
    if not vm:
        raise ConsoleError('VM not found on the cluster', 404)
    if str(vm.get('state', '')).upper() != 'RUNNING':
        raise ConsoleError(f"VM is not running (state {vm.get('state', 'unknown')})")
    node = vm.get('nodeUUID') or ''
    if not node:
        raise ConsoleError('VM has no node assigned (starting or migrating); try again in a moment')
    s = session_for(cluster_id)
    sid = s.session.cookies.get('sessionID') or ''
    if not sid or not s.host:
        raise ConsoleError('no HyperCore session', 502)
    host = cluster_registry.normalize_host(s.host)
    q = urllib.parse.urlencode({'sessionID': sid, 'vm': vm_uuid})
    return Target(url=f'wss://{host}/node/{urllib.parse.quote(node)}/console?{q}', ssl=_ssl_for(s.verify),
                  vm_name=vm.get('name') or vm_uuid, node_uuid=node)


# ------------------------------------------------------------- VM details
_HIDDEN_DEV = ('path', 'shareUUID', 'labels', 'mountPoints', 'virDomainUUID')


def vm_details(cluster_id: str, vm_uuid: str) -> dict:
    """Everything HyperCore says about one VM, for the details window. Left out: cloud-init data
    (often holds passwords or keys), the raw VNC address, storage paths and task session IDs.
    Node UUIDs get their LAN IPs, the snapshot schedule its name."""
    vm = get_vm(cluster_id, vm_uuid)
    if not vm:
        raise ConsoleError('VM not found on the cluster', 404)
    nodes = {n.get('uuid'): n for n in (_get(cluster_id, '/Node') or [])}
    node_ip = lambda u: (nodes.get(u) or {}).get('lanIP') or ''  # noqa: E731
    schedule = None
    if vm.get('snapshotScheduleUUID'):
        try:
            sch = _get(cluster_id, f"/VirDomainSnapshotSchedule/{urllib.parse.quote(vm['snapshotScheduleUUID'])}")
            sch = sch[0] if isinstance(sch, list) and sch else sch
            schedule = (sch or {}).get('name') or vm['snapshotScheduleUUID']
        except ConsoleError:
            schedule = vm['snapshotScheduleUUID']
    devs = {d.get('uuid'): d for d in vm.get('blockDevs') or []}
    aff = vm.get('affinityStrategy') or {}
    return {
        'uuid': vm.get('uuid'), 'name': vm.get('name'), 'description': vm.get('description') or '',
        'state': vm.get('state'), 'node_ip': node_ip(vm.get('nodeUUID')),
        'operating_system': vm.get('operatingSystem'), 'machine_type': vm.get('machineType'),
        'firmware': vm.get('machineTypeKeyword'), 'secure_boot': vm.get('useUEFISecureBoot'),
        'cpu_type': vm.get('cpuType'), 'vcpus': vm.get('numVCPU'), 'mem_bytes': vm.get('mem'),
        'guest_agent': vm.get('guestAgentState'), 'keymap': (vm.get('console') or {}).get('keymap'),
        'tags': [t for t in (vm.get('tags') or '').split(',') if t],
        'affinity': {'strict': aff.get('strictAffinity'), 'preferred_ip': node_ip(aff.get('preferredNodeUUID')),
                     'backup_ip': node_ip(aff.get('backupNodeUUID'))},
        'disks': [{k: v for k, v in d.items() if k not in _HIDDEN_DEV} for d in vm.get('blockDevs') or []],
        'nics': [{'type': n.get('type'), 'mac': n.get('macAddress'), 'vlan': n.get('vlan'), 'connected': n.get('connected'),
                  'ipv4': n.get('ipv4Addresses') or []} for n in vm.get('netDevs') or []],
        'boot_order': [(devs.get(u) or {}).get('name') or (devs.get(u) or {}).get('type') or u for u in vm.get('bootDevices') or []],
        'snapshots': len(vm.get('snapUUIDs') or []), 'snapshot_schedule': schedule,
        'replications': len(vm.get('replicationUUIDs') or []),
        'cloned_from': vm.get('sourceVirDomainUUID') or None,
    }


# ------------------------------------------------------------- search index
_index_cache: dict[str, tuple[float, list[dict]]] = {}
INDEX_TTL = 20


def vm_index(cluster_id: str, fresh: bool = False) -> list[dict]:
    """Searchable facts of every VM: name, all tags, description, guest IPs, MACs, VLANs, mounted ISO
    names, disk names, OS, machine type and the node's LAN IP. One GET /VirDomain (plus /ISO and /Node)
    per INDEX_TTL seconds per cluster, over the service's one session; `fresh` skips the cache (after an
    edit made in HCM, or the Refresh button)."""
    import time as _t
    hit = _index_cache.get(cluster_id)
    if hit and not fresh and _t.monotonic() - hit[0] < INDEX_TTL:
        return hit[1]
    vms = _get(cluster_id, '/VirDomain') or []
    isos = {i.get('uuid'): i.get('name') for i in (_get(cluster_id, '/ISO') or [])}
    nodes = {n.get('uuid'): n.get('lanIP') for n in (_get(cluster_id, '/Node') or [])}
    out = []
    for v in vms:
        devs = v.get('blockDevs') or []
        mounted = []
        for d in devs:
            if d.get('type') == 'IDE_CDROM' and d.get('path'):
                u = d['path'].rsplit('/', 1)[-1]
                mounted.append(isos.get(u) or d.get('name') or u)
        nics = v.get('netDevs') or []
        out.append({'uuid': v.get('uuid'), 'name': v.get('name') or '', 'state': v.get('state') or '',
                    'tags': [t for t in (v.get('tags') or '').split(',') if t],
                    'description': v.get('description') or '',
                    'ips': [ip for n in nics for ip in (n.get('ipv4Addresses') or [])],
                    'macs': [n.get('macAddress') for n in nics if n.get('macAddress')],
                    'vlans': sorted({str(n.get('vlan')) for n in nics if n.get('vlan') is not None}),
                    'isos': mounted, 'disks': [d.get('name') for d in devs if d.get('name') and d.get('type') != 'IDE_CDROM'],
                    'os': v.get('operatingSystem') or '', 'machine': v.get('machineType') or '',
                    'node': nodes.get(v.get('nodeUUID')) or ''})
    _index_cache[cluster_id] = (_t.monotonic(), out)
    return out
