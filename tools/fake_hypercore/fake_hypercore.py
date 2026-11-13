"""
fake_hypercore.py — a synthetic HyperCore cluster for local testing without a real cluster.

Serves just enough of HyperCore over HTTPS (self-signed) for the stack to register it, collect
metrics from it and open VM consoles on it:

  /rest/v1/login, logout, ping, Cluster, Node, VirDomain[/uuid], VirDomainStats, VirDomainSnapshot,
  VirDomainReplication, Condition, TimeZone          (login: any user, password "fake")
  /update/update_status.json                         (idle)
  /node/<nodeUUID>/console?sessionID=..&vm=..        console websocket: RFB 3.8, security None,
                                                     like HyperCore's; closes on a bad session or node

Each VM shows a scripted screen that reacts to the keyboard:
  login     Linux text console at its login prompt; type a user, Enter, a password, Enter -> shell
  shell     logged-in Linux shell; "exit" + Enter -> login
  winlock   Windows lock screen ("Press Ctrl+Alt+Delete to unlock"); Ctrl+Alt+Del -> windesk
  windesk   Windows desktop (an open session); Win+L -> winlock
  picture   graphical lock screen without any text but a clock (needs "learn locked state")
  blank     black; any key or pointer movement wakes it to `wake_to` (default shell)

Test controls (no auth, test use only):
  POST /fake/vm/<uuid>  {"screen": "...", "state": "RUNNING"|"SHUTOFF", "node": <index>}
  POST /fake/conditions [{name, severity, value, isOK, nodeIndex, description, lastSetTime, lastClearTime}]
  GET  /fake/state

Run: python fake_hypercore.py [--port 443]
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import ipaddress
import json
import os
import random
import ssl
import struct
import tempfile
import time
import uuid

from aiohttp import WSMsgType, web
from PIL import Image, ImageDraw, ImageFont

FONT_PATH = '/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf'
FONT_SANS = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
NOW_MS = lambda: int(time.time() * 1000)  # noqa: E731

CLUSTER_UUID = 'f4ce0000-0000-4000-8000-00000000c1a5'
NODES = [{'uuid': f'f4ce0000-0000-4000-9000-00000000000{i}', 'peerID': i + 1,
          'lanIP': f'10.99.0.{11 + i}'} for i in range(3)]

VMS = [
    ('web01', 'login', 0, 'linux'), ('db01', 'shell', 1, 'linux'), ('dc01', 'winlock', 2, 'windows'),
    ('rds01', 'windesk', 0, 'windows'), ('kiosk01', 'picture', 1, 'windows'), ('app02', 'blank', 2, 'linux'),
    ('backup-old', 'login', 0, 'linux'),
]

state = {
    'sessions': set(),
    'vms': {},
    'conditions': [],
}


def _vm_uuid(i: int) -> str:
    return f'f4ce0000-0000-4000-8000-{i:012d}'


for i, (name, screen, node, os_) in enumerate(VMS):
    state['vms'][_vm_uuid(i)] = {
        'uuid': _vm_uuid(i), 'name': name, 'screen': screen, 'node': node, 'os': os_,
        'state': 'SHUTOFF' if name == 'backup-old' else 'RUNNING', 'typed': '', 'login_step': 'user',
        'wake_to': 'shell', 'version': 0,
    }

state['conditions'] = [
    {'name': 'DriveFailure', 'severity': 'CRITICAL', 'value': True, 'isOK': True, 'nodeIndex': 1,
     'description': 'Drive in slot 3 has failed', 'lastSetTime': NOW_MS() - 3 * 3600_000, 'lastClearTime': 0},
    {'name': 'NodeHighMemory', 'severity': 'WARNING', 'value': True, 'isOK': True, 'nodeIndex': 0,
     'description': 'Node memory usage above 90%', 'lastSetTime': NOW_MS() - 40 * 60_000, 'lastClearTime': NOW_MS() - 2 * 86400_000},
    {'name': 'TimeSyncDrift', 'severity': 'NOTICE', 'value': True, 'isOK': True, 'nodeIndex': 2,
     'description': 'NTP offset above 500 ms', 'lastSetTime': NOW_MS() - 26 * 3600_000, 'lastClearTime': 0},
    {'name': 'LicenseExpiring', 'severity': 'INFO', 'value': True, 'isOK': True, 'nodeIndex': 0,
     'description': 'Support contract expires in 30 days', 'lastSetTime': NOW_MS() - 5 * 86400_000, 'lastClearTime': 0},
    {'name': 'StaleAlarm', 'severity': 'CRITICAL', 'value': True, 'isOK': False, 'nodeIndex': 2,
     'description': 'Expired condition (isOK false): must not show', 'lastSetTime': NOW_MS() - 86400_000, 'lastClearTime': 0},
    {'name': 'ClearedAlarm', 'severity': 'ERROR', 'value': False, 'isOK': True, 'nodeIndex': 1,
     'description': 'Replication link down', 'lastSetTime': NOW_MS() - 6 * 3600_000, 'lastClearTime': NOW_MS() - 5 * 3600_000},
    {'name': 'DebugThing', 'severity': 'DEBUG', 'value': True, 'isOK': True, 'nodeIndex': 0,
     'description': 'Debug-level condition: must not show', 'lastSetTime': NOW_MS() - 3600_000, 'lastClearTime': 0},
]


# ------------------------------------------------------------------ REST
def _authed(request: web.Request) -> bool:
    return request.cookies.get('sessionID') in state['sessions']


@web.middleware
async def rest_auth(request: web.Request, handler):
    p = request.path
    if p.startswith('/rest/') and not p.endswith(('/login', '/ping')) and not _authed(request):
        return web.json_response({'error': 'unauthorized'}, status=401)
    return await handler(request)


async def login(request):
    b = await request.json()
    if b.get('password') != 'fake':
        return web.json_response({'error': 'bad credentials'}, status=401)
    sid = uuid.uuid4().hex
    state['sessions'].add(sid)
    r = web.json_response({'sessionID': sid})
    r.set_cookie('sessionID', sid, secure=True, httponly=True)
    return r


async def logout(request):
    state['sessions'].discard(request.cookies.get('sessionID'))
    return web.json_response({})


def _node(i: int) -> dict:
    n = NODES[i]
    return {**n, 'backplaneIP': f'10.98.0.{11 + i}', 'networkStatus': 'ONLINE', 'currentDisposition': 'IN',
            'allowRunningVMs': True, 'virtualizationOnline': True, 'supportsVirtualization': True,
            'cpuUsage': 12.5 + 5 * i + random.random() * 3, 'totalMemUsageBytes': (40 + 5 * i) * 2**30,
            'systemMemUsageBytes': 4 * 2**30, 'memUsagePercentage': 40.0 + 5 * i, 'memSize': 128 * 2**30,
            'capacity': 8 * 2**40, 'numThreads': 32, 'numCores': 16, 'numSockets': 1, 'drives': []}


def _cfg(v: dict) -> dict:
    """The VM's editable configuration, created on first use and changed by the write endpoints."""
    if 'cfg' not in v:
        u = v['uuid']
        v['cfg'] = {
            'description': 'synthetic', 'operatingSystem': 'os_windows_server_2012' if v['os'] == 'windows' else 'os_other',
            'mem': 4 * 2**30, 'numVCPU': 2, 'tags': 'fake,' + v['os'], 'snapshotScheduleUUID': '',
            'machineType': 'scale-7.2', 'cpuType': 'clusterBaseline-7.3', 'machineTypeKeyword': 'bios',
            'affinityStrategy': {'strictAffinity': False, 'preferredNodeUUID': '', 'backupNodeUUID': ''},
            'blockDevs': [{'uuid': u + '-bd01', 'virDomainUUID': u, 'name': '', 'type': 'VIRTIO_DISK',
                           'capacity': 50 * 2**30, 'allocation': 10 * 2**30, 'physical': 10 * 2**30, 'slot': 0,
                           'cacheMode': 'WRITETHROUGH', 'tieringPriorityFactor': 8, 'disableSnapshotting': False, 'path': ''},
                          {'uuid': u + '-cd01', 'virDomainUUID': u, 'name': '', 'type': 'IDE_CDROM', 'capacity': 0,
                           'allocation': 0, 'physical': 0, 'slot': 0, 'cacheMode': 'NONE', 'tieringPriorityFactor': 0,
                           'disableSnapshotting': False, 'path': ''}],
            'netDevs': [{'uuid': u + '-nd01', 'virDomainUUID': u, 'type': 'VIRTIO',
                         'macAddress': '7C:4C:58:00:' + u[-4:-2].upper() + ':' + u[-2:].upper(), 'vlan': 0, 'connected': True,
                         'ipv4Addresses': ['10.99.1.' + str(int(u[-2:]) + 10)]}],
        }
        v['cfg']['bootDevices'] = [v['cfg']['blockDevs'][0]['uuid']]
    return v['cfg']


