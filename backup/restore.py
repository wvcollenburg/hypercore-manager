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
Test restores (testrestore.py) pass extra_tags, a description, and on the network level a VLAN for every NIC.
Step 0, on by default: the backing chain of the point is re-hashed on the proxy first.

Only one restore or backup runs per proxy at a time (the proxy VM cannot be snapshotted while
read-only clones hang off it).
"""
from __future__ import annotations

import logging
import time
import uuid as uuidlib
from datetime import datetime, timezone

import cluster_registry
import db
import incremental
import jobs
import vm_definition
from hypercore import HyperCoreClient, HyperCoreError
from incremental import (IncrementalError, _delete_clones, _safe_delete_snapshot, clone_onto, make_way, pick_proxy, proxy_client_for, proxy_lock,
                         take_snapshot, vm_lock, wait_task)
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
    """True = set the original MAC addresses on the restored VM. Only on explicit request
    ('original'); the default is a new address, whether or not the source VM still exists."""
    return policy == 'original'


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


def _cluster_name(cid: str) -> str:
    """The target cluster's name for the result message; never fails a finished restore."""
    try:
        c = cluster_registry.get(cid)
        return c.name if c else cid
    except Exception:                                       # noqa: BLE001
        return cid


def run_restore(restore_id: int) -> None:
    r = db.get_restore(restore_id)
    if r is None:
        return
    try:
        _run(r)
    except (IncrementalError, HyperCoreError, ProxyError, ValueError, vm_definition.UnknownMachineType) as e:
        db.update_restore(restore_id, status='ERROR', message=str(e) + _discard_failed_vm(restore_id), finished_at=_now(), progress=None)
        log.error('[restore %s] %s', restore_id, e)
    except Exception as e:                                  # noqa: BLE001
        db.update_restore(restore_id, status='ERROR', message=f'Unexpected error: {e}' + _discard_failed_vm(restore_id),
                          finished_at=_now(), progress=None)
        log.exception('[restore %s] unexpected error', restore_id)


def _record_staging(rid: int, uuids: list[str]) -> None:
    """Staging disks on the proxy VM that this restore still owns (migration 056); never fails the restore."""
    try:
        db.update_restore(rid, staging=list(uuids))
    except Exception as e:                                  # noqa: BLE001 — e.g. before migration 056
        log.warning('[restore %s] staging disks not recorded: %s', rid, e)


def remove_staging(hc: HyperCoreClient, uuids: list[str]) -> list[str]:
    """Delete staging disks still attached to a proxy VM; a disk that is gone already counts as removed.
    Returns the ones that could not be removed."""
    present = []
    for u in uuids or []:
        try:
            if hc.get_block_device(u):
                present.append(u)
        except HyperCoreError as e:
            if e.status != 404:
                present.append(u)
    return _delete_clones(hc, present) if present else []


def _discard_failed_vm(rid: int, require_unfinished: bool = False) -> str:
    """Delete the VM a failed restore created (half-built, it also blocks a retry with the same name). Only the
    VM recorded on this restore, and only while it is off. With require_unfinished, only when the restore had
    not reached its last step (an interrupted restore). Returns a note for the message ('' = nothing done)."""
    try:
        r = db.get_restore(rid) or {}
        vm_uuid = r.get('created_uuid')
        if not vm_uuid or (r.get('kind') or 'vm') != 'vm' or (require_unfinished and (r.get('progress') or {}).get('phase') == 'finish'):
            return ''
        hc = jobs.client_for(r['cluster_id'])
        try:
            vm = hc.get_vm(vm_uuid)
        except HyperCoreError:
            return ''                                       # gone already
        if (vm.get('state') or '').upper() not in ('SHUTOFF', 'SHUTDOWN'):
            return f"; the restored VM '{vm.get('name')}' is running and was kept"
        t = hc.delete_vm(vm_uuid)
        if t:
            wait_task(hc, t, 'delete the half-restored VM', 600)
        log.warning('[restore %s] half-restored VM %s deleted', rid, vm_uuid)
        return '; the half-restored VM was deleted'
    except Exception as e:                                  # noqa: BLE001 — the restore's own error is what matters
        log.warning('[restore %s] half-restored VM not deleted: %s', rid, e)
        return f'; the half-restored VM could not be deleted ({e}), delete it on the cluster'


