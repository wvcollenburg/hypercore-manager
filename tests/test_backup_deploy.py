"""backup/deploy.py — cloud-init rendering, the bootstrap receiver and the image push of a proxy VM deployment."""
import importlib
import json
import os
import socket
import subprocess
import sys
import time
import types
import urllib.request

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
    ud = dp.cloud_init_userdata(token='tok123', tz='Europe/Amsterdam', hostname='px-01',
                                ip={'mode': 'static', 'address': '10.0.0.5', 'prefix': 24, 'gateway': '10.0.0.1', 'dns': '10.0.0.2, 1.1.1.1'},
                                authorized_key='ssh-ed25519 AAAA test')
    assert ud.startswith('#cloud-config\n')
    doc = yaml.safe_load(ud)
    assert doc['hostname'] == 'px-01' and 'docker.io' in doc['packages'] and 'qemu-guest-agent' in doc['packages']
    files = {f['path']: f for f in doc['write_files']}
    assert files['/etc/modprobe.d/nbd.conf']['content'].strip() == 'options nbd max_part=0'
    env = files['/etc/hcm-proxy.env']
    assert 'PROXY_API_TOKEN=tok123' in env['content'] and 'PROXY_TLS=cert' in env['content'] and 'PROXY_UPDATE_DIR=/update' in env['content']
    assert 'PROXY_TLS_CERT=/etc/hcm-proxy/tls/proxy.crt' in env['content'] and 'PROXY_TLS_KEY=/etc/hcm-proxy/tls/proxy.key' in env['content']
    assert env['permissions'] == '0600'
    run = files['/usr/local/sbin/hcm-proxy-run.sh']['content']
    assert '--privileged' in run and '-v /sys:/host/sys:ro' in run and '-v /var/lib/hcm-proxy-update:/update' in run
    assert '-v /etc/hcm-proxy/tls:/etc/hcm-proxy/tls:ro' in run, 'the container serves the certificate the receiver generated'
    assert 'cat /etc/hcm-proxy/image' in run and 'docker pull' not in ud, 'the VM pulls nothing: the image is pushed'
    upd = files['/usr/local/sbin/hcm-proxy-update.sh']['content']
    assert 'docker load < "$d/image.tar"' in upd and 'systemctl restart hcm-proxy.service' in upd and upd.index('mv "$d/ready"') < upd.index('docker load')
    boot = files['/usr/local/sbin/hcm-proxy-bootstrap.py']
    assert boot['permissions'] == '0755'
    compile(boot['content'], 'hcm-proxy-bootstrap.py', 'exec')
    for unit in ('hcm-proxy.service', 'hcm-proxy-bootstrap.service', 'hcm-proxy-update.path', 'hcm-proxy-update.service'):
        assert f'/etc/systemd/system/{unit}' in files
    assert 'ConditionPathExists=!/etc/hcm-proxy/image' in files['/etc/systemd/system/hcm-proxy-bootstrap.service']['content']
    assert 'PathExists=/var/lib/hcm-proxy-update/ready' in files['/etc/systemd/system/hcm-proxy-update.path']['content']
    np = yaml.safe_load(files['/etc/netplan/60-hcm-proxy.yaml']['content'])
    eth = np['network']['ethernets']['primary']
    assert eth['addresses'] == ['10.0.0.5/24'] and eth['routes'][0]['via'] == '10.0.0.1' and eth['nameservers']['addresses'] == ['10.0.0.2', '1.1.1.1']
    assert doc['ssh_authorized_keys'] == ['ssh-ed25519 AAAA test']
    assert any('global_filter' in c for c in doc['runcmd']) and 'netplan apply' in doc['runcmd']
    assert doc['runcmd'][-1] == 'systemctl enable --now hcm-proxy-update.path hcm-proxy.service hcm-proxy-bootstrap.service'


def test_cloud_init_dhcp_variant():
    doc = yaml.safe_load(dp.cloud_init_userdata(token='t'))
    paths = [f['path'] for f in doc['write_files']]
    assert '/etc/netplan/60-hcm-proxy.yaml' not in paths and 'ssh_authorized_keys' not in doc
    assert dp.netplan({'mode': 'dhcp'}) is None


def test_vm_body_and_metadata():
    body = dp.vm_body('px', 2, 4, 7, 'ud', 'md')
    assert body['dom']['mem'] == 4 << 30 and body['dom']['numVCPU'] == 2 and body['dom']['netDevs'][0]['vlan'] == 7
    assert body['dom']['cloudInitData'] == {'userData': 'dWQ=', 'metaData': 'bWQ='} and body['options']['machineTypeKeyword'] == 'uefi'   # base64, as HyperCore requires
    md = dp.cloud_init_metadata('px')
    assert md.startswith('instance-id: px-') and 'local-hostname: px' in md