def _vm(v: dict) -> dict:
    c = _cfg(v)
    return {'uuid': v['uuid'], 'name': v['name'], 'nodeUUID': NODES[v['node']]['uuid'] if v['state'] == 'RUNNING' else '',
            'state': v['state'], 'snapUUIDs': list(v.get('snaps', [])), 'guestAgentState': 'AVAILABLE' if v['state'] == 'RUNNING' else 'UNAVAILABLE',
            'replicationUUIDs': [], 'useUEFISecureBoot': False,
            'console': {'type': 'VNC', 'ip': '0.0.0.0', 'port': 5900, 'keymap': 'en-us'}, **c}


async def r_cluster(_):
    return web.json_response([{'uuid': CLUSTER_UUID, 'clusterName': 'FakeCluster', 'icosVersion': '9.8.9.fake'}])


async def r_nodes(_):
    return web.json_response([_node(i) for i in range(len(NODES))])


async def r_vms(_):
    return web.json_response([_vm(v) for v in state['vms'].values()])


async def r_vm(request):
    v = state['vms'].get(request.match_info['uuid'])
    return web.json_response([_vm(v)]) if v else web.json_response({'error': 'not found'}, status=404)


async def r_stats(_):
    return web.json_response([{'uuid': v['uuid'], 'cpuUsage': 3 + random.random() * 20, 'rxBitRate': 1000, 'txBitRate': 2000,
                               'vsdStats': []} for v in state['vms'].values() if v['state'] == 'RUNNING'])


