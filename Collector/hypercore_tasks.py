"""
hypercore_tasks.py — HyperCore REST v2 task feed (GET /rest/v2/TaskTag), shared by the collector
(which records tasks), the balancer (which refuses to move a VM that is busy) and the dashboard.

v2 is experimental on HyperCore 9.8; it only adds to v1. A task record looks like
  {taskTag, state, progressPercent, formattedDescription: "Move Virtual Machine %@ from %@ to %@",
   descriptionParameters: ["vm", "172.16.0.241", "172.16.0.244"], objectUUID, nodeUUIDs,
   created, modified, completed}
Everything here is pure except fetch_tasks(), which takes a logged-in requests.Session.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Optional

ACTIVE_STATES = ('QUEUED', 'RUNNING')
# first words of formattedDescription -> kind
_KINDS = (
    ('Move Virtual Machine', 'move'),
    ('Export Virtual Machine', 'export'),
    ('Import Virtual Machine', 'import'),
    ('Create Snapshot', 'snapshot'),
    ('Delete Snapshot', 'snapshot_delete'),
    ('Clone Virtual Machine', 'clone'),
    ('Create clone of block device', 'disk'),      # a block-device clone: the VM is the last parameter
    ('Start Virtual Machine', 'start'),
    ('Shut Off Virtual Machine', 'stop'),
    ('Shutdown Virtual Machine', 'stop'),
    ('Power Off Virtual Machine', 'stop'),
    ('Reset Virtual Machine', 'stop'),
    ('Create Virtual Machine', 'create'),
    ('Create Replication Virtual Machine', 'replication'),
    ('Update Domain for Replication', 'replication'),
    ('Delete Virtual Machine', 'delete'),
    ('Update Virtual Machine', 'update'),
    ('Delete block device', 'disk'),
    ('Create block device', 'disk'),
    ('Update block device', 'disk'),
)
# kinds that touch a VM's disks or its placement: a migration must wait for them
BUSY_KINDS = frozenset({'move', 'export', 'import', 'snapshot', 'snapshot_delete', 'clone', 'replication', 'disk'})
# where the VM name sits in descriptionParameters, per kind (None = unknown)
_VM_PARAM = {'move': 0, 'export': 0, 'import': 0, 'snapshot': 1, 'snapshot_delete': 1, 'clone': 0, 'start': 0, 'stop': 0,
             'create': 0, 'delete': 0, 'update': 0, 'replication': -1, 'disk': -1}


def v2_base(v1_base: str) -> str:
    """https://host/rest/v1 -> https://host/rest/v2"""
    return v1_base.rstrip('/').rsplit('/rest/', 1)[0] + '/rest/v2'


def kind_of(task: dict) -> str:
    desc = task.get('formattedDescription') or ''
    for prefix, kind in _KINDS:
        if desc.startswith(prefix):
            return kind
    return 'other'


def describe(task: dict) -> str:
    """formattedDescription with every %@ replaced by its parameter."""
    text = task.get('formattedDescription') or ''
    for p in task.get('descriptionParameters') or []:
        text = text.replace('%@', str(p), 1)
    return text.replace('%@', '?')


def vm_name_of(task: dict, kind: Optional[str] = None) -> Optional[str]:
    kind = kind or kind_of(task)
    idx = _VM_PARAM.get(kind)
    params = task.get('descriptionParameters') or []
    if idx is None or not params:
        return None
    try:
        name = params[idx]
    except IndexError:
        return None
    return str(name) or None


def _ts(value) -> Optional[datetime]:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(v, tz=timezone.utc) if v > 0 else None


def normalise(task: dict) -> dict:
    """The row the collector stores and the dashboard shows."""
    kind = kind_of(task)
    return {
        'task_tag': str(task.get('taskTag') or ''),
        'kind': kind,
        'description': describe(task),
        'vm_name': vm_name_of(task, kind),
        'object_uuid': task.get('objectUUID') or None,
        'state': task.get('state') or 'UNINITIALIZED',
        'progress': int(task.get('progressPercent') or 0),
        'node_uuids': [n for n in (task.get('nodeUUIDs') or []) if n],
        'params': [str(p) for p in (task.get('descriptionParameters') or [])],
        'created_at': _ts(task.get('created')),
        'modified_at': _ts(task.get('modified')),
        'completed_at': _ts(task.get('completed')),
    }


def is_active(task: dict) -> bool:
    return (task.get('state') or '') in ACTIVE_STATES


def busy_reason(tasks: Iterable[dict], vm_uuid: Optional[str] = None, vm_name: Optional[str] = None) -> Optional[str]:
    """Why a migration should wait: an active move anywhere on the cluster, or an active task of a
    busy kind on this VM. None when the coast is clear."""
    for t in tasks:
        if not is_active(t):
            continue
        kind = kind_of(t)
        if kind == 'move':
            return f'a migration is in progress ({describe(t)})'
        if kind in BUSY_KINDS and ((vm_uuid and t.get('objectUUID') == vm_uuid) or (vm_name and vm_name_of(t, kind) == vm_name)):
            return f'the VM is busy ({describe(t)})'
    return None


def move_event(task: dict) -> Optional[dict]:
    """A finished Move task as a migration-log event, or None for anything else."""
    if kind_of(task) != 'move' or (task.get('state') or '') != 'COMPLETE':
        return None
    params = [str(p) for p in (task.get('descriptionParameters') or [])] + ['', '', '']
    return {'task_tag': str(task.get('taskTag') or ''), 'vm_name': params[0], 'source_ip': params[1], 'target_ip': params[2],
            'vm_uuid': task.get('objectUUID') or '', 'time': _ts(task.get('completed')) or _ts(task.get('modified'))}


def dedupe_external(events: list[dict], window_seconds: int = 180) -> list[dict]:
    """Migration-log events from the balancer and from the v2 feed describe the same move twice:
    drop an 'external' event when one of our own engines logged the same VM within the window."""
    from datetime import datetime as _dt

    def when(e):
        try:
            return _dt.fromisoformat(str(e.get('time')).replace('Z', '+00:00'))
        except (TypeError, ValueError):
            return None
    ours = [(e.get('vm_name'), when(e)) for e in events if e.get('engine') != 'external']
    out = []
    for e in events:
        if e.get('engine') == 'external':
            t = when(e)
            if t and any(n == e.get('vm_name') and o and abs((o - t).total_seconds()) <= window_seconds for n, o in ours):
                continue
        out.append(e)
    return out


def fetch_tasks(session, v1_base: str, *, active_only: bool = False, limit: int = 200, timeout: float = 15) -> list[dict]:
    """GET /rest/v2/TaskTag through an already logged-in session. Raises on HTTP errors
    (a 404 means the cluster has no v2)."""
    params: dict[str, Any] = {'limitSize': limit, 'descendingOrder': 'true'}
    if active_only:
        params['isActive'] = 'true'
    r = session.get(f'{v2_base(v1_base)}/TaskTag', params=params, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def probe_v2(session, v1_base: str, timeout: float = 10) -> bool:
    """True when the cluster answers on /rest/v2/TaskTag."""
    try:
        r = session.get(f'{v2_base(v1_base)}/TaskTag', params={'isActive': 'true', 'limitSize': 1}, timeout=timeout)
        return r.status_code == 200 and isinstance(r.json(), list)
    except Exception:       # noqa: BLE001 — any failure means "not available"
        return False
