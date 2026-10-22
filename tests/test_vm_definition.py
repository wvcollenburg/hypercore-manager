"""The shipped libvirt templates render to well-formed XML for every machine type / OS profile."""
import importlib.util
import sys
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location('vm_definition', ROOT / 'backup' / 'vm_definition.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


vmdef = _load()

DISK = 'd0dd5d2c-9c7a-44ec-b6bc-d4ce06bb204d'
CD = '06864d00-89a9-4914-9fd2-958d23ef2505'
NVRAM = 'a26166e5-ddf9-4665-83f8-e6dd5254019a'
VTPM = '01812505-ecbc-4e78-9252-01cbc2edc36d'


def _vm(machine_type, os_name, with_nvram=True, with_vtpm=True):
    devs = [{'uuid': CD, 'type': 'IDE_CDROM'}, {'uuid': DISK, 'type': 'VIRTIO_DISK', 'capacity': 15000000000}]
    if with_nvram:
        devs.append({'uuid': NVRAM, 'type': 'NVRAM'})
    if with_vtpm:
        devs.append({'uuid': VTPM, 'type': 'VTPM'})
    return {'name': 'exptest', 'description': 'demo & test', 'mem': 4294967296, 'numVCPU': 2,
            'machineType': machine_type, 'operatingSystem': os_name, 'cpuType': 'intel-core-cascadelake-amd64-9.4',
            'tags': 'test,dnd', 'bootDevices': [DISK, CD], 'blockDevs': devs,
            'netDevs': [{'uuid': 'n1', 'type': 'VIRTIO', 'macAddress': '7C:4C:58:4B:5C:5E', 'vlan': 0}]}


FILES = {DISK: str(uuid.uuid4()), NVRAM: str(uuid.uuid4()), VTPM: str(uuid.uuid4())}


def test_all_fourteen_templates_are_shipped():
    combos = vmdef.known_combinations()
    types = {mt for mt, _ in combos}
    assert types == {'scale-7.2', 'scale-8.10', 'scale-bios-9.3', 'scale-uefi-9.3', 'scale-uefi-tpm-9.3', 'scale-uefi-tpm-9.5', 'scale-uefi-9.5'}
    assert all((mt, 'windows') in combos and (mt, 'other') in combos for mt in types)


@pytest.mark.parametrize('machine_type,profile', vmdef.known_combinations())
def test_render_is_well_formed_and_complete(machine_type, profile):
    os_name = 'os_windows_server_2012' if profile == 'windows' else 'os_other'
    new = str(uuid.uuid4())
    out = vmdef.render(_vm(machine_type, os_name), FILES, new_uuid=new)
    root = ET.fromstring(out)
    assert root.findtext('uuid') == new and root.findtext('name') == new
    assert root.findtext('memory') == str(4294967296 // 1024) and root.findtext('vcpu') == '2'
    disks = [d for d in root.iter('disk') if d.get('device') == 'disk']
    assert len(disks) == 1 and disks[0].find('source').get('name') == f'scribe/{FILES[DISK]}'
    assert disks[0].find('boot').get('order') == '1' and root.find('.//disk[@device="cdrom"]/boot').get('order') == '2'
    assert root.find('.//interface/mac').get('address') == '7C:4C:58:4B:5C:5E'
    ns = {'s': 'http://www.scalecomputing.com/scaled'}
    assert root.find('.//s:machine', ns).get('name') == machine_type
    assert root.find('.//s:os', ns).get('uuid') == os_name
    assert {t.get('name') for t in root.findall('.//s:tag', ns)} == {'test', 'dnd'}
    assert '&amp;' in out and '{{' not in out
    uefi = 'uefi' in machine_type or machine_type == 'scale-8.10'
    assert (root.find('os/nvram') is not None) == uefi
    if uefi:
        assert root.findtext('os/nvram') == f'scribe:scribe/{FILES[NVRAM]}'
    assert (root.find('.//s:vtpm', ns) is not None) == ('tpm' in machine_type)
    assert (root.find('features/hyperv') is not None) == (profile == 'windows')


def test_missing_nvram_mapping_is_an_error():
    with pytest.raises(ValueError):
        vmdef.render(_vm('scale-uefi-9.3', 'os_other', with_nvram=False), {DISK: 'x'}, new_uuid='u')


def test_unknown_machine_type_falls_to_learn_mode():
    with pytest.raises(vmdef.UnknownMachineType):
        vmdef.render(_vm('scale-future-12.0', 'os_other'), FILES, new_uuid='u')


def test_learned_template_override():
    tpl = '<domain type="kvm"><name>{{uuid}}</name><uuid>{{uuid}}</uuid><memory unit="KiB">{{mem_kib}}</memory><vcpu placement="static">{{vcpu}}</vcpu><devices>{{devices}}</devices><x os="{{os}}" cpu="{{cpu_type}}" d="{{description}}">{{tags}}</x></domain>'
    out = vmdef.render(_vm('scale-future-12.0', 'os_other'), FILES, new_uuid='u', template=tpl)
    assert ET.fromstring(out).findtext('uuid') == 'u'
