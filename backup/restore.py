"""
restore.py — restore a VM from a backup point.

Fast path (verified as E9b):
  1. the proxy writes the point's NVRAM and vTPM images plus a generated, *diskless* libvirt
     definition into a temporary restore directory on the target
  2. HyperCore imports that directory: a VM with firmware state, TPM state, NICs, no data disks
     (seconds, no bulk data through the import path)
  3. for every data disk the backup service creates a writable staging disk on the proxy's VM
     and the proxy fills it from the point (allocated blocks only, at Scribe speed)
  4. the proxy VM is snapshotted (no clones may be attached at that moment) and the staging
     disks are cloned onto the restored VM; staging disks and snapshot are removed
  5. boot order, MAC/VLAN policy, optional start

Only one restore or backup runs per proxy at a time (the proxy VM cannot be snapshotted while
read-only clones hang off it).
"""
from __future__ import annotations

import logging
import time
import uuid as uuidlib
from datetime import datetime, timezone

import db
import incremental
import jobs
import vm_definition
from hypercore import HyperCoreClient, HyperCoreError
from incremental import IncrementalError, _delete_clones, _safe_delete_snapshot, pick_proxy, proxy_client_for, proxy_lock, wait_snapshot, wait_task
from proxy_client import ProxyError

log = logging.getLogger('backup.restore')


def _now() -> datetime:
    return datetime.now(timezone.utc)


def os_profile(definition: dict) -> str:
    return vm_definition.os_profile(definition.get('operatingSystem'))


def render_definition(definition: dict, new_uuid: str, utility_files: dict[str, str]) -> str:
    """Diskless libvirt XML for the point's VM; learned templates win over shipped ones."""
    learned = db.get_os_template(definition.get('machineType') or '', os_profile(definition))
    return vm_definition.render(definition, utility_files, new_uuid=new_uuid, template=learned, include_disks=False)


def mac_decision(policy: str, source_vm_exists: bool) -> bool:
    """True = set the original MAC addresses on the restored VM."""
    if policy == 'original':
        return True
    if policy == 'new':
        return False
    return not source_vm_exists        # auto: original when the source VM is gone


def plan_devices(definition: dict) -> tuple[list[dict], list[dict]]:
    """(data disks, utility devices) of the backed-up VM from its stored definition."""
    disks = [b for b in definition.get('blockDevs', []) if b.get('type') in HyperCoreClient.DISK_TYPES]
    utils = [b for b in definition.get('blockDevs', []) if b.get('type') in HyperCoreClient.UTILITY_TYPES]
    return disks, utils


def boot_order(definition: dict, disk_map: dict[str, str], new_vm: dict) -> list[str]:
    """Original boot order translated to the restored VM's devices; a CD-ROM of the new VM keeps
    its place at the end."""
    order = []
    for u in definition.get('bootDevices') or []:
        if u in disk_map:
            order.append(disk_map[u])
    for b in new_vm.get('blockDevs', []):
        if b.get('type') == 'IDE_CDROM' and b['uuid'] not in order:
            order.append(b['uuid'])
    return order


def run_restore(restore_id: int) -> None:
    r = db.get_restore(restore_id)
    if r is None:
        return
    try:
        _run(r)
    except (IncrementalError, HyperCoreError, ProxyError, ValueError, vm_definition.UnknownMachineType) as e:
        db.update_restore(restore_id, status='ERROR', message=str(e), finished_at=_now())
        log.error('[restore %s] %s', restore_id, e)
    except Exception as e:                                  # noqa: BLE001
        db.update_restore(restore_id, status='ERROR', message=f'Unexpected error: {e}', finished_at=_now())
        log.exception('[restore %s] unexpected error', restore_id)