def cleanup_interrupted(restore_ids: list[int]) -> None:
    """After a restart: the staging disks and the half-built VM of restores the restart interrupted."""
    for rid in restore_ids:
        note = _discard_failed_vm(rid, require_unfinished=True)
        if note:
            try:
                r = db.get_restore(rid) or {}
                db.update_restore(rid, message=(r.get('message') or 'Interrupted by a restart.') + note)
            except Exception:                               # noqa: BLE001
                log.warning('[restore %s] note not recorded', rid, exc_info=True)
    sweep_staging()


def sweep_staging() -> int:
    """Daily and after a restart: staging disks that finished or failed restores left on proxy VMs."""
    removed = 0
    try:
        rows = db.restores_with_staging()
    except Exception:                                       # noqa: BLE001 — before migration 056
        log.debug('staging sweep skipped', exc_info=True)
        return 0
    for r in rows:
        try:
            left = remove_staging(jobs.client_for(r['cluster_id']), list(r['staging']))
        except HyperCoreError as e:
            log.warning('[restore %s] staging disks not removed yet: %s', r['id'], e)
            continue
        removed += len(r['staging']) - len(left)
        _record_staging(r['id'], left)
    return removed


def _run(r: dict) -> None:
    rid = r['id']; params = r.get('params') or {}
    point = db.get_point(r['point_id'])
    chain = db.get_chain(point['chain_id']) if point else None
    if not point or not chain:
        raise IncrementalError('restore point no longer exists')
    share = db.get_share(chain['target_id'], with_secret=True) if chain.get('target_id') else None
    if not share:
        raise IncrementalError('the backup target of this chain no longer exists')
    if jobs.is_nfs(share['uri_base']):
        raise IncrementalError(jobs.smb_only_error('A VM restore'))
    definition = point.get('definition') or {}
    disks, utils = plan_devices(definition)
    if not disks:
        raise IncrementalError('the point has no data disk')
    # where the VM is created: the chain's own cluster, or another cluster of its folder. The chain lives on
    # the folder's share, so the target cluster's own proxy reads it and stages the disks there.
    cid = r.get('cluster_id') or chain['cluster_id']
    hc = jobs.client_for(cid)
    pf = jobs.preflight(hc)
    if not pf['ok']:
        raise IncrementalError(f"{pf['detail']}. {jobs.DEFERRED_HINT.get(pf['kind'], '')}".strip())
    proxy = pick_proxy(cid)
    pc = proxy_client_for(proxy)
    target = {'uri': share['uri_base'], 'username': share.get('username'), 'password': share.get('password')}
    name = (r.get('target_name') or f"{chain['vm_name']}-restored").strip()
    existing = {(v.get('name') or '').lower(): v for v in hc.list_vms_full()}
    if name.lower() in existing:
        raise IncrementalError(f"a VM named '{name}' already exists on the cluster; choose another name")
    source_exists = any(v.get('uuid') == definition.get('uuid') for v in existing.values())
    if not source_exists and cid != chain['cluster_id']:
        try:                                       # the source VM may still run on its own cluster
            source_exists = any(v.get('uuid') == definition.get('uuid') for v in jobs.client_for(chain['cluster_id']).list_vms())
        except HyperCoreError:
            source_exists = True                   # unknown: assume it runs, so its MAC address is not reused by default

    # 0. integrity of the backing chain, before anything is created on the cluster. The weekly
    #    verify may be days old; a delta damaged since then would restore silently corrupt.
    verify = bool(params.get('verify', True))
    if verify:
        db.update_restore(rid, message='verifying chain', progress={'phase': 'verify', 'done': 0, 'total': 0})
        res_v = incremental.verify_chain(chain, share, pc, upto_seq=point['seq'],
                                         on_progress=lambda p: db.update_restore(rid, message=incremental.verify_progress_text(p),
                                                                                 progress={'phase': 'verify', 'done': int(p.get('bytes_done') or 0) >> 20,
                                                                                           'total': int(p.get('bytes_total') or 0) >> 20}))
        if not res_v['ok']:
            detail = '; '.join(f'{d}: {m}' for d, m in res_v['failed'].items()) or 'the proxy reported a mismatch'
            raise IncrementalError(f'chain integrity check failed, nothing restored: {detail}')

    # 1. diskless import directory: NVRAM/vTPM with fresh file names + generated XML
    db.update_restore(rid, message='preparing definition', progress={'phase': 'prepare'})
    new_uuid = str(uuidlib.uuid4())
    utility_files = {u['uuid']: str(uuidlib.uuid4()) for u in utils}
    xml = render_definition(definition, new_uuid, utility_files)
    dirname = f"{jobs.safe_name(name)}_{time.strftime('%Y%m%d-%H%M%S')}"
    out_dir = f"{chain['folder_slug']}/_restore/{dirname}"
    pc.wait(pc.submit('flatten', {'target': target, 'chain_dir': chain['chain_dir'], 'seq': point['seq'], 'out_dir': out_dir,
                                  'only_uuids': list(utility_files), 'files': {k: f'{v}.qcow2' for k, v in utility_files.items()},
                                  'sizes': {u['uuid']: int(u.get('capacity') or 0) for u in utils if u.get('capacity')},
                                  'definition_xml': xml, 'definition_name': f'{dirname}.xml'}), timeout=3600)

    # 2. import the diskless VM
    db.update_restore(rid, message='importing definition', progress={'phase': 'import'})
    source_uri = jobs.build_path_uri(f"{share['uri_base'].rstrip('/')}/{out_dir}", share.get('username') or None, share.get('password') or None)
    tags = ','.join(t for t in [*(definition.get('tags') or '').split(','), *(params.get('extra_tags') or [])] if t.strip())
    tag, created = hc.import_vm(source_uri, name=name, description=params.get('description') or definition.get('description') or '',
                                tags=tags, definition_file=f'{dirname}.xml')
    if created:
        db.update_restore(rid, created_uuid=created)       # known before the import finishes: a failure removes it
    wait_task(hc, tag, 'import', 3600)
    new_vm = hc.get_vm(created) if created else next((v for v in hc.list_vms_full() if (v.get('name') or '').lower() == name.lower()), None)
    if not new_vm:
        raise IncrementalError('import finished but the restored VM was not found')
    db.update_restore(rid, created_uuid=new_vm['uuid'])

    # 3.–4. staging disks on the proxy, filled from the point, cloned onto the restored VM
    disk_map: dict[str, str] = {}
    make_way(proxy['id'], 'a restore')
    with vm_lock(cid, proxy['vm_uuid']), proxy_lock(proxy['id']):
        staging: list[tuple[dict, str]] = []
        snap_uuid = None
        try:
            for d in disks:
                cap = int(d['capacity'])
                # staging disks take heavy one-off writes: keep them off the SSD tier (factor 0)
                stag, su = jobs.retry_transient(lambda: hc.create_block_device(proxy['vm_uuid'], cap, tiering=0), 'create staging disk')
                staging.append((d, su))
                _record_staging(rid, [s for _, s in staging])     # a restart or a failed cleanup must not lose them
                wait_task(hc, stag, 'create staging disk', 600)
            db.update_restore(rid, message=f'writing {len(disks)} disk(s) to the proxy', progress={'phase': 'write', 'done': 0, 'total': 0})
            res = pc.wait(pc.submit('stage', {'target': target, 'chain_dir': chain['chain_dir'], 'seq': point['seq'],
                                              'devices': [{'uuid': d['uuid'], 'staging_clone_uuid': su} for d, su in staging]}),
                          on_progress=lambda p: db.update_restore(rid, message=f"writing disks: {p.get('blocks_done', 0)}/{p.get('blocks_total', 0)} MiB",
                                                                  progress={'phase': 'write', 'done': int(p.get('blocks_done') or 0),
                                                                            'total': int(p.get('blocks_total') or 0)}),
                          timeout=8 * 3600)['result']
            db.update_restore(rid, message='attaching disks to the restored VM', progress={'phase': 'attach'})
            snap_uuid = take_snapshot(hc, proxy['vm_uuid'], f'hcm-restore-{rid}', quiesce=False, retain_days=1)['uuid']
            for d, su in staging:
                # the disk goes back on the bus it had (IDE as IDE, SCSI as SCSI): the VM is off, so even
                # IDE may be attached now. A refusal is an error, never a silent switch to another bus.
                dev_type = d.get('type') if d.get('type') in HyperCoreClient.DISK_TYPES else 'VIRTIO_DISK'
                tiering = int(d['tieringPriorityFactor']) if d.get('tieringPriorityFactor') is not None else 8
                try:
                    new_dev = clone_onto(hc, su, snap_uuid, new_vm['uuid'], int(d['capacity']), read_only=False, dev_type=dev_type, tiering=tiering)
                except (HyperCoreError, IncrementalError) as e:
                    raise IncrementalError(f"disk {d['uuid'][:8]} could not be attached as {dev_type}: {e}") from e
                disk_map[d['uuid']] = new_dev
                # the clone inherits the staging disk's tiering (0) whatever the template says: put the
                # source's factor back, or the restored VM runs from an uncached disk
                fields = {'tieringPriorityFactor': tiering}
                if d.get('cacheMode'):
                    fields['cacheMode'] = d['cacheMode']       # recorded in the point; the clone gets HyperCore's default
                if d.get('name'):
                    fields['name'] = d['name']                 # an orchestrator's identity for the volume (CSI)
                t = jobs.retry_transient(lambda: hc.patch_block_device(new_dev, **fields), 'disk settings')
                if t:
                    wait_task(hc, t, 'disk settings', 300)
        finally:
            _safe_delete_snapshot(hc, snap_uuid, 'restore staging')
            if staging:
                _record_staging(rid, _delete_clones(hc, [su for _, su in staging]))   # the daily sweep retries what is left

    # 5. boot order, NICs, start
    db.update_restore(rid, message='boot order and network', progress={'phase': 'finish'})
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
        if old_nic.get('type') and old_nic['type'] != new_nic.get('type'):
            # the restore template always renders a virtio NIC: an E1000 or RTL8139 guest would come back without network
            fields['type'] = old_nic['type']
        if params.get('vlan') is not None:
            fields['vlan'] = int(params['vlan'])         # a test restore on the network level: the isolated test VLAN
        elif old_nic.get('vlan') is not None:
            fields['vlan'] = old_nic['vlan']
        if use_original_mac and old_nic.get('macAddress'):
            fields['macAddress'] = old_nic['macAddress']
        t = hc.patch_netdev(new_nic['uuid'], **fields)
        if t:
            wait_task(hc, t, 'network device', 300)
    not_started = None
    if params.get('start'):
        # the VM is complete at this point; a refused start (typically the original MAC address is
        # in use by the still-running source VM) is reported, not treated as a failed restore
        try:
            t = hc.vm_action(new_vm['uuid'], 'START')
            if t:
                wait_task(hc, t, 'start', 600)
        except (HyperCoreError, IncrementalError) as e:
            not_started = str(e)
            log.warning('[restore %s] restored VM not started: %s', rid, e)
    try:
        pc.wait(pc.submit('sweep', {'target': target, 'folder': chain['folder_slug'], 'older_than_hours': 0}), timeout=600)
    except ProxyError as e:
        log.warning('[restore %s] restore directory cleanup failed: %s', rid, e)
    bytes_written = res.get('bytes_written', 0)
    db.update_restore(rid, status='COMPLETE', finished_at=_now(), progress=None,
                      message=f"restored as '{name}'" + (f' on {_cluster_name(cid)}' if cid != chain['cluster_id'] else '')
                              + f" ({len(disks)} disk(s), {bytes_written >> 20} MiB written); chain "
                              f"{'verified' if verify else 'not verified (skipped)'}; MAC "
                              f"{'original' if use_original_mac else 'new'}, NICs {'connected' if connected else 'disconnected'}"
                              + (f'. Not started: {not_started} Give it a new MAC address or stop the source VM, then start it.' if not_started
                                 else (', started' if params.get('start') else '')))
    log.info('[restore %s] done: %s', rid, name)