async def r_conditions(_):
    out = []
    for c in state['conditions']:
        n = NODES[c.get('nodeIndex', 0)]
        out.append({'name': c['name'], 'description': c['description'], 'value': c['value'], 'isOK': c['isOK'],
                    'isExpired': not c['isOK'], 'nodeUUID': n['uuid'], 'nodeLANIP': n['lanIP'], 'userVisible': True,
                    'severity': c['severity'], 'lastSetTime': c.get('lastSetTime', 0), 'lastClearTime': c.get('lastClearTime', 0),
                    'modified': NOW_MS(), 'tags': c.get('tags', [])})
    return web.json_response(out)


async def r_empty(_):
    return web.json_response([])


async def r_tz(_):
    return web.json_response([{'timeZone': 'Europe/Amsterdam'}])


async def r_update(_):
    return web.json_response({'updateStatus': {'masterState': 'COMPLETE', 'percent': 100}})


async def r_ping(_):
    return web.json_response({'status': 'Active'})


# ---------------------------------------------------------------- writes
# Every write returns a task tag that is COMPLETE at once. SHUTDOWN takes 3 s, unless the VM's
# `ignores_acpi` is set (a guest that never shuts down); a change set test uses that for the timeout.
_tasks: dict[str, str] = {}
_log: list = []


def _task(what: str, created: str = '') -> web.Response:
    tag = str(len(_tasks) + 1000)
    _tasks[tag] = 'COMPLETE'
    _log.append({'t': time.time(), 'task': tag, 'what': what})
    return web.json_response({'taskTag': tag, 'createdUUID': created})


async def r_task(request):
    tag = request.match_info['tag']
    return web.json_response([{'taskTag': tag, 'state': _tasks.get(tag, 'UNINITIALIZED'), 'progressPercent': 100,
                               'formattedMessage': '', 'formattedDescription': ''}])


def _find_dev(uuid: str, kind: str):
    for v in state['vms'].values():
        for d in _cfg(v)[kind]:
            if d['uuid'] == uuid:
                return v, d
    return None, None


async def r_action(request):
    out = []
    for a in await request.json():
        v = state['vms'].get(a.get('virDomainUUID'))
        if not v:
            return web.json_response({'error': 'no such VM'}, status=404)
        act = a.get('actionType')
        if act == 'START':
            v['state'] = 'RUNNING'
        elif act == 'STOP':
            v['state'] = 'SHUTOFF'
        elif act == 'SHUTDOWN' and not v.get('ignores_acpi'):
            asyncio.get_running_loop().call_later(3, lambda v=v: v.update(state='SHUTOFF'))
        _log.append({'t': time.time(), 'what': f"{act} {v['name']}"})
        tag = str(len(_tasks) + 1000)
        _tasks[tag] = 'COMPLETE'
        out.append({'taskTag': tag})
    return web.json_response(out)


async def r_vm_patch(request):
    v = state['vms'].get(request.match_info['uuid'])
    if not v:
        return web.json_response({'error': 'not found'}, status=404)
    b = await request.json()
    if 'name' in b:
        v['name'] = b.pop('name')
    _cfg(v).update(b)
    return _task(f"patch {v['name']} {sorted(b) + (['name'] if 'name' in b else [])}")


async def r_vm_delete(request):
    v = state['vms'].pop(request.match_info['uuid'], None)
    return _task(f"delete {v['name']}") if v else web.json_response({'error': 'not found'}, status=404)