def _run(r: dict) -> None:
    rid = r['id']; params = r.get('params') or {}
    point = db.get_point(r['point_id'])
    chain = db.get_chain(point['chain_id']) if point else None
    if not point or not chain:
        raise IncrementalError('restore point no longer exists')
    share = db.get_share(chain['target_id'], with_secret=True) if chain.get('target_id') else None
    if not share:
        raise IncrementalError('the backup target of this chain no longer exists')
    definition = point.get('definition') or {}
    disks, utils = plan_devices(definition)
    if not disks:
        raise IncrementalError('the point has no data disk')
    hc = jobs.client_for(chain['cluster_id'])
    proxy = pick_proxy(chain['cluster_id'])
    pc = proxy_client_for(proxy)
    target = {'uri': share['uri_base'], 'username': share.get('username'), 'password': share.get('password')}
    name = (r.get('target_name') or f"{chain['vm_name']}-restored").strip()
    existing = {(v.get('name') or '').lower(): v for v in hc.list_vms_full()}
    if name.lower() in existing:
        raise IncrementalError(f"a VM named '{name}' already exists on the cluster; choose another name")
    source_exists = any(v.get('uuid') == definition.get('uuid') for v in existing.values())

    # 1. diskless import directory: NVRAM/vTPM with fresh file names + generated XML
    db.update_restore(rid, message='preparing definition')
    new_uuid = str(uuidlib.uuid4())
    utility_files = {u['uuid']: str(uuidlib.uuid4()) for u in utils}
    xml = render_definition(definition, new_uuid, utility_files)
    dirname = f"{jobs.safe_name(name)}_{time.strftime('%Y%m%d-%H%M%S')}"
    out_dir = f"{chain['folder_slug']}/_restore/{dirname}"
    pc.wait(pc.submit('flatten', {'target': target, 'chain_dir': chain['chain_dir'], 'seq': point['seq'], 'out_dir': out_dir,
                                  'only_uuids': list(utility_files), 'files': {k: f'{v}.qcow2' for k, v in utility_files.items()},
                                  'definition_xml': xml, 'definition_name': f'{dirname}.xml'}), timeout=3600)

    # 2. import the diskless VM
    db.update_restore(rid, message='importing definition')
    source_uri = jobs.build_path_uri(f"{share['uri_base'].rstrip('/')}/{out_dir}", share.get('username') or None, share.get('password') or None)
    tag, created = hc.import_vm(source_uri, name=name, description=definition.get('description') or '', tags=definition.get('tags') or '',
                                definition_file=f'{dirname}.xml')
    wait_task(hc, tag, 'import', 3600)
    new_vm = hc.get_vm(created) if created else next((v for v in hc.list_vms_full() if (v.get('name') or '').lower() == name.lower()), None)
    if not new_vm:
        raise IncrementalError('import finished but the restored VM was not found')
    db.update_restore(rid, created_uuid=new_vm['uuid'])

    # 3.–4. staging disks on the proxy, filled from the point, cloned onto the restored VM
    disk_map: dict[str, str] = {}
    with proxy_lock(proxy['id']):
        staging: list[tuple[dict, str]] = []
        snap_uuid = None
        try:
            for d in disks:
                cap = int(d['capacity'])
                stag, su = hc.create_block_device(proxy['vm_uuid'], cap)
                wait_task(hc, stag, 'create staging disk', 600)
                staging.append((d, su))
            db.update_restore(rid, message=f'writing {len(disks)} disk(s) to the proxy')
            res = pc.wait(pc.submit('stage', {'target': target, 'chain_dir': chain['chain_dir'], 'seq': point['seq'],
                                              'devices': [{'uuid': d['uuid'], 'staging_clone_uuid': su} for d, su in staging]}),
                          on_progress=lambda p: db.update_restore(rid, message=f"writing disks: {p.get('blocks_done', 0)}/{p.get('blocks_total', 0)} MiB"),
                          timeout=8 * 3600)['result']
            db.update_restore(rid, message='attaching disks to the restored VM')
            stag, snap_uuid = hc.create_snapshot(proxy['vm_uuid'], f'hcm-restore-{rid}', quiesce=False)
            wait_task(hc, stag, 'proxy snapshot', 600)
            wait_snapshot(hc, snap_uuid)
            for d, su in staging:
                ctag, new_dev = hc.clone_device(su, snap_uuid, new_vm['uuid'], int(d['capacity']), read_only=False)
                wait_task(hc, ctag, 'clone staging disk', 600)
                disk_map[d['uuid']] = new_dev
        finally:
            _safe_delete_snapshot(hc, snap_uuid, 'restore staging')
            _delete_clones(hc, [su for _, su in staging])

    # 5. boot order, NICs, start
    new_vm = hc.get_vm(new_vm['uuid'])
    order = boot_order(definition, disk_map, new_vm)
    if order:
        t = hc.patch_vm(new_vm['uuid'], bootDevices=order)
        if t:
            wait_task(hc, t, 'boot order', 300)
    use_original_mac = mac_decision(params.get('mac_policy') or 'auto', source_exists)
    connected = bool(params.get('nic_connected', not source_exists))
    for new_nic, old_nic in zip(new_vm.get('netDevs', []), definition.get('netDevs', [])):
        fields = {'connected': connected}
        if old_nic.get('vlan') is not None:
            fields['vlan'] = old_nic['vlan']
        if use_original_mac and old_nic.get('macAddress'):
            fields['macAddress'] = old_nic['macAddress']
        t = hc.patch_netdev(new_nic['uuid'], **fields)
        if t:
            wait_task(hc, t, 'network device', 300)
    if params.get('start'):
        t = hc.vm_action(new_vm['uuid'], 'START')
        if t:
            wait_task(hc, t, 'start', 600)
    try:
        pc.wait(pc.submit('sweep', {'target': target, 'folder': chain['folder_slug'], 'older_than_hours': 0}), timeout=600)
    except ProxyError as e:
        log.warning('[restore %s] restore directory cleanup failed: %s', rid, e)
    bytes_written = res.get('bytes_written', 0)
    db.update_restore(rid, status='COMPLETE', finished_at=_now(),
                      message=f"restored as '{name}' ({len(disks)} disk(s), {bytes_written >> 20} MiB written); MAC "
                              f"{'original' if use_original_mac else 'new'}, NICs {'connected' if connected else 'disconnected'}"
                              f"{', started' if params.get('start') else ''}")
    log.info('[restore %s] done: %s', rid, name)
