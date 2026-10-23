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


@pytest.mark.parametrize('ref, host, repo, reference', [
    ('ghcr.io/scale/hcm-proxy:1.2', 'ghcr.io', 'scale/hcm-proxy', '1.2'),
    ('ghcr.io/scale/hcm-proxy', 'ghcr.io', 'scale/hcm-proxy', 'latest'),
    ('nginx', 'registry-1.docker.io', 'library/nginx', 'latest'),
    ('scale/hcm-proxy:dev', 'registry-1.docker.io', 'scale/hcm-proxy', 'dev'),
    ('localhost:5000/hcm-proxy:1', 'localhost:5000', 'hcm-proxy', '1'),
    ('ghcr.io/scale/hcm-proxy@sha256:abc', 'ghcr.io', 'scale/hcm-proxy', 'sha256:abc'),
])
def test_parse_image_ref(ref, host, repo, reference):
    r = dp.parse_image_ref(ref)
    assert (r['host'], r['repository'], r['reference']) == (host, repo, reference)


def test_parse_image_ref_rejects_junk():
    with pytest.raises(ValueError):
        dp.parse_image_ref('not an image')


class _Registry:
    """A registry that challenges anonymous pulls with a Bearer token flow, like ghcr.io and Docker Hub."""
    def __init__(self, token_status=200, token=b'{"token":"t0k"}', manifest_status=200):
        self.calls, self.token_status, self.token, self.manifest_status = [], token_status, token, manifest_status

    def __call__(self, url, headers, timeout):
        self.calls.append((url, headers.get('Authorization')))
        if url.startswith('https://ghcr.io/token?'):
            return self.token_status, {}, self.token
        if headers.get('Authorization') == 'Bearer t0k':
            return self.manifest_status, {}, b'{}'
        return 401, {'WWW-Authenticate': 'Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:scale/hcm-proxy:pull"'}, b''


def test_image_pullable_public_image_passes_the_token_flow():
    reg = _Registry()
    assert dp.image_pullable('ghcr.io/scale/hcm-proxy:1', fetch=reg) == {'ok': True, 'detail': None}
    assert [u for u, _ in reg.calls] == ['https://ghcr.io/v2/scale/hcm-proxy/manifests/1',
                                         'https://ghcr.io/token?scope=repository%3Ascale%2Fhcm-proxy%3Apull&service=ghcr.io',
                                         'https://ghcr.io/v2/scale/hcm-proxy/manifests/1']
    assert reg.calls[-1][1] == 'Bearer t0k'


def test_image_pullable_private_or_missing_image_is_refused():
    r = dp.image_pullable('ghcr.io/scale/hcm-proxy:1', fetch=_Registry(token_status=403, token=b'{"errors":[{"code":"DENIED"}]}'))
    assert r['ok'] is False and 'private or does not exist' in r['detail'] and 'HTTP 403' in r['detail']
    r = dp.image_pullable('ghcr.io/scale/hcm-proxy:1', fetch=_Registry(manifest_status=404))
    assert r['ok'] is False and 'HTTP 404' in r['detail']
    assert dp.image_pullable('two words', fetch=_Registry())['ok'] is False


def test_image_pullable_unreachable_registry_does_not_block():
    def down(url, headers, timeout):
        raise OSError('no route to host')
    r = dp.image_pullable('ghcr.io/scale/hcm-proxy:1', fetch=down)
    assert r['ok'] is None and 'not reachable from the backup service' in r['detail']


def test_start_script_retries_the_pull_and_logs():
    ud = dp.cloud_init_userdata(token='t', proxy_image='ghcr.io/x/hcm-proxy:1')
    start = next(f['content'] for f in yaml.safe_load(ud)['write_files'] if f['path'].endswith('hcm-proxy-start.sh'))
    assert 'exec >>/var/log/hcm-proxy-start.log' in start
    assert 'for i in 1 2 3 4 5 6; do docker pull ghcr.io/x/hcm-proxy:1 && break;' in start
    assert start.index('docker pull') < start.index('docker run')
