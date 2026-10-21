"""
vm_definition.py — render a libvirt domain XML for a HyperCore import from a VirDomain record.

HyperCore's import needs a definition file next to the qcow2 images. A native export produces
one, but everything in it is either per machine type (templates under vm_definitions/) or
derivable from the VM record. render() fills a template so a restore from a proxy-built chain
never needs a native export. Verified on HyperCore 9.8.4 (minimal and full variants both
import and boot; HyperCore adds PCI addresses, features and the TPM command line itself).

A combination without a shipped template raises UnknownMachineType; the caller falls back to
"learn mode" (one native export of any VM of that type, its template stored in PostgreSQL).
"""
from __future__ import annotations

import re
from pathlib import Path
from xml.sax.saxutils import escape

TEMPLATE_DIR = Path(__file__).parent / 'vm_definitions'
_DISK_TYPES = ('VIRTIO_DISK', 'IDE_DISK', 'SCSI_DISK')


class UnknownMachineType(KeyError):
    pass


def os_profile(operating_system: str | None) -> str:
    """HyperCore uses two libvirt profiles: Windows and everything else."""
    return 'windows' if (operating_system or '').startswith('os_windows') else 'other'


def template_path(machine_type: str, operating_system: str | None) -> Path:
    return TEMPLATE_DIR / f'{machine_type}__os_{os_profile(operating_system)}.xml'


def load_template(machine_type: str, operating_system: str | None, override: str | None = None) -> str:
    if override:
        return override
    p = template_path(machine_type, operating_system)
    if not p.exists():
        raise UnknownMachineType(f'{machine_type} / {os_profile(operating_system)}')
    return p.read_text()


def _disk_xml(index: int, file_uuid: str, boot_order: int | None) -> str:
    boot = f'      <boot order="{boot_order}"/>\n' if boot_order else ''
    return (f'    <disk type="network" device="disk">\n{boot}'
            f'      <driver name="qemu" type="scribe" cache="none"/>\n'
            f'      <source protocol="scribe" name="scribe/{file_uuid}"/>\n'
            f'      <serial>{file_uuid[:8]}</serial>\n'
            f'      <target dev="vd{chr(97 + index)}" bus="virtio"/>\n'
            f'    </disk>\n')


def _cdrom_xml(boot_order: int | None) -> str:
    boot = f'      <boot order="{boot_order}"/>\n' if boot_order else ''
    return (f'    <disk device="cdrom">\n{boot}'
            f'      <driver name="qemu" type="scribe" cache="none"/>\n'
            f'      <target dev="hda" bus="sata"/>\n      <readonly/>\n    </disk>\n')


def _nic_xml(mac: str | None) -> str:
    mac_el = f'      <mac address="{escape(mac)}"/>\n' if mac else ''
    return (f'    <interface type="bridge">\n{mac_el}'
            f'      <model type="virtio"/>\n      <source bridge="lan"/>\n    </interface>\n')


def render(vm: dict, file_map: dict[str, str], *, new_uuid: str, template: str | None = None) -> str:
    """
    vm        — the VirDomain record (name, description, mem, numVCPU, machineType, operatingSystem,
                cpuType, tags, bootDevices, blockDevs, netDevs)
    file_map  — live block device uuid → uuid used for the file in the import directory
                (<uuid>.qcow2); must cover every disk, the NVRAM and the vTPM device present
    new_uuid  — uuid for the imported domain (HyperCore assigns the VM its own uuid anyway)
    template  — optional learned template text overriding the shipped one
    """
    machine_type = vm.get('machineType') or ''
    text = load_template(machine_type, vm.get('operatingSystem'), template)

    disks = [b for b in vm.get('blockDevs', []) if b.get('type') in _DISK_TYPES]
    cdroms = [b for b in vm.get('blockDevs', []) if b.get('type') == 'IDE_CDROM']
    nvram = next((b for b in vm.get('blockDevs', []) if b.get('type') == 'NVRAM'), None)
    vtpm = next((b for b in vm.get('blockDevs', []) if b.get('type') == 'VTPM'), None)
    boot = {u: i + 1 for i, u in enumerate(vm.get('bootDevices') or [])}

    for b in disks:
        if b['uuid'] not in file_map:
            raise ValueError(f'no file mapping for disk {b["uuid"]}')
    devices = ''.join(_cdrom_xml(boot.get(b['uuid'])) for b in cdroms)
    devices += ''.join(_disk_xml(i, file_map[b['uuid']], boot.get(b['uuid'])) for i, b in enumerate(disks))
    devices += ''.join(_nic_xml(n.get('macAddress')) for n in vm.get('netDevs', []))

    tags = ''.join(f'        <tag name="{escape(t.strip())}"/>\n'
                   for t in (vm.get('tags') or '').split(',') if t.strip())
    values = {
        'uuid': new_uuid,
        'description': escape(f"{vm.get('name', '')}: {vm.get('description') or ''}"),
        'mem_kib': str(int(vm.get('mem', 0)) // 1024),
        'vcpu': str(int(vm.get('numVCPU', 1))),
        'os': escape(vm.get('operatingSystem') or 'os_other'),
        'cpu_type': escape(vm.get('cpuType') or ''),
        'tags': tags,
        'devices': devices,
    }
    if '{{nvram_uuid}}' in text:
        if not nvram or nvram['uuid'] not in file_map:
            raise ValueError(f'{machine_type} needs an NVRAM image but none is mapped')
        values['nvram_uuid'] = file_map[nvram['uuid']]
    if '{{vtpm_uuid}}' in text:
        if not vtpm or vtpm['uuid'] not in file_map:
            raise ValueError(f'{machine_type} needs a vTPM image but none is mapped')
        values['vtpm_uuid'] = file_map[vtpm['uuid']]

    out = re.sub(r'{{(\w+)}}', lambda m: values[m.group(1)], text)
    left = re.findall(r'{{\w+}}', out)
    if left:
        raise ValueError(f'unfilled placeholders: {left}')
    return out


def known_combinations() -> list[tuple[str, str]]:
    out = []
    for p in sorted(TEMPLATE_DIR.glob('*__os_*.xml')):
        mt, prof = p.stem.split('__os_')
        out.append((mt, prof))
    return out
