"""Minimal HyperCore REST API client for exports, imports and the incremental calls.

One login session per (cluster, account) is shared by every client instance in the process
(see _Session). The session rides on the shared ClusterSession, so it re-logs in after a 401
and moves to another node of the cluster when the current one stops answering. Credentials
come from the cluster registry.
"""
from __future__ import annotations

import atexit
import threading
import time

import requests
import urllib3

import cluster_registry
import hypercore_session


class HyperCoreError(Exception):
    """An API call failed. `status` is the HTTP status when the cluster answered (0 otherwise)."""
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = int(status or 0)

    @property
    def transient(self) -> bool:
        """A 500 from HyperCore usually means another task holds the VM (one task per object,
        one per node); the call is worth repeating after a pause."""
        return self.status == 500 or 'impeding' in str(self).lower() or 'timed out waiting' in str(self).lower()


def first_tag(tags: str) -> str:
    """The first tag of HyperCore's comma-separated tag string ('' if none)."""
    return (tags or '').split(',')[0].strip()


def _vm_sort_key(vm: dict):
    tag = first_tag(vm.get('tags', '')).lower()
    name = (vm.get('name') or '').lower()
    # tag == "" -> True sorts after False, so untagged VMs land last
    return (tag == '', tag, name)


# ------------------------------------------------------------------ sessions
# One HyperCore login session per (cluster, account), shared by every client instance in this
# process. HyperCore opens and closes a session for each basic-auth request and counts the logins
# against an hourly limit, so the service logs in once, reuses the cookie until the cluster says
# 401, and renews it on its own every SESSION_MAX_AGE seconds (a logout followed by a login) to
# keep the session list on the cluster clean. Everything is logged out at process exit.
SESSION_MAX_AGE = 6 * 3600


class _Session:
    """One ClusterSession plus the age-based renewal. With a cluster_id the session fails over
    across the cluster's nodes; with a bare host it is pinned to that address."""
    def __init__(self, host: str, username: str, password: str, verify: bool, cluster_id: str | None = None):
        self.host, self.username, self.password, self.verify = host, username, password, verify
        self.hc = hypercore_session.ClusterSession(
            cluster_id, username, password, verify, service='backup', name=cluster_id or host,
            host=None if cluster_id else host, seed=host, login_timeout=30,
            http_factory=lambda: requests.Session(), base_url_of=lambda h: cluster_registry.base_url(h))
        self.http = self.hc.session
        self.lock = self.hc.lock

    @property
    def base(self) -> str:              # REST base of the node in use (follows a switch)
        return self.hc.base_url

    @property
    def logged_in_at(self) -> float:
        return self.hc.logged_in_at

    @logged_in_at.setter
    def logged_in_at(self, value: float) -> None:
        self.hc.logged_in_at = value

    def login(self) -> None:
        try:
            self.hc.login()
        except hypercore_session.AuthFailed as e:
            raise HyperCoreError('Authentication failed (401): check the cluster credentials') from e
        except hypercore_session.ClusterUnreachable as e:
            raise HyperCoreError(f'Cannot reach the cluster: {e}') from e
        except requests.exceptions.RequestException as e:
            raise HyperCoreError(f'Cannot reach {self.base}: {e}') from e

    def logout(self) -> None:
        self.hc.logout()

    def ensure(self) -> None:
        """Logged in, and not older than SESSION_MAX_AGE."""
        with self.lock:
            if self.logged_in_at and time.monotonic() - self.logged_in_at > SESSION_MAX_AGE:
                self.logout()
            if not self.logged_in_at:
                self.login()


_SESSIONS: dict[tuple[str, str], _Session] = {}
_SESSIONS_LOCK = threading.Lock()


def session_for(host: str, username: str, password: str, verify: bool, cluster_id: str | None = None) -> _Session:
    key = (cluster_id or host, username)
    with _SESSIONS_LOCK:
        sess = _SESSIONS.get(key)
        if sess and (sess.password != password or sess.verify != verify or sess.host != host):   # changed: start over
            sess.logout()
            sess = None
        if sess is None:
            sess = _SESSIONS[key] = _Session(host, username, password, verify, cluster_id)
    return sess


