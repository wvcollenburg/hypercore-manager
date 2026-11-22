"""Replication targets are recognised as replicas, not shown as stopped VMs (William, 2026-11-22).

HyperCore marks a replication target with sourceVirDomainUUID (non-empty: "this is a replication target").
The collector records it on the VM's inventory point; the dashboard lists replicas in their own section,
last, and names the source VM when the user may see its cluster in full.
"""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location('hcm_collector_replicas', ROOT / 'Collector' / 'collector.py')
collector = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(collector)


def _vm(uuid, name, state='SHUTOFF', source=None):
    vm = {'uuid': uuid, 'name': name, 'state': state, 'nodeUUID': 'n1', 'tags': 'x', 'netDevs': [], 'blockDevs': [],
          'numVCPU': 2, 'mem': 1 << 30, 'snapUUIDs': []}
    if source is not None:
        vm['sourceVirDomainUUID'] = source
    return vm


def test_the_inventory_carries_the_replication_source_of_a_replica_only():
    vms = [_vm('a', 'pihole', source='19432a76-3ddb-486f-8226-8736e85224e2'), _vm('b', 'web', 'RUNNING', source=''), _vm('c', 'db')]
    points, _ = collector.collect_fast('c1', [{'uuid': 'n1', 'peerID': 1, 'lanIP': '10.0.0.1'}], vms, [], {})
    inv = {line.split('vm_name=')[1].split(',')[0]: line
           for line in (p.to_line_protocol() for p in points) if line.startswith('vm_inventory,')}
    assert 'vm_source=19432a76-3ddb-486f-8226-8736e85224e2' in inv['pihole']
    assert 'vm_source' not in inv['web'] and 'vm_source' not in inv['db']


def test_the_vm_list_puts_replicas_in_their_own_section_last(repo):
    src = (repo / 'dashboard' / 'static' / 'index.html').read_text()
    body = src[src.index('const allRows = cachedVmData'):]
    body = body[:body.index('container.innerHTML')]
    assert 'replicas = allRows.filter(r => r.vm.replica_of)' in body and 'rows = allRows.filter(r => !r.vm.replica_of)' in body
    # the section is appended after every other group (and after the ungrouped list)
    assert body.index("if (replicas.length) {") > body.index("if (currentVmGroup === 'none') {")
    assert "t('vm.replicas')" in body and "t('vm.replica')" in body


def test_the_dashboard_reads_the_source_and_names_it_only_on_fully_visible_clusters(repo):
    app = (repo / 'dashboard' / 'app.py').read_text()
    assert "vm['replica_of'] = rec.values.get('vm_source') or None" in app
    fn = app[app.index('def _replica_sources('):]
    fn = fn[:fn.index('\ndef ')]
    assert "scope_for('vm.view', [cid]).get(cid) == 'all'" in fn
    assert 'is_uuid(' in fn, 'only uuids reach the Flux query'
