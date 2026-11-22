"""
vm_rows.py — one row per VM from vm_metrics records, pure.

vm_metrics carries the node a VM runs on as tags (node_uuid, node_peer_id), next to its name, first
tag and VLAN. A VM that live-migrates inside the queried range therefore has one series per node it
ran on. The row describes the VM as of its NEWEST sample: taking the first series seen (the lowest
node in tag order) kept a migrated VM under its old node for as long as that series stayed in range,
which emptied the newest node in the "group by node" view.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

# tags that describe the VM at the time of a sample
_AS_OF = (('name', 'vm_name', 'Unknown'), ('node_uuid', 'node_uuid', 'unknown'),
          ('node_peer_id', 'node_peer_id', 'unknown'), ('tag', 'vm_tag', ''), ('vlan', 'vm_vlan', '0'))


def fold(records: Iterable[tuple[datetime, dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    """{vm_uuid: row} from (time, values) pairs in any order. Each row has uuid, name, node_uuid,
    node_peer_id, tag, vlan (all as of the newest sample) and history, oldest first. Samples taken
    while a VM had no node (mid-migration) are left out."""
    vms: dict[str, dict[str, Any]] = {}
    newest: dict[str, datetime] = {}
    for when, values in records:
        if values.get('node_uuid', 'unknown') == 'unknown':
            continue
        uuid = values.get('vm_uuid', 'unknown')
        vm = vms.get(uuid)
        if vm is None:
            vm = vms[uuid] = {'uuid': uuid, 'history': []}
        if uuid not in newest or when >= newest[uuid]:
            newest[uuid] = when
            for key, tag, default in _AS_OF:
                vm[key] = values.get(tag, default)
        vm['history'].append({'time': when.isoformat(), 'cpu_usage': values.get('cpu_usage'),
                              'rx_bit_rate': values.get('rx_bit_rate'), 'tx_bit_rate': values.get('tx_bit_rate')})
    for vm in vms.values():
        vm['history'].sort(key=lambda h: h['time'])
    return vms


def newest_per(records: Iterable[tuple[datetime, dict[str, Any], Any]], key: str = 'vm_uuid') -> dict[str, Any]:
    """{key value: value} from (time, values, value) triples: per VM the value of its NEWEST sample. A VM that
    migrated in the window has a last() per node it ran on; the first one seen was its old node's."""
    out: dict[str, Any] = {}
    when_of: dict[str, datetime] = {}
    for when, values, value in records:
        k = values.get(key, 'unknown')
        if k not in when_of or when >= when_of[k]:
            when_of[k], out[k] = when, value
    return out


# tags that describe a virtual disk at the time of a sample
_VSD_AS_OF = (('name', 'vsd_name', 'unknown'), ('type', 'vsd_type', 'unknown'), ('vm_uuid', 'vm_uuid', 'unknown'),
              ('vm_name', 'vm_name', 'Unknown'), ('node_uuid', 'node_uuid', 'unknown'))
_VSD_FIELDS = ('read_kbps', 'write_kbps', 'read_latency_us', 'write_latency_us', 'read_iops', 'write_iops', 'capacity_bytes')


def fold_vsds(records: Iterable[tuple[datetime, dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    """{vsd_uuid: row} from vsd_metrics records, like fold(): a disk's VM moves between nodes, so it has a series
    per node; the row describes it as of its newest sample (the first series seen kept it on its old node) and
    its history is one series, oldest first."""
    vsds: dict[str, dict[str, Any]] = {}
    newest: dict[str, datetime] = {}
    for when, values in records:
        uuid = values.get('vsd_uuid', 'unknown')
        vsd = vsds.get(uuid)
        if vsd is None:
            vsd = vsds[uuid] = {'uuid': uuid, 'history': []}
        if uuid not in newest or when >= newest[uuid]:
            newest[uuid] = when
            for k, tag, default in _VSD_AS_OF:
                vsd[k] = values.get(tag, default)
        vsd['history'].append(dict({'time': when.isoformat()}, **{f: values.get(f) for f in _VSD_FIELDS}))
    for vsd in vsds.values():
        vsd['history'].sort(key=lambda h: h['time'])
    return vsds