def logout_all() -> None:
    with _SESSIONS_LOCK:
        for sess in _SESSIONS.values():
            sess.logout()
        _SESSIONS.clear()


atexit.register(logout_all)


class HyperCoreClient:
    def __init__(self, host: str, username: str, password: str, verify_tls: bool = True, timeout: int = 30,
                 cluster_id: str | None = None):
        self.verify = verify_tls
        self.timeout = timeout
        self.session = session_for(host, username, password, verify_tls, cluster_id)
        if not verify_tls:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    @property
    def base(self) -> str:
        """REST base of the node in use; read it at call time, it follows a failover."""
        return self.session.base

    def http(self) -> requests.Session:
        """The logged-in requests session, for calls that stream a body (no retry possible)."""
        self.session.ensure()
        return self.session.http

    def _request(self, method: str, path: str, **kwargs):
        timeout = kwargs.pop('timeout', self.timeout)
        try:
            self.session.ensure()                             # login, or renewal when the session is old
            resp = self.session.hc.request(method, path, timeout=timeout, **kwargs)
        except HyperCoreError:
            raise
        except hypercore_session.AuthFailed as e:
            raise HyperCoreError('Authentication failed (401): check the cluster credentials') from e
        except hypercore_session.ClusterUnreachable as e:
            raise HyperCoreError(f'Cannot reach the cluster: {e}') from e
        except requests.exceptions.SSLError as e:
            raise HyperCoreError(f'TLS error talking to {self.base}{path}: {e}') from e
        except requests.exceptions.ConnectionError as e:
            raise HyperCoreError(f'Cannot reach {self.base}{path}: {e}') from e
        except requests.exceptions.Timeout as e:
            raise HyperCoreError(f'Timeout talking to {self.base}{path}') from e
        if resp.status_code == 401:
            raise HyperCoreError('Authentication failed (401): check the cluster credentials', 401)
        if resp.status_code == 403:
            raise HyperCoreError('Permission denied (403): the cluster account lacks rights for this action', 403)
        if not resp.ok:
            try:
                detail = resp.json().get('error', '')
            except Exception:
                detail = resp.text[:200]
            raise HyperCoreError(f'HyperCore API {resp.status_code} on {path}: {detail}', resp.status_code)
        return resp.json() if resp.text else None

    def update_status(self) -> dict:
        """The cluster's self-update state, from /update/update_status.json (outside /rest). While an
        update runs the REST API is read-only cluster-wide. Returns {} when the file is unavailable."""
        url = self.base.rsplit('/rest/', 1)[0] + '/update/update_status.json'
        try:
            resp = self.http().get(url, timeout=self.timeout)
        except requests.exceptions.RequestException as e:
            raise HyperCoreError(f'Cannot read {url}: {e}') from e
        if not resp.ok or not resp.text:
            return {}
        try:
            return resp.json() or {}
        except ValueError:
            return {}

    def list_nodes(self) -> list[dict]:
        return self._request('GET', '/Node') or []

    def ping(self) -> bool:
        self._request('GET', '/ping')
        return True

    def list_vms(self) -> list[dict]:
        vms = self._request('GET', '/VirDomain') or []
        slim = [{
            'uuid': vm.get('uuid'), 'name': vm.get('name'), 'description': vm.get('description', ''),
            'state': vm.get('state', ''), 'mem': vm.get('mem', 0), 'numVCPU': vm.get('numVCPU', 0),
            'tags': vm.get('tags', ''),
        } for vm in vms]
        return sorted(slim, key=_vm_sort_key)

    def export_vm(self, vm_uuid: str, path_uri: str, compress: bool = False) -> str:
        """Start a VM export. HyperCore creates the basename directory of path_uri on the
        target, which is how each run gets its own timestamped folder. Returns the taskTag."""
        body = {'target': {'pathURI': path_uri}}
        if compress:
            body['target']['compress'] = True
        result = self._request('POST', f'/VirDomain/{vm_uuid}/export', json=body)
        task_tag = (result or {}).get('taskTag')
        if not task_tag:
            raise HyperCoreError(f'Export accepted but no taskTag returned: {result}')
        return task_tag

    def import_vm(self, source_uri: str, name: str | None = None, description: str | None = None,
                  tags: str | None = None, definition_file: str | None = None) -> tuple[str, str]:
        """Import a VM from a previously exported folder. Returns (taskTag, createdUUID)."""
        body: dict = {'source': {'pathURI': source_uri}}
        if definition_file:
            body['source']['definitionFileName'] = definition_file
        template = {}
        if name:
            template['name'] = name
        if description is not None:
            template['description'] = description
        if tags is not None:
            template['tags'] = tags
        if template:
            body['template'] = template
        result = self._request('POST', '/VirDomain/import', json=body) or {}
        task_tag = result.get('taskTag')
        if not task_tag:
            raise HyperCoreError(f'Import accepted but no taskTag returned: {result}')
        return task_tag, result.get('createdUUID', '')

    def task_status(self, task_tag: str) -> dict:
        """{'state': QUEUED|RUNNING|COMPLETE|ERROR|UNINITIALIZED, 'progressPercent': int, ...}"""
        data = self._request('GET', f'/TaskTag/{task_tag}') or []
        if isinstance(data, list) and data:
            return data[0]
        return {'state': 'UNINITIALIZED', 'progressPercent': 0}


