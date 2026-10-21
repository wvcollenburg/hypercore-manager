"""backup/deploy.py — cloud-init rendering and request bodies for proxy VM deployment."""
import importlib
import os
import sys

import pytest
import yaml

_BACKUP = os.path.join(os.path.dirname(__file__), '..', 'backup')
_CLASH = ('config', 'db', 'log', 'api', 'hypercore', 'jobs', 'scheduler', 'incremental', 'proxy_client', 'manifest_names', 'deploy')


def _import():
    saved = {n: sys.modules.pop(n) for n in _CLASH if n in sys.modules}
    sys.path.insert(0, _BACKUP)
    try:
        return importlib.import_module('deploy')
    finally:
        sys.path.remove(_BACKUP)
        for n in _CLASH:
            sys.modules.pop(n, None)
        sys.modules.update(saved)


dp = _import()


def test_cloud_init_is_valid_and_complete():
    ud = dp.cloud_init_userdata(token='tok123', proxy_image='ghcr.io/x/hcm-proxy:1', tz='Europe/Amsterdam', hostname='px-01',
                                ip={'mode': 'static', 'address': '10.0.0.5', 'prefix': 24, 'gateway': '10.0.0.1', 'dns': '10.0.0.2, 1.1.1.1'},
                                authorized_key='ssh-ed25519 AAAA test')
    assert ud.startswith('#cloud-config\n')
    doc = yaml.safe_load(ud)
    assert doc['hostname'] == 'px-01' and 'docker.io' in doc['packages'] and 'qemu-guest-agent' in doc['packages']
    files = {f['path']: f for f in doc['write_files']}
    assert files['/etc/modprobe.d/nbd.conf']['content'].strip() == 'options nbd max_part=0'
    env = files['/etc/hcm-proxy.env']
    assert 'PROXY_API_TOKEN=tok123' in env['content'] and 'PROXY_TLS=auto' in env['content'] and env['permissions'] == '0600'
    start = files['/usr/local/sbin/hcm-proxy-start.sh']['content']
    assert 'docker pull ghcr.io/x/hcm-proxy:1' in start and '--privileged' in start and '-v /sys:/host/sys:ro' in start and start.endswith('ghcr.io/x/hcm-proxy:1\n')
    np = yaml.safe_load(files['/etc/netplan/60-hcm-proxy.yaml']['content'])
    eth = np['network']['ethernets']['primary']
    assert eth['addresses'] == ['10.0.0.5/24'] and eth['routes'][0]['via'] == '10.0.0.1' and eth['nameservers']['addresses'] == ['10.0.0.2', '1.1.1.1']
    assert doc['ssh_authorized_keys'] == ['ssh-ed25519 AAAA test']
    assert any('global_filter' in c for c in doc['runcmd']) and 'netplan apply' in doc['runcmd'] and doc['runcmd'][-1].endswith('hcm-proxy-start.sh')


def test_cloud_init_dhcp_and_tarball_variant():
    ud = dp.cloud_init_userdata(token='t', proxy_image='img', image_tar_url='http://stack:5000/hcm-proxy.tar')
    doc = yaml.safe_load(ud)
    paths = [f['path'] for f in doc['write_files']]
    assert '/etc/netplan/60-hcm-proxy.yaml' not in paths and 'ssh_authorized_keys' not in doc
    start = [f for f in doc['write_files'] if f['path'].endswith('.sh')][0]['content']
    assert 'curl -fsSL "http://stack:5000/hcm-proxy.tar" | docker load' in start and 'docker pull' not in start
    assert dp.netplan({'mode': 'dhcp'}) is None


def test_vm_body_and_metadata():
    body = dp.vm_body('px', 2, 4, 7, 'ud', 'md')
    assert body['dom']['mem'] == 4 << 30 and body['dom']['numVCPU'] == 2 and body['dom']['netDevs'][0]['vlan'] == 7
    assert body['dom']['cloudInitData'] == {'userData': 'dWQ=', 'metaData': 'bWQ='} and body['options']['machineTypeKeyword'] == 'uefi'   # base64, as HyperCore requires
    md = dp.cloud_init_metadata('px')
    assert md.startswith('instance-id: px-') and 'local-hostname: px' in md