async def r_dev_post(request):
    kind = 'blockDevs' if 'Block' in request.path else 'netDevs'
    b = await request.json()
    v = state['vms'].get(b.get('virDomainUUID'))
    if not v:
        return web.json_response({'error': 'no such VM'}, status=404)
    d = dict(b, uuid=str(uuid.uuid4()))
    if kind == 'blockDevs':
        d.setdefault('slot', len(_cfg(v)['blockDevs']))
        d.setdefault('name', ''); d.setdefault('allocation', 0); d.setdefault('path', '')
    else:
        d.setdefault('macAddress', '7C:4C:58:%02X:%02X:%02X' % tuple(random.randrange(256) for _ in range(3)))
        d['ipv4Addresses'] = []
    _cfg(v)[kind].append(d)
    return _task(f"add {kind} to {v['name']}", d['uuid'])


async def r_dev_patch(request):
    kind = 'blockDevs' if 'Block' in request.path else 'netDevs'
    v, d = _find_dev(request.match_info['uuid'], kind)
    if not d:
        return web.json_response({'error': 'not found'}, status=404)
    b = await request.json()
    d.update(b)
    return _task(f"patch {kind} of {v['name']} {sorted(b)}")


async def r_dev_delete(request):
    kind = 'blockDevs' if 'Block' in request.path else 'netDevs'
    v, d = _find_dev(request.match_info['uuid'], kind)
    if not d:
        return web.json_response({'error': 'not found'}, status=404)
    _cfg(v)[kind].remove(d)
    return _task(f"delete {kind} of {v['name']}")


async def r_isos(_):
    return web.json_response([{'uuid': 'f4ce0000-0000-4000-a000-000000000001', 'name': 'ubuntu-24.04-server.iso', 'size': 2 * 2**30,
                               'readyForInsert': True, 'path': 'scribe/f4ce0000-0000-4000-a000-000000000001'},
                              {'uuid': 'f4ce0000-0000-4000-a000-000000000002', 'name': 'virtio-win-0.1.262.iso', 'size': 700 * 2**20,
                               'readyForInsert': True, 'path': 'scribe/f4ce0000-0000-4000-a000-000000000002'}])


async def r_schedules(_):
    return web.json_response([{'uuid': 'f4ce0000-0000-4000-b000-000000000001', 'name': 'Bronze', 'rrules': []},
                              {'uuid': 'f4ce0000-0000-4000-b000-000000000002', 'name': 'Gold', 'rrules': []}])


_reps = {'f4ce0000-0000-4000-c000-000000000001': {'uuid': 'f4ce0000-0000-4000-c000-000000000001', 'label': '',
                                                   'enable': True, 'sourceDomainUUID': _vm_uuid(2), 'targetDomainUUID': 'remote',
                                                   'connectionUUID': 'f4ce0000-0000-4000-e000-000000000001'}}


async def r_remote_conns(_):
    return web.json_response([{'uuid': 'f4ce0000-0000-4000-e000-000000000001', 'connectionStatus': 'ESTABLISHED',
                               'remoteClusterInfo': {'uuid': 'remote-cluster', 'clusterName': 'FakeDR', 'icosVersion': '9.8.9'},
                               'replicationOK': True}])


async def r_reps(_):
    return web.json_response(list(_reps.values()))


async def r_rep_create(request):
    b = await request.json()
    u = str(uuid.uuid4())
    _reps[u] = {'uuid': u, 'label': b.get('label', ''), 'enable': bool(b.get('enable', True)), 'sourceDomainUUID': b.get('sourceDomainUUID'),
                'targetDomainUUID': 'remote-' + u[:8], 'connectionUUID': b.get('connectionUUID')}
    return _task(f"replication created to {b.get('connectionUUID')}", u)


async def r_rep_patch(request):
    r = _reps.get(request.match_info['uuid'])
    if not r:
        return web.json_response({'error': 'not found'}, status=404)
    r.update({k: v for k, v in (await request.json()).items() if k in ('enable', 'label', 'connectionUUID')})
    return _task(f"replication {r['uuid'][:8]} enable={r['enable']} conn={r['connectionUUID']}")


_snaps: list = []


async def r_snapshot(request):
    b = await request.json()
    u = str(uuid.uuid4())
    _snaps.append({'uuid': u, 'domainUUID': b.get('domainUUID'), 'label': b.get('label'), 'type': b.get('type', 'USER'),
                   'timestamp': int(time.time()), 'localRetainUntilTimestamp': b.get('localRetainUntilTimestamp', 0),
                   'state': 'CREATED', 'blockCountDiff': 0})
    v = state['vms'].get(b.get('domainUUID'))
    if v:
        v.setdefault('snaps', []).append(u)
    return _task(f"snapshot {b.get('label')} quiesce={b.get('attemptQuiesce')} repl={b.get('replication')}", u)