def _free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def test_bootstrap_receiver_takes_one_push_and_hands_over(tmp_path):
    """The receiver cloud-init installs: health says who it is, the push needs the token, the
    tarball lands with its tag and the ready marker, and the process exits afterwards."""
    script = tmp_path / 'bootstrap.py'; script.write_text(dp.BOOTSTRAP_PY)
    (tmp_path / 'env').write_text('PROXY_API_TOKEN=s3cret\nPROXY_TLS=cert\n')
    drop, tls_dir = tmp_path / 'drop', tmp_path / 'tls'
    port = _free_port()
    env = dict(os.environ, HCM_PROXY_ENV=str(tmp_path / 'env'), HCM_PROXY_UPDATE_DIR=str(drop), HCM_PROXY_TLS_DIR=str(tls_dir),
               HCM_PROXY_BOOTSTRAP_PORT=str(port))
    proc = subprocess.Popen([sys.executable, '-I', str(script)], env=env, stderr=subprocess.PIPE)
    try:
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
        base = f'https://127.0.0.1:{port}'
        for _ in range(200):
            try:
                health = json.load(urllib.request.urlopen(base + '/health', timeout=1, context=ctx)); break
            except OSError:
                time.sleep(0.05)
        assert health == {'status': 'ok', 'service': 'hcm-proxy-bootstrap'}
        assert (tls_dir / 'proxy.crt').exists() and (tls_dir / 'proxy.key').stat().st_mode & 0o777 == 0o600, 'the certificate the container reuses'
        payload = b'not really a tarball ' * 1000
        req = urllib.request.Request(base + '/update', data=payload, method='POST', headers={'X-Image-Tag': 'hcm-proxy:9.9'})
        try:
            urllib.request.urlopen(req, timeout=5, context=ctx); assert False, 'no token must be refused'
        except urllib.error.HTTPError as e:
            assert e.code == 401
        req.add_header('Authorization', 'Bearer s3cret')
        r = json.load(urllib.request.urlopen(req, timeout=5, context=ctx))
        assert r == {'accepted': True, 'tag': 'hcm-proxy:9.9', 'bytes': len(payload)}
        assert (drop / 'image.tar').read_bytes() == payload and (drop / 'image.tag').read_text() == 'hcm-proxy:9.9\n' and (drop / 'ready').exists()
        assert proc.wait(timeout=10) == 0, 'the receiver exits after the push: the proxy container takes the port'
    finally:
        if proc.poll() is None:
            proc.kill()


class _FakeBoot:
    """Stands in for the ProxyClient the deployment points at the VM's port 8083."""
    answers: list = []
    pushed: list = []

    def __init__(self, url, token, **kw):
        self.url, self.token = url, token

    def health(self):
        a = _FakeBoot.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    def push_image(self, tag, chunks, timeout=900):
        _FakeBoot.pushed.append((self.url, self.token, tag, b''.join(chunks)))
        return {'accepted': True, 'tag': tag, 'bytes': len(_FakeBoot.pushed[-1][3])}


def test_push_image_waits_for_the_receiver_pins_it_and_pushes(monkeypatch):
    _FakeBoot.answers = [dp.ProxyError('refused'), {'status': 'ok', 'service': 'hcm-proxy-bootstrap'}]
    _FakeBoot.pushed = []
    pins = []
    monkeypatch.setattr(dp, 'ProxyClient', _FakeBoot)
    monkeypatch.setattr(dp.proxy_image, 'stack_image', lambda **kw: {'available': True, 'version': '0.4.0', 'tag': 'hcm-proxy:0.4.0'})
    monkeypatch.setattr(dp.proxy_image, 'stream_stack_image', lambda: ('hcm-proxy:0.4.0', iter([b'abc', b'def'])))
    msgs = []
    r = dp.push_image('10.0.0.9', 'tok', on_message=msgs.append, sleep=lambda s: None,
                      fingerprint_of=lambda url: pins.append(url) or 'ab' * 32)
    assert r['tag'] == 'hcm-proxy:0.4.0' and r['tls_fingerprint'] == 'ab' * 32
    assert _FakeBoot.pushed == [('https://10.0.0.9:8083', 'tok', 'hcm-proxy:0.4.0', b'abcdef')]
    assert pins and all(u == 'https://10.0.0.9:8083' for u in pins), 'the certificate is read over the same TLS endpoint'
    assert msgs[0].startswith('pushing proxy image 0.4.0') and 'pinned' in msgs[0] and 'loaded on the VM' in msgs[1]


def test_push_image_leaves_a_running_proxy_alone_and_times_out(monkeypatch):
    _FakeBoot.answers = [{'status': 'ok', 'service': 'hcm-proxy'}]
    _FakeBoot.pushed = []
    monkeypatch.setattr(dp, 'ProxyClient', _FakeBoot)
    fp = lambda url: 'cd' * 32   # noqa: E731
    assert dp.push_image('10.0.0.9', 'tok', sleep=lambda s: None, fingerprint_of=fp)['skipped'] and _FakeBoot.pushed == []
    _FakeBoot.answers = [dp.ProxyError('refused')] * 50
    clock = iter(range(0, 100000, 400))
    monkeypatch.setattr(dp.time, 'monotonic', lambda: next(clock))
    with pytest.raises(dp.IncrementalError, match='bootstrap receiver'):
        dp.push_image('10.0.0.9', 'tok', timeout=1000, sleep=lambda s: None, fingerprint_of=fp)