# ----------------------------------------------------------------- incremental backups
# The calls below were verified on HyperCore 9.8.4 (see PLAN-cbt-incrementals.md, E1–E9).

class HyperCoreIncrementalMixin:
    """Snapshot, clone, changed-block and VM-definition calls used by the proxy path."""

    DISK_TYPES = ('VIRTIO_DISK', 'IDE_DISK', 'SCSI_DISK')
    UTILITY_TYPES = ('NVRAM', 'VTPM')

    def get_vm(self, vm_uuid: str) -> dict:
        data = self._request('GET', f'/VirDomain/{vm_uuid}') or []
        if not data:
            raise HyperCoreError(f'VM {vm_uuid} not found on the cluster')
        return data[0] if isinstance(data, list) else data

    def list_vms_full(self) -> list[dict]:
        return self._request('GET', '/VirDomain') or []

    def definition_of(self, vm: dict) -> dict:
        """The subset of a VirDomain record a restore needs (stored with every point)."""
        return {
            'uuid': vm.get('uuid'), 'name': vm.get('name'), 'description': vm.get('description', ''),
            'mem': vm.get('mem'), 'numVCPU': vm.get('numVCPU'), 'machineType': vm.get('machineType'),
            'operatingSystem': vm.get('operatingSystem'), 'cpuType': vm.get('cpuType'), 'tags': vm.get('tags', ''),
            'bootDevices': vm.get('bootDevices', []), 'useUEFISecureBoot': vm.get('useUEFISecureBoot'),
            'blockDevs': [{k: b.get(k) for k in ('uuid', 'type', 'capacity', 'slot', 'cacheMode', 'name', 'tieringPriorityFactor')} for b in vm.get('blockDevs', [])],
            'netDevs': [{k: n.get(k) for k in ('uuid', 'type', 'macAddress', 'vlan', 'connected')} for n in vm.get('netDevs', [])],
        }

    def backup_devices(self, vm: dict) -> list[dict]:
        """Disks plus NVRAM and vTPM; CD-ROMs are not backed up."""
        return [b for b in vm.get('blockDevs', []) if b.get('type') in self.DISK_TYPES + self.UTILITY_TYPES]

    # -- snapshots
    def create_snapshot(self, vm_uuid: str, label: str, quiesce: bool = True, retain_days: float | None = None) -> tuple[str, str]:
        """A USER snapshot that is never replicated. With retain_days the cluster removes it itself
        after that many days (localRetainUntilTimestamp; it cannot be extended later, the resource
        has no PATCH), so an anchor left behind by a vanished installation does not squat forever."""
        body = {'domainUUID': vm_uuid, 'label': label, 'attemptQuiesce': bool(quiesce), 'replication': False, 'type': 'USER'}
        if retain_days:
            body['localRetainUntilTimestamp'] = int(time.time() + float(retain_days) * 86400)
        r = self._request('POST', '/VirDomainSnapshot', json=body) or {}
        if not r.get('taskTag'):
            raise HyperCoreError(f'snapshot not accepted: {r}')
        return r['taskTag'], r.get('createdUUID', '')

    def get_snapshot(self, snap_uuid: str) -> dict | None:
        data = self._request('GET', f'/VirDomainSnapshot/{snap_uuid}') or []
        return data[0] if data else None

    def list_snapshots(self, vm_uuid: str | None = None) -> list[dict]:
        snaps = self._request('GET', '/VirDomainSnapshot') or []
        return [s for s in snaps if vm_uuid is None or s.get('domainUUID') == vm_uuid]

    def delete_snapshot(self, snap_uuid: str) -> str:
        r = self._request('DELETE', f'/VirDomainSnapshot/{snap_uuid}') or {}
        return r.get('taskTag', '')

    @staticmethod
    def snapshot_serials(snapshot: dict) -> dict:
        """device uuid -> serial number, from a snapshot's deviceSnapshots."""
        return {d['uuid']: d['serialNumber'] for d in snapshot.get('deviceSnapshots', []) if d.get('uuid')}

    # -- clones onto the proxy VM
    def clone_device(self, device_uuid: str, snap_uuid: str, target_vm_uuid: str, capacity: int,
                     read_only: bool = True, dev_type: str = 'VIRTIO_DISK', tiering: int | None = None) -> tuple[str, str]:
        """Attach a snapshot's block device to target_vm as a new (thin) device. Returns (taskTag, createdUUID).
        Backup clones onto the proxy are always VIRTIO_DISK (an IDE disk cannot be hot-added and breaks
        snapshotting of the VM it lands on); a restore passes the disk's original type, the VM is off then."""
        template = {'virDomainUUID': target_vm_uuid, 'type': dev_type, 'capacity': int(capacity)}
        if tiering is not None:
            template['tieringPriorityFactor'] = int(tiering)
        body = {'snapUUID': snap_uuid, 'template': template,
                'options': {'readOnly': bool(read_only), 'regenerateDiskID': False}}
        r = self._request('POST', f'/VirDomainBlockDevice/{device_uuid}/clone', json=body) or {}
        if not r.get('taskTag'):
            raise HyperCoreError(f'clone not accepted: {r}')
        return r['taskTag'], r.get('createdUUID', '')

    def create_block_device(self, vm_uuid: str, capacity: int, dev_type: str = 'VIRTIO_DISK', tiering: int | None = None) -> tuple[str, str]:
        body = {'virDomainUUID': vm_uuid, 'type': dev_type, 'capacity': int(capacity)}
        if tiering is not None:
            body['tieringPriorityFactor'] = int(tiering)      # 0 for scratch disks: keep heavy one-off writes off the SSD tier
        r = self._request('POST', '/VirDomainBlockDevice', json=body) or {}
        if not r.get('taskTag'):
            raise HyperCoreError(f'block device not accepted: {r}')
        return r['taskTag'], r.get('createdUUID', '')

    def get_block_device(self, device_uuid: str) -> dict | None:
        data = self._request('GET', f'/VirDomainBlockDevice/{device_uuid}') or []
        return data[0] if data else None

    def delete_block_device(self, device_uuid: str) -> str:
        r = self._request('DELETE', f'/VirDomainBlockDevice/{device_uuid}') or {}
        return r.get('taskTag', '')

    def patch_block_device(self, device_uuid: str, **fields) -> str:
        """PATCH a block device (tieringPriorityFactor, name, ...). A clone inherits these from its
        source and ignores them in the clone template (verified on 9.8.4), so a restore sets them here."""
        r = self._request('PATCH', f'/VirDomainBlockDevice/{device_uuid}', json=fields) or {}
        return r.get('taskTag', '')

    def snap_diff(self, device_uuid: str, serial_from: int, serial_to: int) -> list[dict]:
        """Changed 1 MiB blocks between two serials (inclusive intervals); serial_from = 0 is the allocation map."""
        return self._request('POST', f'/VirDomainBlockDevice/{device_uuid}/snapDiff',
                             json={'snapshotSerialFrom': int(serial_from), 'snapshotSerialTo': int(serial_to)}) or []

    # -- VM control used by restores
    def vm_action(self, vm_uuid: str, action: str) -> str:
        r = self._request('POST', '/VirDomain/action', json=[{'virDomainUUID': vm_uuid, 'actionType': action}]) or []
        item = r[0] if isinstance(r, list) and r else r
        return (item or {}).get('taskTag', '')

    def patch_vm(self, vm_uuid: str, **fields) -> str:
        r = self._request('PATCH', f'/VirDomain/{vm_uuid}', json=fields) or {}
        return r.get('taskTag', '')

    def patch_netdev(self, netdev_uuid: str, **fields) -> str:
        r = self._request('PATCH', f'/VirDomainNetDevice/{netdev_uuid}', json=fields) or {}
        return r.get('taskTag', '')

    def delete_vm(self, vm_uuid: str) -> str:
        r = self._request('DELETE', f'/VirDomain/{vm_uuid}') or {}
        return r.get('taskTag', '')

    # -- ISO images and CD-ROM drives (file delivery into a guest through its own front door)
    def create_iso(self, name: str, size: int) -> str:
        r = self._request('POST', '/ISO', json={'name': name, 'size': int(size), 'readyForInsert': False}) or {}
        if not r.get('createdUUID'):
            raise HyperCoreError(f'ISO not created: {r}')
        return r['createdUUID']

    def upload_iso(self, iso_uuid: str, stream, size: int) -> None:
        """PUT the image data; `stream` yields bytes and must total exactly `size`. The body is sent
        with a plain Content-Length (HyperCore's nginx rejects chunked uploads), so the iterable is
        wrapped in a file-like reader that knows its length."""
        url = f'{self.base}/ISO/{iso_uuid}/data'
        try:
            resp = self.http().put(url, data=LengthStream(stream, size), timeout=(30, 3600),
                                   headers={'Content-Type': 'application/octet-stream'})
        except requests.exceptions.RequestException as e:
            raise HyperCoreError(f'ISO upload failed: {e}') from e
        if not resp.ok:
            raise HyperCoreError(f'ISO upload failed ({resp.status_code}): {resp.text[:200]}')

    def ready_iso(self, iso_uuid: str) -> str:
        r = self._request('PATCH', f'/ISO/{iso_uuid}', json={'readyForInsert': True}) or {}
        return r.get('taskTag', '')

    def delete_iso(self, iso_uuid: str) -> str:
        r = self._request('DELETE', f'/ISO/{iso_uuid}') or {}
        return r.get('taskTag', '')

    def list_isos(self) -> list[dict]:
        return self._request('GET', '/ISO') or []

    def set_cdrom(self, cdrom_uuid: str, iso_uuid: str | None) -> str:
        """Insert an ISO into a CD-ROM device (iso_uuid None/'' ejects)."""
        r = self._request('PATCH', f'/VirDomainBlockDevice/{cdrom_uuid}', json={'path': f'scribe/{iso_uuid}' if iso_uuid else ''}) or {}
        return r.get('taskTag', '')


class LengthStream:
    """File-like view over an iterable of bytes with a known total length, so HTTP clients send
    Content-Length instead of chunked encoding."""

    def __init__(self, iterable, size: int):
        self._it = iter(iterable)
        self._buf = b''
        self.len = int(size)
        self._sent = 0

    def __len__(self):
        return self.len

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            chunks = [self._buf] + list(self._it)
            self._buf = b''
            data = b''.join(chunks)
            self._sent += len(data)
            return data
        while len(self._buf) < n:
            try:
                self._buf += next(self._it)
            except StopIteration:
                break
        data, self._buf = self._buf[:n], self._buf[n:]
        self._sent += len(data)
        return data


class HyperCoreClient(HyperCoreIncrementalMixin, HyperCoreClient):   # type: ignore[no-redef]
    """The export/import client plus the incremental-backup calls."""