async def r_snapshots(_):
    return web.json_response(_snaps)


async def r_clone(request):
    src = state['vms'].get(request.match_info['uuid'])
    if not src:
        return web.json_response({'error': 'not found'}, status=404)
    b = await request.json()
    t = b.get('template') or {}
    import copy
    u = str(uuid.uuid4())
    v = copy.deepcopy({k: x for k, x in src.items() if k != 'snaps'})
    v.update(uuid=u, name=t.get('name') or src['name'] + '-clone', state='SHUTOFF')
    for d in v['cfg']['blockDevs']:
        d['uuid'], d['virDomainUUID'] = str(uuid.uuid4()), u
    for n in v['cfg']['netDevs']:
        n['uuid'], n['virDomainUUID'] = str(uuid.uuid4()), u
        n['macAddress'] = '7C:4C:58:%02X:%02X:%02X' % tuple(random.randrange(256) for _ in range(3))
    v['cfg']['description'] = t.get('description', v['cfg']['description'])
    v['cfg']['tags'] = t.get('tags', v['cfg']['tags'])
    v['cfg']['bootDevices'] = [v['cfg']['blockDevs'][0]['uuid']] if v['cfg']['blockDevs'] else []
    state['vms'][u] = v
    return _task(f"clone {src['name']} -> {v['name']} snap={b.get('snapUUID') or 'new'}", u)


async def r_vm_create(request):
    b = await request.json()
    dom, opts = b.get('dom') or {}, b.get('options') or {}
    u = str(uuid.uuid4())
    v = {'uuid': u, 'name': dom.get('name') or 'new', 'screen': 'blank', 'node': 0,
         'os': 'windows' if 'windows' in (dom.get('operatingSystem') or '') else 'linux', 'state': 'SHUTOFF',
         'typed': '', 'login_step': 'user', 'wake_to': 'login', 'version': 0}
    v['cfg'] = {'description': dom.get('description', ''), 'operatingSystem': dom.get('operatingSystem', 'os_other'),
                'mem': dom.get('mem', 2**30), 'numVCPU': dom.get('numVCPU', 1), 'tags': dom.get('tags', ''),
                'snapshotScheduleUUID': dom.get('snapshotScheduleUUID', ''), 'machineType': 'scale-7.2',
                'cpuType': 'clusterBaseline-7.3', 'machineTypeKeyword': opts.get('machineTypeKeyword', 'bios'),
                'affinityStrategy': {'strictAffinity': False, 'preferredNodeUUID': '', 'backupNodeUUID': ''},
                'blockDevs': [], 'bootDevices': [],
                'netDevs': [dict(n, uuid=str(uuid.uuid4()), virDomainUUID=u, ipv4Addresses=[],
                                 macAddress=n.get('macAddress') or '7C:4C:58:%02X:%02X:%02X' % tuple(random.randrange(256) for _ in range(3)))
                            for n in dom.get('netDevs') or []]}
    if opts.get('machineTypeKeyword') in ('uefi', 'tpm', 'tpm-compatible'):
        v['cfg']['blockDevs'].append({'uuid': str(uuid.uuid4()), 'virDomainUUID': u, 'type': 'NVRAM', 'capacity': 540672, 'slot': -1,
                                      'name': '', 'path': '', 'cacheMode': 'NONE', 'tieringPriorityFactor': 0, 'disableSnapshotting': False})
    state['vms'][u] = v
    return _task(f"create {v['name']} cloudinit={bool(dom.get('cloudInitData'))} fw={opts.get('machineTypeKeyword')}", u)


_images = [{'uuid': 'f4ce0000-0000-4000-d000-000000000001', 'name': 'ubuntu-noble-cloudimg.img', 'capacityBytes': 3758096384}]


async def r_images(_):
    return web.json_response(_images)


async def r_image_attach(request):
    t = (await request.json()).get('template') or {}
    v = state['vms'].get(t.get('virDomainUUID'))
    if not v:
        return web.json_response({'error': 'no such VM'}, status=404)
    d = {'uuid': str(uuid.uuid4()), 'virDomainUUID': v['uuid'], 'type': t.get('type', 'VIRTIO_DISK'), 'capacity': t.get('capacity', 0),
         'allocation': 2 * 2**30, 'slot': len(_cfg(v)['blockDevs']), 'name': '', 'path': '', 'cacheMode': 'WRITETHROUGH',
         'tieringPriorityFactor': 8, 'disableSnapshotting': False}
    _cfg(v)['blockDevs'].append(d)
    v['screen'] = 'login'                            # it boots into the image's login prompt
    return _task(f"attach image to {v['name']}", d['uuid'])


