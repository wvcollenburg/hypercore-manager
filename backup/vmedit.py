"""
vmedit.py — VM edits and power actions from HCM, now or in a maintenance window.

A change set is every change to one VM, run at one moment. HCM holds it until then (table
backup.vm_changes, migration 043): nothing reaches HyperCore before the set runs, so it can be
cancelled up to its start and an HA restart cannot apply half of it early.

Most changes need the VM off: HyperCore applies them at a cold start, a reboot inside the guest is
not enough. Those sets run as: OS shutdown (ACPI), wait until the VM is off, apply, start again (if it
was running). A guest that has not stopped after the timeout is powered off only when the set says
so (force_off); otherwise the set fails and the VM keeps running unchanged. The few changes HyperCore
applies to a running VM (LIVE below) never cause a shutdown.

Removing a disk or a NIC and deleting the VM need the VM's name typed as confirmation; a snapshot of
the VM is taken before a disk is removed, so its data can still be recovered from that snapshot.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

import psycopg2.extras

import db
import jobs
from hypercore import HyperCoreError
from incremental import IncrementalError, wait_task

log = logging.getLogger('backup.vmedit')

DISK_TYPES = ('VIRTIO_DISK', 'SCSI_DISK', 'IDE_DISK')
ALL_DISK_TYPES = DISK_TYPES + ('IDE_CDROM',)
CACHE_MODES = ('NONE', 'WRITEBACK', 'WRITETHROUGH')
NIC_TYPES = ('VIRTIO', 'INTEL_E1000', 'RTL8139', 'PCNET')
OS_TYPES = ('os_windows_server_2012', 'os_other')
POWER_ACTIONS = ('START', 'SHUTDOWN', 'STOP', 'REBOOT', 'RESET')

# VM fields HyperCore takes on a running VM (metadata) and the ones that need a cold start
VM_LIVE = ('name', 'description', 'tags', 'snapshotScheduleUUID', 'affinityStrategy')
VM_CYCLE = ('operatingSystem', 'mem', 'numVCPU', 'bootDevices')
DISK_LIVE = ('tieringPriorityFactor', 'disableSnapshotting', 'name')
DISK_CYCLE = ('cacheMode',)
NIC_LIVE = ('connected',)
NIC_CYCLE = ('vlan', 'type', 'macAddress')

_MAC = re.compile(r'^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$')
OFF_STATES = ('SHUTOFF', 'CRASHED')
GIB = 1024 ** 3


class EditError(ValueError):
    """A change set that cannot be accepted; the message is fit for the user."""


# ------------------------------------------------------------------ planning
def _dev(vm: dict, uuid: str, kind: str) -> dict:
    devs = vm.get('blockDevs' if kind == 'disk' else 'netDevs') or []
    d = next((x for x in devs if x.get('uuid') == uuid), None)
    if d is None:
        raise EditError(f'{kind} {uuid} does not belong to this VM')
    return d


def _int(v, what: str, lo: int, hi: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise EditError(f'{what} must be a whole number') from None
    if not lo <= n <= hi:
        raise EditError(f'{what} must be between {lo} and {hi}')
    return n


def replication_op(o: dict, replications: list[dict], connections: list[dict]) -> Optional[dict]:
    """A VM replicates to one cluster at most. Through the REST API a replication can be created
    (POST) and its transmissions paused or resumed (PATCH enable); ending it, and with that pointing it
    at another cluster, is not in the API (DELETE answers 405) and stays in HyperCore's own UI.
    `o`: {'target': connection uuid} for a VM without a replication, {'pause': bool} for one with.
    Returns None when nothing changes."""
    names = {c.get('uuid'): (c.get('remoteClusterInfo') or {}).get('clusterName') or c.get('cluster') or c.get('uuid')
             for c in connections}
    cur = replications[0] if replications else None
    if cur is None:
        target = o.get('target') or ''
        if not target:
            return None
        if target not in names:
            raise EditError('that remote cluster connection does not exist')
        return {'op': 'replication', 'action': 'create', 'connection': target, 'text': f'start replicating to {names[target]}'}
    if o.get('target') and o['target'] != cur.get('connectionUUID'):
        raise EditError(f"this VM replicates to {names.get(cur.get('connectionUUID'), 'another cluster')}; ending a replication "
                        'is not available in the HyperCore REST API, end it in HyperCore first')
    if 'pause' not in o:
        return None
    enable = not bool(o['pause'])
    if enable == bool(cur.get('enable')):
        return None
    to = names.get(cur.get('connectionUUID'), 'the remote cluster')
    return {'op': 'replication', 'action': 'enable' if enable else 'disable', 'replication': cur['uuid'],
            'connection': cur.get('connectionUUID'),
            'text': f'resume replication to {to}' if enable else f'pause replication to {to}'}


def plan(kind: str, ops: list[dict], vm: dict, ctx: Optional[dict] = None) -> dict:
    """Validate a change set against the VM as it is now. Returns {'ops', 'cycle', 'destructive',
    'summary'}: ops normalised and in apply order (removals first, the VM's own fields last)."""
    if kind == 'power':
        if len(ops) != 1 or ops[0].get('op') != 'power' or ops[0].get('action') not in POWER_ACTIONS:
            raise EditError('a power change is one action: ' + ', '.join(POWER_ACTIONS))
        a = ops[0]['action']
        return {'ops': [{'op': 'power', 'action': a}], 'cycle': False, 'destructive': False, 'summary': [f'power: {a}']}
    if kind == 'delete':
        return {'ops': [{'op': 'delete_vm'}], 'cycle': True, 'destructive': True, 'summary': ['delete the VM']}
    if kind == 'snapshot':
        o = ops[0] if ops else {}
        label = str(o.get('label') or '').strip()
        if not label:
            raise EditError('the snapshot needs a label')
        days = _int(o.get('retain_days') or 0, 'retention', 0, 3650)
        op = {'op': 'snapshot', 'label': label[:200], 'quiesce': bool(o.get('quiesce')), 'replicate': bool(o.get('replicate')),
              'retain_days': days}
        return {'ops': [op], 'cycle': False, 'destructive': False,
                'summary': [f"snapshot '{op['label']}'" + (f", removed after {days} day{'s' if days != 1 else ''}" if days else '')]}
    if kind == 'clone':
        o = ops[0] if ops else {}
        name = str(o.get('name') or '').strip()
        if not name:
            raise EditError('the clone needs a name')
        snap = str(o.get('snap_uuid') or '') or None
        if snap and snap not in (vm.get('snapUUIDs') or []):
            raise EditError('that snapshot does not belong to this VM')
        op = {'op': 'clone', 'name': name, 'description': str(o.get('description') or ''),
              'tags': ','.join(t.strip() for t in str(o.get('tags') or '').split(',') if t.strip()),
              'snap_uuid': snap, 'start': bool(o.get('start'))}
        return {'ops': [op], 'cycle': False, 'destructive': False,
                'summary': [f"clone to '{name}'" + (' from a snapshot' if snap else ' from its current state') + (', started' if op['start'] else '')]}
    if kind != 'edit':
        raise EditError('unknown kind')
    if not ops:
        raise EditError('no changes')
    ctx = ctx or {}
    out: list[dict] = []
    cycle = destructive = False
    summary: list[str] = []
    name = vm.get('name') or vm.get('uuid')
    for o in ops:
        op = o.get('op')
        if op == 'vm':
            fields = {k: v for k, v in (o.get('fields') or {}).items() if k in VM_LIVE + VM_CYCLE}
            if not fields:
                continue
            if 'mem' in fields:
                fields['mem'] = _int(fields['mem'], 'memory', 128 * 1024 ** 2, 4096 * GIB)
            if 'numVCPU' in fields:
                fields['numVCPU'] = _int(fields['numVCPU'], 'vCPUs', 1, 256)
            if 'operatingSystem' in fields and fields['operatingSystem'] not in OS_TYPES:
                raise EditError('operating system must be one of ' + ', '.join(OS_TYPES))
            if 'name' in fields:
                fields['name'] = str(fields['name']).strip()
                if not fields['name']:
                    raise EditError('the name cannot be empty')
            if 'tags' in fields:
                tags = fields['tags'] if isinstance(fields['tags'], list) else str(fields['tags']).split(',')
                fields['tags'] = ','.join(t.strip() for t in tags if str(t).strip())
            if 'bootDevices' in fields:
                own = {d.get('uuid') for d in vm.get('blockDevs') or []}
                if not isinstance(fields['bootDevices'], list) or any(u not in own for u in fields['bootDevices']):
                    raise EditError('boot order may only name this VM\'s own devices')
            if 'affinityStrategy' in fields:
                a = fields['affinityStrategy'] or {}
                fields['affinityStrategy'] = {'strictAffinity': bool(a.get('strictAffinity')),
                                              'preferredNodeUUID': a.get('preferredNodeUUID') or '',
                                              'backupNodeUUID': a.get('backupNodeUUID') or ''}
            c = any(k in VM_CYCLE for k in fields)
            out.append({'op': 'vm', 'fields': fields, 'cycle': c, '_order': 9})
            cycle |= c
            summary += [_describe_vm_field(k, v) for k, v in fields.items()]
        elif op == 'disk_resize':
            d = _dev(vm, o.get('device'), 'disk')
            cap = _int(o.get('capacity'), 'disk size', 1, 64 * 1024 * GIB)
            if cap <= int(d.get('capacity') or 0):
                raise EditError(f"disk {d.get('name') or d.get('type')} can only grow")
            out.append({'op': 'disk_resize', 'device': d['uuid'], 'capacity': cap, 'cycle': False, '_order': 5})
            summary.append(f"grow disk {d.get('name') or d.get('type')} (slot {d.get('slot')}) to {cap / GIB:g} GB")
        elif op == 'disk_add':
            t = o.get('type') or 'VIRTIO_DISK'
            if t not in ALL_DISK_TYPES:
                raise EditError('disk type must be one of ' + ', '.join(ALL_DISK_TYPES))
            body: dict[str, Any] = {'type': t}
            if t != 'IDE_CDROM':
                body['capacity'] = _int(o.get('capacity'), 'disk size', GIB // 1024, 64 * 1024 * GIB)
            if o.get('cacheMode'):
                if o['cacheMode'] not in CACHE_MODES:
                    raise EditError('cache mode must be one of ' + ', '.join(CACHE_MODES))
                body['cacheMode'] = o['cacheMode']
            if o.get('tieringPriorityFactor') is not None:
                body['tieringPriorityFactor'] = _int(o['tieringPriorityFactor'], 'tiering priority', 0, 11)
            c = t == 'IDE_CDROM'                     # a new CD-ROM drive is new hardware; a new disk hot-plugs
            out.append({'op': 'disk_add', 'body': body, 'cycle': c, '_order': 4})
            cycle |= c
            summary.append(f"add {t}" + (f" of {body['capacity'] / GIB:g} GB" if 'capacity' in body else ''))
        elif op == 'disk_set':
            d = _dev(vm, o.get('device'), 'disk')
            fields = {k: v for k, v in (o.get('fields') or {}).items() if k in DISK_LIVE + DISK_CYCLE}
            if 'cacheMode' in fields and fields['cacheMode'] not in CACHE_MODES:
                raise EditError('cache mode must be one of ' + ', '.join(CACHE_MODES))
            if 'tieringPriorityFactor' in fields:
                fields['tieringPriorityFactor'] = _int(fields['tieringPriorityFactor'], 'tiering priority', 0, 11)
            if 'disableSnapshotting' in fields:
                fields['disableSnapshotting'] = bool(fields['disableSnapshotting'])
            if not fields:
                continue
            c = any(k in DISK_CYCLE for k in fields)
            out.append({'op': 'disk_set', 'device': d['uuid'], 'fields': fields, 'cycle': c, '_order': 5})
            cycle |= c
            summary.append(f"disk {d.get('name') or d.get('type')} (slot {d.get('slot')}): "
                           + ', '.join(f'{k} {v}' for k, v in fields.items()))
        elif op == 'disk_remove':
            d = _dev(vm, o.get('device'), 'disk')
            out.append({'op': 'disk_remove', 'device': d['uuid'], 'cycle': True, '_order': 1,
                        'label': f"{d.get('name') or d.get('type')} (slot {d.get('slot')})"})
            cycle = destructive = True
            summary.append(f"REMOVE disk {d.get('name') or d.get('type')} (slot {d.get('slot')}, "
                           f"{int(d.get('capacity') or 0) / GIB:g} GB)")
        elif op == 'iso':
            d = _dev(vm, o.get('device'), 'disk')
            if d.get('type') != 'IDE_CDROM':
                raise EditError('an ISO goes into a CD-ROM drive')
            iso = o.get('iso_uuid') or None
            out.append({'op': 'iso', 'device': d['uuid'], 'iso_uuid': iso, 'iso_name': o.get('iso_name') or '',
                        'cycle': False, '_order': 6})
            summary.append(f"CD-ROM slot {d.get('slot')}: " + (f"insert {o.get('iso_name') or iso}" if iso else 'eject'))
        elif op == 'nic_set':
            d = _dev(vm, o.get('device'), 'nic')
            fields = {k: v for k, v in (o.get('fields') or {}).items() if k in NIC_LIVE + NIC_CYCLE}
            if 'vlan' in fields:
                fields['vlan'] = _int(fields['vlan'], 'VLAN', 0, 4094)
            if 'type' in fields and fields['type'] not in NIC_TYPES:
                raise EditError('NIC type must be one of ' + ', '.join(NIC_TYPES))
            if 'macAddress' in fields and not _MAC.match(str(fields['macAddress'])):
                raise EditError('MAC address must look like 7C:4C:58:00:00:01')
            if 'connected' in fields:
                fields['connected'] = bool(fields['connected'])
            if not fields:
                continue
            c = any(k in NIC_CYCLE for k in fields)
            out.append({'op': 'nic_set', 'device': d['uuid'], 'fields': fields, 'cycle': c, '_order': 3})
            cycle |= c
            summary.append(f"NIC {d.get('macAddress')}: " + ', '.join(f'{k} {v}' for k, v in fields.items()))
        elif op == 'nic_add':
            body = {'type': o.get('type') or 'VIRTIO', 'vlan': _int(o.get('vlan', 0), 'VLAN', 0, 4094),
                    'connected': bool(o.get('connected', True))}
            if body['type'] not in NIC_TYPES:
                raise EditError('NIC type must be one of ' + ', '.join(NIC_TYPES))
            if o.get('macAddress'):
                if not _MAC.match(str(o['macAddress'])):
                    raise EditError('MAC address must look like 7C:4C:58:00:00:01')
                body['macAddress'] = o['macAddress']
            out.append({'op': 'nic_add', 'body': body, 'cycle': True, '_order': 3})
            cycle = True
            summary.append(f"add {body['type']} NIC on VLAN {body['vlan']}")
        elif op == 'nic_remove':
            d = _dev(vm, o.get('device'), 'nic')
            out.append({'op': 'nic_remove', 'device': d['uuid'], 'cycle': True, '_order': 2, 'label': d.get('macAddress')})
            cycle = destructive = True
            summary.append(f"REMOVE NIC {d.get('macAddress')} (VLAN {d.get('vlan')})")
        elif op == 'replication':
            r = replication_op(o, ctx.get('replications') or [], ctx.get('connections') or [])
            if r is None:
                continue
            out.append(dict(r, cycle=False, _order=7))
            summary.append(r['text'])
        else:
            raise EditError(f'unknown change {op!r}')
    if not out:
        raise EditError('no changes')
    out.sort(key=lambda x: x['_order'])
    for x in out:
        x.pop('_order', None)
    return {'ops': out, 'cycle': cycle, 'destructive': destructive, 'summary': summary}


def _describe_vm_field(k: str, v) -> str:
    if k == 'mem':
        return f'memory {int(v) / GIB:g} GB'
    if k == 'numVCPU':
        return f'vCPUs {v}'
    if k == 'bootDevices':
        return 'boot order'
    if k == 'affinityStrategy':
        return 'node affinity'
    if k == 'snapshotScheduleUUID':
        return 'snapshot schedule'
    return f'{k} {v!r}'


FIRMWARE = ('bios', 'uefi', 'tpm', 'tpm-compatible')


def plan_create(spec: dict, isos: list[dict], images: list[dict], schedules: list[dict]) -> dict:
    """Validate a new VM. Returns {'ops': [{'op': 'create', 'spec': ...}], 'summary', 'name'}."""
    name = str(spec.get('name') or '').strip()
    if not name:
        raise EditError('the VM needs a name')
    os_ = spec.get('operatingSystem') or 'os_other'
    if os_ not in OS_TYPES:
        raise EditError('operating system must be one of ' + ', '.join(OS_TYPES))
    fw = spec.get('firmware') or 'uefi'
    if fw not in FIRMWARE:
        raise EditError('firmware must be one of ' + ', '.join(FIRMWARE))
    out = {'name': name, 'description': str(spec.get('description') or ''),
           'tags': ','.join(t.strip() for t in str(spec.get('tags') or '').split(',') if t.strip()),
           'operatingSystem': os_, 'firmware': fw, 'secure_boot': bool(spec.get('secure_boot')) and fw != 'bios',
           'numVCPU': _int(spec.get('numVCPU', 2), 'vCPUs', 1, 256),
           'mem': _int(spec.get('mem', 4 * GIB), 'memory', 128 * 1024 ** 2, 4096 * GIB),
           'guest_tools': bool(spec.get('guest_tools')), 'start': bool(spec.get('start')),
           'disks': [], 'nics': [], 'iso_uuid': None, 'iso_name': '', 'cloud_init': '', 'schedule_uuid': ''}
    image_ids = {i['uuid']: i for i in images}
    for i, d in enumerate(spec.get('disks') or []):
        t = d.get('type') or 'VIRTIO_DISK'
        if t not in DISK_TYPES:
            raise EditError('disk type must be one of ' + ', '.join(DISK_TYPES))
        disk = {'type': t, 'capacity': _int(d.get('capacity'), 'disk size', GIB // 1024, 64 * 1024 * GIB)}
        if d.get('image_uuid'):
            if i != 0 or t != 'VIRTIO_DISK':
                raise EditError('only the first disk can come from an image, and it is a VIRTIO disk')
            img = image_ids.get(d['image_uuid'])
            if not img:
                raise EditError('that image is not on the cluster')
            disk['image_uuid'], disk['image_name'] = img['uuid'], img.get('name') or ''
            disk['capacity'] = max(disk['capacity'], int(img.get('capacity') or 0))
        out['disks'].append(disk)
    for n in spec.get('nics') or []:
        nic = {'type': n.get('type') or 'VIRTIO', 'vlan': _int(n.get('vlan', 0), 'VLAN', 0, 4094), 'connected': bool(n.get('connected', True))}
        if nic['type'] not in NIC_TYPES:
            raise EditError('NIC type must be one of ' + ', '.join(NIC_TYPES))
        out['nics'].append(nic)
    if spec.get('iso_uuid'):
        iso = next((i for i in isos if i.get('uuid') == spec['iso_uuid']), None)
        if not iso:
            raise EditError('that ISO is not on the cluster')
        out['iso_uuid'], out['iso_name'] = iso['uuid'], iso.get('name') or ''
    if spec.get('cloud_init'):
        out['cloud_init'] = str(spec['cloud_init'])
        if len(out['cloud_init']) > 64 * 1024:
            raise EditError('cloud-init user-data is limited to 64 KiB')
    if spec.get('snapshotScheduleUUID'):
        if not any(x.get('uuid') == spec['snapshotScheduleUUID'] for x in schedules):
            raise EditError('that snapshot schedule is not on the cluster')
        out['schedule_uuid'] = spec['snapshotScheduleUUID']
    summary = [f"create {name}: {out['numVCPU']} vCPU, {out['mem'] / GIB:g} GB, {fw}"]
    summary += [f"disk {d['capacity'] / GIB:g} GB" + (f" from {d['image_name']}" if d.get('image_uuid') else '') for d in out['disks']]
    summary += [f"NIC {n['type']} VLAN {n['vlan']}" for n in out['nics']]
    if out['iso_uuid']:
        summary.append(f"CD-ROM {out['iso_name']}")
    if out['start']:
        summary.append('start')
    return {'ops': [{'op': 'create', 'spec': out}], 'cycle': False, 'destructive': False, 'summary': summary, 'name': name}


def _create_vm(hc, change_id: int, spec: dict, step) -> None:
    """New VM: create it with its NICs, then disks (an image clone or empty), the CD-ROM, boot order, start.
    A failure after the VM exists leaves it in place for the admin to look at; nothing is deleted."""
    import base64
    from deploy import attach_image
    dom = {'name': spec['name'], 'description': spec['description'], 'tags': spec['tags'], 'mem': spec['mem'],
           'numVCPU': spec['numVCPU'], 'operatingSystem': spec['operatingSystem'], 'blockDevs': [],
           'netDevs': [dict(n) for n in spec['nics']]}
    if spec.get('schedule_uuid'):
        dom['snapshotScheduleUUID'] = spec['schedule_uuid']
    if spec.get('cloud_init'):
        b64 = lambda t: base64.b64encode(t.encode()).decode()  # noqa: E731 — HyperCore wants base64 (plain text: 500)
        dom['cloudInitData'] = {'userData': b64(spec['cloud_init']),
                                'metaData': b64(f"instance-id: {spec['name']}-{change_id}\nlocal-hostname: {spec['name']}\n")}
    body = {'dom': dom, 'options': {'attachGuestToolsISO': spec['guest_tools'], 'machineTypeKeyword': spec['firmware'],
                                    'useUEFISecureBoot': spec['secure_boot']}}
    r = jobs.retry_transient(lambda: hc._request('POST', '/VirDomain', json=body) or {}, 'create VM')
    if not r.get('createdUUID'):
        raise EditError(f'the cluster did not create the VM: {r}')
    vm_uuid = r['createdUUID']
    db._exec('UPDATE backup.vm_changes SET vm_uuid = %s WHERE id = %s', (vm_uuid, change_id))
    _task(hc, r.get('taskTag', ''), 'create VM', 600)
    step('create VM', True, vm_uuid)
    boot = []
    for d in spec['disks']:
        if d.get('image_uuid'):
            dev = attach_image(hc, d['image_uuid'], vm_uuid, max(1, int(d['capacity']) >> 30))
            step(f"disk from image {d.get('image_name', '')}", True)
        else:
            tag, dev = hc.create_block_device(vm_uuid, d['capacity'], d['type'])
            _task(hc, tag, 'add disk', 600)
            step(f"add {d['type']} {d['capacity'] / GIB:g} GB", True)
        if dev:
            boot.append(dev)
    if spec.get('iso_uuid'):
        r = hc._request('POST', '/VirDomainBlockDevice', json={'virDomainUUID': vm_uuid, 'type': 'IDE_CDROM',
                                                                 'path': f"scribe/{spec['iso_uuid']}"}) or {}
        _task(hc, r.get('taskTag', ''), 'add CD-ROM', 300)
        if r.get('createdUUID'):
            boot.append(r['createdUUID'])            # after the disks: an empty disk falls through to the installer
        step(f"CD-ROM with {spec.get('iso_name', '')}", True)
    if boot:
        _task(hc, hc.patch_vm(vm_uuid, bootDevices=boot), 'boot order', 300)
        step('boot order', True)
    if spec.get('start'):
        _task(hc, jobs.retry_transient(lambda: hc.vm_action(vm_uuid, 'START'), 'start'), 'start', 600)
        step('start', True)


def confirm_ok(destructive: bool, confirm_name: Optional[str], vm: dict) -> bool:
    return not destructive or (confirm_name or '').strip() == (vm.get('name') or '').strip()


# ---------------------------------------------------------------- database
def create(cluster_id: str, vm: dict, kind: str, p: dict, run_at: Optional[datetime], force_off: bool,
           timeout_min: int, created_by: Optional[str]) -> int:
    pending = None if kind == 'create' else db._one("SELECT id FROM backup.vm_changes WHERE cluster_id = %s AND vm_uuid = %s AND status IN ('scheduled', 'running')",
                      (cluster_id, vm['uuid']))
    if pending:
        raise EditError('this VM already has a scheduled or running change; cancel it first')
    row = db._one("""INSERT INTO backup.vm_changes (cluster_id, vm_uuid, vm_name, kind, ops, summary, cycle, run_at,
                       force_off, shutdown_timeout_min, created_by)
                     VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                  (cluster_id, vm['uuid'], vm.get('name'), kind, psycopg2.extras.Json(p['ops']), '; '.join(p['summary']),
                   p['cycle'], run_at, bool(force_off), int(timeout_min), created_by))
    log.info('VM change %s for %s by %s: %s', row['id'], vm.get('name'), created_by, '; '.join(p['summary']))
    return int(row['id'])


def cancel(change_id: int, cluster_id: str, by: Optional[str]) -> bool:
    return db._exec("""UPDATE backup.vm_changes SET status = 'cancelled', cancelled_by = %s, finished_at = now()
                       WHERE id = %s AND cluster_id = %s AND status = 'scheduled'""", (by, change_id, cluster_id)) > 0


def get(change_id: int) -> Optional[dict]:
    return db._one('SELECT * FROM backup.vm_changes WHERE id = %s', (change_id,))


def list_changes(cluster_id: str, vm_uuid: Optional[str] = None, limit: int = 50) -> list[dict]:
    if vm_uuid:
        return db._all('SELECT * FROM backup.vm_changes WHERE cluster_id = %s AND vm_uuid = %s ORDER BY created_at DESC LIMIT %s',
                       (cluster_id, vm_uuid, limit))
    return db._all("""SELECT * FROM backup.vm_changes WHERE cluster_id = %s
                      ORDER BY (status IN ('scheduled', 'running')) DESC, created_at DESC LIMIT %s""", (cluster_id, limit))


def due() -> list[int]:
    return [r['id'] for r in db._all("""SELECT id FROM backup.vm_changes WHERE status = 'scheduled'
                                         AND (run_at IS NULL OR run_at <= now()) ORDER BY run_at NULLS FIRST, id""")]


def interrupted() -> int:
    """Sets left 'running' by a restart of this service: their outcome is unknown."""
    return db._exec("""UPDATE backup.vm_changes SET status = 'failed', finished_at = now(),
                         error = 'interrupted by a restart of the backup service: check the VM'
                       WHERE status = 'running'""")


def _claim(change_id: int) -> bool:
    return db._exec("UPDATE backup.vm_changes SET status = 'running', started_at = now() WHERE id = %s AND status = 'scheduled'",
                    (change_id,)) > 0


def _finish(change_id: int, status: str, result: list, error: Optional[str]) -> None:
    db._exec('UPDATE backup.vm_changes SET status = %s, finished_at = now(), result = %s, error = %s WHERE id = %s',
             (status, psycopg2.extras.Json(result), error, change_id))


def _progress(change_id: int, result: list) -> None:
    db._exec('UPDATE backup.vm_changes SET result = %s WHERE id = %s', (psycopg2.extras.Json(result), change_id))


# --------------------------------------------------------------- execution
class _Step:
    def __init__(self, change_id: int, result: list):
        self.id, self.result = change_id, result

    def __call__(self, what: str, ok: bool = True, detail: str = '') -> None:
        self.result.append({'at': datetime.now(timezone.utc).isoformat(), 'step': what, 'ok': ok, 'detail': detail})
        try:
            _progress(self.id, self.result)
        except Exception:  # noqa: BLE001 — progress is a nicety
            pass


def _task(hc, tag: str, what: str, timeout: float = 900) -> None:
    if tag:
        wait_task(hc, tag, what, timeout)


def _state(hc, vm_uuid: str) -> str:
    return str(hc.get_vm(vm_uuid).get('state') or '').upper()


SHUTDOWN_REPEAT = 60


def _wait_off(hc, vm_uuid: str, seconds: float, repeat=None) -> bool:
    """Poll until the VM is off. `repeat` is called every SHUTDOWN_REPEAT seconds while waiting: a guest
    that is still booting ignores the ACPI power button (seen on Lab 241: a shutdown sent 3 s after a
    start was lost and the set ran into its timeout), so the request is sent again."""
    deadline = time.monotonic() + seconds
    next_repeat = time.monotonic() + SHUTDOWN_REPEAT
    while time.monotonic() < deadline:
        if _state(hc, vm_uuid) in OFF_STATES:
            return True
        if repeat and time.monotonic() >= next_repeat:
            try:
                repeat()
            except HyperCoreError as e:
                log.info('repeated shutdown request for %s not accepted: %s', vm_uuid, e)
            next_repeat = time.monotonic() + SHUTDOWN_REPEAT
        time.sleep(5)
    return _state(hc, vm_uuid) in OFF_STATES


def _power_off(hc, ch: dict, step: _Step) -> None:
    """OS shutdown and wait; past the timeout a forced power off, or EditError when not allowed."""
    vm = ch['vm_uuid']
    mins = int(ch['shutdown_timeout_min'])
    # the VM's state is the measure, not the task: HyperCore's SHUTDOWN task may stay open until the
    # guest is down, and the timeout here is the user's, not a task limit
    jobs.retry_transient(lambda: hc.vm_action(vm, 'SHUTDOWN'), 'shutdown')
    if _wait_off(hc, vm, mins * 60, repeat=lambda: hc.vm_action(vm, 'SHUTDOWN')):
        step('OS shutdown', True, 'the VM is off')
        return
    if not ch['force_off']:
        step('OS shutdown', False, f'the guest did not shut down within {mins} min')
        raise EditError(f'the guest did not shut down within {mins} minutes; nothing was changed and the VM keeps '
                        'running (tick "force power off" to power it off in that case)')
    step('OS shutdown', False, f'the guest did not shut down within {mins} min: powering off')
    jobs.retry_transient(lambda: hc.vm_action(vm, 'STOP'), 'power off')
    if not _wait_off(hc, vm, 300):
        raise EditError('the VM did not power off')
    step('power off', True)


def _apply(hc, vm_uuid: str, o: dict) -> str:
    """One change; returns the task tag to wait for."""
    op = o['op']
    if op == 'vm':
        return hc.patch_vm(vm_uuid, **o['fields'])
    if op == 'disk_resize':
        return hc.patch_block_device(o['device'], capacity=o['capacity'])
    if op == 'disk_set':
        return hc.patch_block_device(o['device'], **o['fields'])
    if op == 'disk_add':
        r = hc._request('POST', '/VirDomainBlockDevice', json=dict(o['body'], virDomainUUID=vm_uuid)) or {}
        return r.get('taskTag', '')
    if op == 'disk_remove':
        return hc.delete_block_device(o['device'])
    if op == 'iso':
        return hc.set_cdrom(o['device'], o.get('iso_uuid'))
    if op == 'nic_set':
        return hc.patch_netdev(o['device'], **o['fields'])
    if op == 'nic_add':
        r = hc._request('POST', '/VirDomainNetDevice', json=dict(o['body'], virDomainUUID=vm_uuid)) or {}
        return r.get('taskTag', '')
    if op == 'nic_remove':
        r = hc._request('DELETE', f"/VirDomainNetDevice/{o['device']}") or {}
        return r.get('taskTag', '')
    if op == 'replication':
        a = o.get('action')
        if a == 'create':
            r = hc._request('POST', '/VirDomainReplication', json={'sourceDomainUUID': vm_uuid, 'connectionUUID': o['connection'],
                                                                   'enable': True, 'label': ''}) or {}
        else:
            r = hc._request('PATCH', f"/VirDomainReplication/{o['replication']}", json={'enable': a == 'enable'}) or {}
        return r.get('taskTag', '')
    raise EditError(f'unknown change {op}')


def explain(o: dict, error: str) -> str:
    """HyperCore's error, made actionable where we know the cause."""
    low = error.lower()
    if o.get('op') == 'replication' and o.get('action') == 'create' and 'pre-existing replication' in low:
        cluster = (o.get('text') or '').replace('start replicating to ', '') or 'the remote cluster'
        return (f'{cluster} still holds the replica from an earlier replication of this VM, and HyperCore cannot start a '
                f'new replication while it exists. Ask an administrator of {cluster} to remove that replica there, then '
                f'try again. (HyperCore: {error.split(" failed: ", 1)[-1]})')
    return error


def _describe(o: dict) -> str:
    op = o['op']
    return {'vm': 'VM settings', 'disk_resize': 'grow disk', 'disk_set': 'disk settings', 'disk_add': 'add disk',
            'disk_remove': f"remove disk {o.get('label', '')}", 'iso': 'CD-ROM', 'nic_set': 'NIC settings',
            'nic_add': 'add NIC', 'nic_remove': f"remove NIC {o.get('label', '')}",
            'replication': o.get('text') or 'replication'}.get(op, op)


def run(change_id: int) -> None:
    """Run one change set (APScheduler job). Every step lands in its result as it happens."""
    if not _claim(change_id):
        return
    ch = get(change_id)
    result: list = []
    step = _Step(change_id, result)
    vm_uuid = ch['vm_uuid']
    was_running = False
    off_by_us = False
    try:
        hc = jobs.client_for(ch['cluster_id'])
        pf = jobs.preflight(hc, ch['cluster_id'])
        if not pf.get('ok'):
            raise EditError(f"cluster not ready: {pf.get('detail')}")
        if ch['kind'] == 'create':
            _create_vm(hc, change_id, ch['ops'][0]['spec'], step)
            _finish(change_id, 'done', result, None)
            return
        if ch['kind'] == 'snapshot':
            o = ch['ops'][0]
            tag, snap = jobs.retry_transient(lambda: hc.create_snapshot(vm_uuid, o['label'], quiesce=o['quiesce'],
                                                                        retain_days=o['retain_days'] or None,
                                                                        replication=o['replicate']), 'snapshot')
            _task(hc, tag, 'snapshot', 900)
            step('snapshot', True, f"{o['label']} ({snap})")
            _finish(change_id, 'done', result, None)
            return
        if ch['kind'] == 'clone':
            o = ch['ops'][0]
            body = {'template': {'name': o['name'], 'description': o['description'], 'tags': o['tags']}}
            if o.get('snap_uuid'):
                body['snapUUID'] = o['snap_uuid']
            r = jobs.retry_transient(lambda: hc._request('POST', f'/VirDomain/{vm_uuid}/clone', json=body) or {}, 'clone')
            _task(hc, r.get('taskTag', ''), 'clone', 3600)
            new = r.get('createdUUID', '')
            step('clone', True, f"{o['name']} ({new})")
            if o.get('start') and new:
                _task(hc, jobs.retry_transient(lambda: hc.vm_action(new, 'START'), 'start clone'), 'start clone', 600)
                step('start the clone', True)
            _finish(change_id, 'done', result, None)
            return
        state = _state(hc, vm_uuid)
        was_running = state not in OFF_STATES
        ops = ch['ops'] or []
        if ch['kind'] == 'power':
            action = ops[0]['action']
            if action == 'SHUTDOWN':
                if was_running:
                    _power_off(hc, ch, step)
                else:
                    step('OS shutdown', True, 'the VM was already off')
            else:
                if action == 'START' and was_running:
                    step('start', True, 'the VM was already running')
                else:
                    _task(hc, jobs.retry_transient(lambda: hc.vm_action(vm_uuid, action), action.lower()), action.lower(), 600)
                    step(action.lower(), True)
            _finish(change_id, 'done', result, None)
            return
        if ch['cycle'] and was_running:
            _power_off(hc, ch, step)
            off_by_us = True
        if ch['kind'] == 'delete':
            _task(hc, jobs.retry_transient(lambda: hc.delete_vm(vm_uuid), 'delete VM'), 'delete VM', 900)
            step('delete VM', True)
            _finish(change_id, 'done', result, None)
            return
        if any(o['op'] == 'disk_remove' for o in ops):
            tag, snap = jobs.retry_transient(
                lambda: hc.create_snapshot(vm_uuid, f'hcm-before-edit-{change_id}', quiesce=False, retain_days=30), 'snapshot')
            _task(hc, tag, 'snapshot before removing a disk', 600)
            step('snapshot before removing a disk', True, f'hcm-before-edit-{change_id}, kept 30 days')
        failed = None
        for o in ops:
            try:
                _task(hc, jobs.retry_transient(lambda o=o: _apply(hc, vm_uuid, o), _describe(o)), _describe(o), 900)
                step(_describe(o), True)
            except (HyperCoreError, IncrementalError, EditError) as e:
                msg = explain(o, str(e))
                step(_describe(o), False, msg[:400])
                failed = msg
                break                                    # later changes may depend on this one
        if off_by_us:
            try:
                _task(hc, jobs.retry_transient(lambda: hc.vm_action(vm_uuid, 'START'), 'start'), 'start', 600)
                step('start', True)
            except (HyperCoreError, IncrementalError) as e:
                step('start', False, str(e)[:300])
                failed = failed or f'start: {e}'
        _finish(change_id, 'failed' if failed else 'done', result, failed)
    except Exception as e:  # noqa: BLE001
        if isinstance(e, (HyperCoreError, IncrementalError, EditError)):
            log.warning('VM change %s failed: %s', change_id, e)
            err = str(e)[:500]
        else:
            log.exception('VM change %s crashed', change_id)
            err = f'unexpected error: {e}'[:500]
        if off_by_us and ch['kind'] != 'delete':
            # we shut it down, so we start it again, whatever went wrong in between
            try:
                _task(hc, jobs.retry_transient(lambda: hc.vm_action(vm_uuid, 'START'), 'start'), 'start', 600)
                step('start', True, 'started again after the failure')
            except Exception as e2:  # noqa: BLE001
                step('start', False, str(e2)[:300])
                err += f'; the VM could not be started again: {e2}'[:300]
        _finish(change_id, 'failed', result, err)


_running_lock = threading.Lock()


def tick(enqueue) -> None:
    """Start every due set (run_at passed, or 'now'): called every few seconds by the scheduler."""
    with _running_lock:
        for cid in due():
            enqueue(cid)
