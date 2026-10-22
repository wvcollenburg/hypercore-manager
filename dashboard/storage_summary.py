"""
storage_summary.py — the Infraviewer's storage overview: per-VM series plus cluster totals,
built from rows InfluxDB has already aggregated per VM per window.

The overview (nodes view, VMs view) only needs totals: IOPS and throughput summed over a VM's
disks, latency as the worst disk. Shipping every disk's history for that was 1 MB per request
and seconds of Python; the per-disk history now only travels when a VM detail panel asks for one
VM. Everything in here is pure so it is unit-tested without InfluxDB.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

SUM_FIELDS = ('read_iops', 'write_iops', 'read_kbps', 'write_kbps')   # summed over a VM's disks
MAX_FIELDS = ('read_latency_us', 'write_latency_us')                   # worst disk
FIELDS = SUM_FIELDS + MAX_FIELDS
UTILITY_TYPES = ('NVRAM', 'VTPM')


def _parse(t: str) -> datetime:
    return datetime.fromisoformat(t.replace('Z', '+00:00'))


def _fresh(t: str | None, now: datetime, stale_seconds: float) -> bool:
    return bool(t) and (now - _parse(t)).total_seconds() <= stale_seconds


def build(rows: Iterable[tuple], latest: Iterable[dict], now: datetime | None = None, stale_seconds: float = 120) -> dict:
    """
    rows   — (vm_uuid, vm_name, field, time_iso, value): one value per VM per field per window
             (sum for SUM_FIELDS, max for MAX_FIELDS; InfluxDB does that part)
    latest — one dict per disk with its newest sample: uuid, name, type, vm_uuid, vm_name,
             node_uuid, time, and the FIELDS
    Returns {'vms': [{uuid, name, history: [...], vsds: [...]}], 'cluster': {'history': [...]}}.
    A VM is listed when it has a fresh history point or a fresh disk sample; stale disks are
    dropped; the cluster series sums IOPS/throughput and takes the worst latency across VMs.
    """
    now = now or datetime.now(timezone.utc)
    vms: dict[str, dict] = {}
    series: dict[str, dict[str, dict]] = {}            # vm -> time -> point
    for vm_uuid, vm_name, field, t, value in rows:
        if field not in FIELDS or value is None:
            continue
        vms.setdefault(vm_uuid, {'uuid': vm_uuid, 'name': vm_name, 'history': [], 'vsds': []})
        if vm_name and vms[vm_uuid]['name'] in (None, '', 'Unknown'):
            vms[vm_uuid]['name'] = vm_name
        series.setdefault(vm_uuid, {}).setdefault(t, {'time': t})[field] = round(float(value), 1)
    for d in latest:
        if not _fresh(d.get('time'), now, stale_seconds):
            continue
        vm_uuid = d.get('vm_uuid') or 'unknown'
        vm = vms.setdefault(vm_uuid, {'uuid': vm_uuid, 'name': d.get('vm_name') or 'Unknown', 'history': [], 'vsds': []})
        vm['vsds'].append({'uuid': d.get('uuid'), 'name': d.get('name'), 'type': d.get('type'), 'node_uuid': d.get('node_uuid'),
                           'time': d['time'], **{f: d.get(f) for f in FIELDS}})
    cluster: dict[str, dict] = {}
    out = []
    for vm_uuid, vm in vms.items():
        hist = sorted(series.get(vm_uuid, {}).values(), key=lambda p: p['time'])
        vm['history'] = hist
        fresh_hist = bool(hist) and _fresh(hist[-1]['time'], now, stale_seconds)
        if not fresh_hist and not vm['vsds']:
            continue                                     # gone or stopped: no fresh sample anywhere
        vm['vsds'].sort(key=lambda v: (v.get('name') or '', v.get('uuid') or ''))
        out.append(vm)
        for p in hist:
            c = cluster.setdefault(p['time'], {'time': p['time']})
            for f in SUM_FIELDS:
                if p.get(f) is not None:
                    c[f] = (c.get(f) or 0) + p[f]
            for f in MAX_FIELDS:
                if p.get(f) is not None:
                    c[f] = max(c.get(f) or 0, p[f])
    out.sort(key=lambda v: (v['name'] or '', v['uuid']))
    return {'vms': out, 'cluster': {'history': sorted(cluster.values(), key=lambda p: p['time'])}}


def flux_queries(bucket: str, cluster_filter: str, hours: int, window: str, stale_seconds: float,
                 stop: datetime | None = None) -> tuple[str, str, str]:
    """(sum query, max query, latest query). The first two aggregate per disk per window, then
    per VM per window (sum, or max for latency); the third is one newest sample per disk.
    Both series queries get the same explicit `stop`: the last, partial window ends at the
    range's stop, and two queries issued milliseconds apart would otherwise put their final
    points at two different times, splitting one point into two half-filled ones."""
    stop = (stop or datetime.now(timezone.utc)).replace(microsecond=0)
    stop_s = stop.isoformat().replace('+00:00', 'Z')
    base = (f'from(bucket: "{bucket}")\n  |> range(start: -{int(hours)}h, stop: {stop_s})\n  {cluster_filter}\n'
            f'  |> filter(fn: (r) => r["_measurement"] == "vsd_metrics")\n')

    def per_vm(fields, fn):
        flt = ' or '.join(f'r["_field"] == "{f}"' for f in fields)
        return (base + f'  |> filter(fn: (r) => {flt})\n'
                f'  |> aggregateWindow(every: {window}, fn: mean, createEmpty: false)\n'
                f'  |> group(columns: ["vm_uuid", "vm_name", "_field"])\n'
                f'  |> aggregateWindow(every: {window}, fn: {fn}, createEmpty: false)\n')
    latest = (f'from(bucket: "{bucket}")\n  |> range(start: -{int(max(stale_seconds, 60) * 2)}s)\n  {cluster_filter}\n'
              f'  |> filter(fn: (r) => r["_measurement"] == "vsd_metrics")\n'
              f'  |> last()\n'
              f'  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")\n')
    return per_vm(SUM_FIELDS, 'sum'), per_vm(MAX_FIELDS, 'max'), latest