async def fake_log(_):
    return web.json_response(_log)


async def fake_vm(request):
    v = state['vms'].get(request.match_info['uuid'])
    if not v:
        return web.json_response({'error': 'not found'}, status=404)
    b = await request.json()
    for k in ('screen', 'state', 'node', 'wake_to', 'ignores_acpi'):
        if k in b:
            v[k] = b[k]
    v['typed'] = ''
    v['version'] += 1
    return web.json_response(v)


async def fake_conditions(request):
    state['conditions'] = await request.json()
    return web.json_response({'ok': True})


async def fake_state(_):
    return web.json_response({'vms': list(state['vms'].values()), 'conditions': state['conditions'],
                              'sessions': len(state['sessions'])})


# ---------------------------------------------------------------- screens
def _font(size: int, sans: bool = False):
    try:
        return ImageFont.truetype(FONT_SANS if sans else FONT_PATH, size)
    except OSError:
        return ImageFont.load_default()


def _text_console(lines: list[str]) -> Image.Image:
    img = Image.new('RGB', (720, 400), (0, 0, 0))
    d = ImageDraw.Draw(img)
    f = _font(14)
    rows = lines[-24:]
    for i, ln in enumerate(rows):
        d.text((4, 2 + i * 16), ln, fill=(200, 200, 200), font=f)
    return img


def render(v: dict) -> Image.Image:
    name, screen, typed = v['name'], v['screen'], v['typed']
    boot = ['[  OK  ] Started systemd-journald.service - Journal Service.',
            '[  OK  ] Reached target network-online.target - Network is Online.',
            '[  OK  ] Started ssh.service - OpenBSD Secure Shell server.', '']
    if screen == 'login':
        prompt = f'{name} login: ' if v['login_step'] == 'user' else 'Password: '
        shown = typed if v['login_step'] == 'user' else ''
        lines = boot + [f'Ubuntu 24.04.1 LTS {name} tty1', '']
        if v['login_step'] == 'password':
            lines += [f'{name} login: {v.get("user", "")}']
        return _text_console(lines + [prompt + shown + '_'])
    if screen == 'shell':
        return _text_console([f'{name} login: root', 'Password:', 'Welcome to Ubuntu 24.04.1 LTS (GNU/Linux 6.8.0-45-generic x86_64)',
                              '', ' * Documentation:  https://help.ubuntu.com', '',
                              'Last login: Thu Nov 12 09:14:02 2026 from 10.0.0.5', f'root@{name}:~# ' + typed + '_'])
    if screen == 'blank':
        return Image.new('RGB', (720, 400), (0, 0, 0))
    now = dt.datetime.now()
    if screen in ('winlock', 'picture'):
        img = Image.new('RGB', (1024, 768))
        d = ImageDraw.Draw(img)
        seed = random.Random(now.strftime('%Y%m%d') + name)
        base = (seed.randint(10, 80), seed.randint(40, 120), seed.randint(90, 180))
        for y in range(768):
            k = y / 768
            d.line([(0, y), (1024, y)], fill=(int(base[0] * (1 - k) + 10 * k), int(base[1] * (1 - k) + 20 * k), int(base[2] * (1 - k) + 40 * k)))
        if screen == 'picture':
            for _ in range(6):
                x, y, r = seed.randint(0, 1024), seed.randint(0, 768), seed.randint(40, 160)
                d.ellipse([x - r, y - r, x + r, y + r], fill=(seed.randint(60, 200), seed.randint(60, 200), seed.randint(60, 200)))
        d.text((60, 520), now.strftime('%H:%M'), fill=(255, 255, 255), font=_font(96, True))
        d.text((64, 640), now.strftime('%A, %B %d'), fill=(255, 255, 255), font=_font(32, True))
        if screen == 'winlock':
            d.text((330, 380), 'Press Ctrl+Alt+Delete to unlock.', fill=(255, 255, 255), font=_font(26, True))
        return img
    if screen == 'windesk':
        img = Image.new('RGB', (1024, 768), (0, 90, 140))
        d = ImageDraw.Draw(img)
        d.rectangle([0, 728, 1024, 768], fill=(30, 30, 30))
        d.rectangle([120, 90, 880, 600], fill=(240, 240, 240), outline=(80, 80, 80))
        d.rectangle([120, 90, 880, 122], fill=(255, 255, 255))
        d.text((132, 96), 'Server Manager', fill=(0, 0, 0), font=_font(16, True))
        d.text((150, 150), 'Dashboard     Local Server     All Servers     AD DS     DNS', fill=(20, 20, 20), font=_font(16, True))
        d.text((150, 200), f'WELCOME TO SERVER MANAGER  {name.upper()}', fill=(20, 20, 20), font=_font(20, True))
        d.text((150, 260), 'Configure this local server', fill=(0, 80, 160), font=_font(16, True))
        d.text((900, 738), now.strftime('%H:%M'), fill=(255, 255, 255), font=_font(14, True))
        return img
    return Image.new('RGB', (640, 480), (128, 0, 128))


