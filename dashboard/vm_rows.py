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