def on_key(v: dict, keysym: int, down: bool, mods: set[int]) -> None:
    if not down:
        return
    scr = v['screen']
    if scr == 'blank':
        v['screen'] = v.get('wake_to') or 'shell'
        return
    if scr == 'winlock' and keysym in (0xffff, 0xff9f) and {0xffe3, 0xffe9} <= mods:
        v['screen'] = 'windesk'
        return
    if scr == 'windesk' and keysym in (0x6c, 0x4c) and (mods & {0xffeb, 0xffec}):
        v['screen'] = 'winlock'
        return
    if scr not in ('login', 'shell'):
        return
    if keysym == 0xff0d:                              # Enter
        line, v['typed'] = v['typed'], ''
        if scr == 'login' and v['login_step'] == 'user':
            v['user'], v['login_step'] = line, 'password'
        elif scr == 'login':
            v['screen'], v['login_step'] = 'shell', 'user'
        elif scr == 'shell' and line.strip() in ('exit', 'logout'):
            v['screen'] = 'login'
        return
    if keysym == 0xff08:                              # BackSpace
        v['typed'] = v['typed'][:-1]
        return
    if 0x20 <= keysym <= 0x7e:
        if v['screen'] == 'login' and v['login_step'] == 'password':
            v['typed'] += '*'
        else:
            v['typed'] += chr(keysym)


# --------------------------------------------------------------- console
async def console(request: web.Request):
    sid = request.query.get('sessionID', '')
    v = state['vms'].get(request.query.get('vm', ''))
    ws = web.WebSocketResponse(protocols=('binary',), max_msg_size=0)
    await ws.prepare(request)
    if sid not in state['sessions'] or not v or v['state'] != 'RUNNING' or NODES[v['node']]['uuid'] != request.match_info['node']:
        await ws.close()
        return ws
    buf = bytearray()
    q: asyncio.Queue = asyncio.Queue()

    async def reader():
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                await q.put(msg.data)
            elif msg.type == WSMsgType.ERROR:
                break
        await q.put(None)

    rt = asyncio.create_task(reader())

    async def read(n: int) -> bytes:
        while len(buf) < n:
            chunk = await q.get()
            if chunk is None:
                raise ConnectionError('closed')
            buf.extend(chunk)
        out = bytes(buf[:n])
        del buf[:n]
        return out

    try:
        await ws.send_bytes(b'RFB 003.008\n')
        await read(12)
        await ws.send_bytes(b'\x01\x01')
        await read(1)
        await ws.send_bytes(struct.pack('>I', 0))
        await read(1)                                       # ClientInit
        img = render(v)
        w, h = img.size
        name = f'QEMU ({v["name"]})'.encode()
        pf = struct.pack('>BBBBHHHBBB3x', 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
        await ws.send_bytes(struct.pack('>HH', w, h) + pf + struct.pack('>I', len(name)) + name)
        rawmode = 'BGRX'
        mods: set[int] = set()
        sent_version, sent_size = None, (w, h)
        pending_incremental = False
        while True:
            try:
                (mtype,) = struct.unpack('>B', await asyncio.wait_for(read(1), 0.2))
            except asyncio.TimeoutError:
                mtype = None
            if mtype == 0:                                  # SetPixelFormat
                data = await read(19)
                bpp, _, big, _, _, _, _, rs, gs, bs = struct.unpack('>BBBBHHHBBB', data[3:16])
                rawmode = 'RGBX' if (rs, gs, bs) == (0, 8, 16) else 'BGRX'
            elif mtype == 2:                                # SetEncodings
                _, n = struct.unpack('>BH', await read(3))
                await read(4 * n)
            elif mtype == 3:                                # FramebufferUpdateRequest
                inc, *_ = struct.unpack('>BHHHH', await read(9))
                if not inc:
                    sent_version = None
                pending_incremental = True
            elif mtype == 4:                                # KeyEvent
                down, _, _, key = struct.unpack('>BBBI', await read(7))
                if key in (0xffe1, 0xffe2, 0xffe3, 0xffe4, 0xffe9, 0xffea, 0xffeb, 0xffec):
                    (mods.add if down else mods.discard)(key)
                on_key(v, key, bool(down), mods)
                v['version'] += 1
            elif mtype == 5:                                # PointerEvent
                await read(5)
                if v['screen'] == 'blank':
                    v['screen'] = v.get('wake_to') or 'shell'
                    v['version'] += 1
            elif mtype == 6:                                # ClientCutText
                await read(3)
                (n,) = struct.unpack('>I', await read(4))
                await read(n)
            elif mtype is not None:
                raise ConnectionError(f'unknown client message {mtype}')
            # clock screens change every minute
            version = (v['version'], v['screen'], dt.datetime.now().strftime('%H%M'))
            if pending_incremental and version != sent_version:
                img = render(v)
                rects = b''
                if img.size != sent_size:                   # DesktopSize pseudo-rectangle
                    rects += struct.pack('>HHHHi', 0, 0, img.width, img.height, -223)
                    sent_size = img.size
                data = img.tobytes('raw', rawmode)
                rects += struct.pack('>HHHHi', 0, 0, img.width, img.height, 0) + data
                count = 2 if rects[8:12] == struct.pack('>i', -223) else 1
                await ws.send_bytes(struct.pack('>BxH', 0, count) + rects)
                sent_version, pending_incremental = version, False
    except (ConnectionError, ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        rt.cancel()
        await ws.close()
    return ws


# ------------------------------------------------------------------- TLS
def _tls() -> ssl.SSLContext:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'fake-hypercore')])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1))
            .not_valid_after(dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=365))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName('fakehc'), x509.DNSName('localhost'),
                                                        x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), False)
            .sign(key, hashes.SHA256()))
    d = tempfile.mkdtemp()
    cp, kp = os.path.join(d, 'c.pem'), os.path.join(d, 'k.pem')
    open(cp, 'wb').write(cert.public_bytes(serialization.Encoding.PEM))
    open(kp, 'wb').write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(cp, kp)
    return ctx


def make_app() -> web.Application:
    app = web.Application(middlewares=[rest_auth])
    r = app.router
    for base in ('/rest/v1',):
        r.add_post(f'{base}/login', login)
        r.add_post(f'{base}/logout', logout)
        r.add_get(f'{base}/ping', r_ping)
        r.add_get(f'{base}/Cluster', r_cluster)
        r.add_get(f'{base}/Node', r_nodes)
        r.add_get(f'{base}/VirDomain', r_vms)
        r.add_get(f'{base}/VirDomain/{{uuid}}', r_vm)
        r.add_get(f'{base}/VirDomainStats', r_stats)
        r.add_get(f'{base}/VirDomainSnapshot', r_snapshots)
        r.add_get(f'{base}/Condition', r_conditions)
        r.add_get(f'{base}/TimeZone', r_tz)
        r.add_get(f'{base}/TaskTag/{{tag}}', r_task)
        r.add_post(f'{base}/VirDomain/action', r_action)
        r.add_patch(f'{base}/VirDomain/{{uuid}}', r_vm_patch)
        r.add_post(f'{base}/VirDomain', r_vm_create)
        r.add_post(f'{base}/VirDomain/{{uuid}}/clone', r_clone)
        r.add_get(f'{base}/VirtualDisk', r_images)
        r.add_post(f'{base}/VirtualDisk/{{uuid}}/attach', r_image_attach)
        r.add_delete(f'{base}/VirDomain/{{uuid}}', r_vm_delete)
        for dev in ('VirDomainBlockDevice', 'VirDomainNetDevice'):
            r.add_post(f'{base}/{dev}', r_dev_post)
            r.add_patch(f'{base}/{dev}/{{uuid}}', r_dev_patch)
            r.add_delete(f'{base}/{dev}/{{uuid}}', r_dev_delete)
        r.add_get(f'{base}/ISO', r_isos)
        r.add_get(f'{base}/VirDomainSnapshotSchedule', r_schedules)
        r.add_get(f'{base}/VirDomainReplication', r_reps)
        r.add_patch(f'{base}/VirDomainReplication/{{uuid}}', r_rep_patch)
        r.add_get(f'{base}/RemoteClusterConnection', r_remote_conns)
        r.add_post(f'{base}/VirDomainReplication', r_rep_create)
        r.add_post(f'{base}/VirDomainSnapshot', r_snapshot)
    r.add_get('/update/update_status.json', r_update)
    r.add_get('/node/{node}/console', console)
    r.add_post('/fake/vm/{uuid}', fake_vm)
    r.add_post('/fake/conditions', fake_conditions)
    r.add_get('/fake/state', fake_state)
    r.add_get('/fake/log', fake_log)
    return app


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=443)
    ap.add_argument('--plain', action='store_true', help='HTTP instead of HTTPS (unit tests)')
    a = ap.parse_args()
    web.run_app(make_app(), port=a.port, ssl_context=None if a.plain else _tls())
