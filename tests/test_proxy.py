"""proxy/ — the backup proxy's pure parts and its job API, without root, nbd, qemu or a share.

proxy/ reuses module names other services have (config, api, jobs, log), so it is imported in
isolation the same way backup/ is: proxy/ first on sys.path for the import only, clashing names
purged from sys.modules before and after.
"""
import importlib
import json
import os
import random
import sys
import types

import pytest

_PROXY = os.path.join(os.path.dirname(__file__), '..', 'proxy')
_CLASH = ('config', 'devices', 'copier', 'images', 'targets', 'manifest', 'identity', 'jobs', 'api', 'log')


def _import():
    saved = {n: sys.modules.pop(n) for n in _CLASH if n in sys.modules}
    sys.path.insert(0, _PROXY)
    try:
        mods = {n: importlib.import_module(n) for n in _CLASH if n != 'log'}
    finally:
        sys.path.remove(_PROXY)
        for n in _CLASH:
            sys.modules.pop(n, None)
        sys.modules.update(saved)
    return types.SimpleNamespace(**mods)


P = _import()
MiB = 1 << 20


# ----------------------------------------------------------------------------- targets
def test_parse_smb_and_nfs_uris():
    t = P.targets.parse_uri('smb://nas.example/backups/hypercore/tenant%20a')
    assert (t.scheme, t.host, t.share, t.subpath) == ('smb', 'nas.example', 'backups', 'hypercore/tenant a')
    n = P.targets.parse_uri('nfs://nas.example/export/hcm')
    assert (n.scheme, n.host, n.share, n.subpath) == ('nfs', 'nas.example', 'export/hcm', '')
    with pytest.raises(ValueError):
        P.targets.parse_uri('ftp://nas/x')
    with pytest.raises(ValueError):
        P.targets.parse_uri('smb://nas')


def test_mount_commands():
    t = P.targets.parse_uri('smb://nas/share/sub')
    cmd = P.targets.build_mount_cmd(t, '/mnt/x', '/tmp/cred')
    assert cmd[:4] == ['mount', '-t', 'cifs', '//nas/share'] and 'credentials=/tmp/cred' in cmd[-1] and 'vers=3.0' in cmd[-1]
    assert 'guest' in P.targets.build_mount_cmd(t, '/mnt/x', None)[-1]
    n = P.targets.parse_uri('nfs://nas/export/hcm')
    assert P.targets.build_mount_cmd(n, '/mnt/y')[:5] == ['mount', '-t', 'nfs', 'nas:/export/hcm', '/mnt/y']


def test_smb_username_forms():
    assert P.targets.smb_username('user@corp.example') == 'corp.example\\user'
    assert P.targets.smb_username('CORP/user') == 'CORP\\user'
    assert P.targets.smb_username('CORP\\user') == 'CORP\\user'
    assert P.targets.smb_username(None) is None


def test_mounter_refcounts_and_writes_credentials_once(tmp_path):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[0] == 'mount':
            cred = [o for o in cmd[-1].split(',') if o.startswith('credentials=')][0].split('=', 1)[1]
            text = open(cred).read()
            assert 'username=user' in text and 'domain=corp' in text and 'password=pw' in text
            os.makedirs(cmd[4], exist_ok=True)
        return types.SimpleNamespace(returncode=0, stdout='', stderr='')

    m = P.targets.Mounter(str(tmp_path), run=fake_run)
    p1 = m.acquire('smb://nas/share/tenant', 'user@corp', 'pw')
    p2 = m.acquire('smb://nas/share/other', 'user@corp', 'pw')
    assert p1.endswith('/tenant') and p2.endswith('/other') and os.path.isdir(p1)
    assert len([c for c in calls if c[0] == 'mount']) == 1, 'same share mounted once'
    assert not [f for f in os.listdir(tmp_path) if f.startswith('cred-')], 'credentials file removed after mount'
    m.release('smb://nas/share/tenant'); m.release('smb://nas/share/other')


def test_mounter_reports_mount_failure(tmp_path):
    def failing(cmd, **kw):
        return types.SimpleNamespace(returncode=32, stdout='', stderr='mount error(13): Permission denied')
    m = P.targets.Mounter(str(tmp_path), run=failing)
    with pytest.raises(RuntimeError, match='Permission denied'):
        m.acquire('smb://nas/share', 'u', 'p')


# ----------------------------------------------------------------------------- copier / devices
def _random_file(path, mib, seed):
    rnd = random.Random(seed)
    with open(path, 'wb') as fh:
        for _ in range(mib):
            fh.write(rnd.randbytes(MiB) if hasattr(rnd, 'randbytes') else bytes(rnd.getrandbits(8) for _ in range(MiB)))


def test_copy_intervals_copies_mapped_blocks_and_zeroes_holes(tmp_path):
    src, dst = str(tmp_path / 'src'), str(tmp_path / 'dst')
    _random_file(src, 8, 1)
    with open(dst, 'wb') as fh:
        fh.write(b'\xff' * 8 * MiB)                         # pre-existing content must survive outside the map
    ivs = [{'lower': 1, 'upper': 2}, {'lower': 5, 'upper': 5, 'differentHow': 'TO_IS_HOLE'}, {'lower': 7, 'upper': 7}]
    written = P.copier.copy_intervals(src, dst, ivs, writers=3)
    assert written == 4 * MiB
    s, d = open(src, 'rb').read(), open(dst, 'rb').read()
    for blk in (1, 2, 7):
        assert d[blk * MiB:(blk + 1) * MiB] == s[blk * MiB:(blk + 1) * MiB]
    assert d[5 * MiB:6 * MiB] == bytes(MiB)
    for blk in (0, 3, 4, 6):
        assert d[blk * MiB:(blk + 1) * MiB] == b'\xff' * MiB


def test_copy_intervals_cancels(tmp_path):
    src, dst = str(tmp_path / 'src'), str(tmp_path / 'dst')
    _random_file(src, 4, 2); open(dst, 'wb').truncate(4 * MiB)
    with pytest.raises(RuntimeError, match='cancelled'):
        P.copier.copy_intervals(src, dst, [{'lower': 0, 'upper': 3}], writers=1, cancelled=lambda: True)


def test_device_helpers(tmp_path):
    dev = tmp_path / 'dev' / 'disk' / 'by-id'
    dev.mkdir(parents=True)
    u = 'abcdef12-0000-0000-0000-000000000000'
    assert P.devices.by_id_path(u, str(tmp_path / 'dev')).endswith('/disk/by-id/virtio-abcdef12')
    with pytest.raises(TimeoutError):
        P.devices.wait_for_device(u, timeout=0.3, dev_dir=str(tmp_path / 'dev'), poll=0.1)
    (dev / 'virtio-abcdef12').write_bytes(b'\0' * 3 * MiB)
    assert P.devices.wait_for_device(u, timeout=1, dev_dir=str(tmp_path / 'dev'), poll=0.1).endswith('virtio-abcdef12')
    assert P.devices.device_size(str(dev / 'virtio-abcdef12')) == 3 * MiB
    assert P.devices.is_read_only(str(dev / 'virtio-abcdef12')) is None


# ----------------------------------------------------------------------------- manifest / identity
def test_manifest_names_and_verify(tmp_path):
    assert P.manifest.point_dirname('full', 0) == 'base'
    assert P.manifest.point_dirname('delta', 3) == 'delta-0003'
    assert P.manifest.point_dirname('full', 14) == 'full-0014'
    assert P.manifest.chain_dirname('My VM (prod)', '3cf115a9-8208-41b2-a67d-0ed5421441c2') == 'My_VM__prod__3cf115a9.chain'
    pd = tmp_path / 'base'; pd.mkdir()
    (pd / 'x.qcow2').write_bytes(b'abc')
    P.manifest.write_json(str(pd / 'manifest.json'), {'devices': [{'file': 'x.qcow2', 'sha256': P.manifest.sha256_file(str(pd / 'x.qcow2'))}]})
    assert P.manifest.verify_point(str(pd)) == {'ok': True, 'checked': 1, 'mismatches': []}
    (pd / 'x.qcow2').write_bytes(b'abd')
    assert P.manifest.verify_point(str(pd))['mismatches'] == ['x.qcow2: checksum differs']


def test_identity_reads_host_macs(tmp_path):
    for n, mac in (('enp3s0', '7c:4c:58:4b:5c:5e'), ('lo', '00:00:00:00:00:00'), ('docker0', '02:42:ac:11:00:02'), ('veth1', '12:34:56:78:9a:bc')):
        (tmp_path / n).mkdir(); (tmp_path / n / 'address').write_text(mac + '\n')
    assert P.identity.host_macs(str(tmp_path)) == [{'ifname': 'enp3s0', 'mac': '7C:4C:58:4B:5C:5E'}]
    assert P.identity.host_macs(str(tmp_path / 'nope')) == []


# ----------------------------------------------------------------------------- job engine + API (file backend)
class _FakeMounter:
    """Pretends every target is already mounted under <root>/<key>."""
    def __init__(self, root):
        self.root = root; self.acquired = []; self.released = []

    def acquire(self, uri, username, password):
        t = P.targets.parse_uri(uri); p = os.path.join(self.root, t.key, t.subpath)
        os.makedirs(p, exist_ok=True); self.acquired.append(uri); return p

    def release(self, uri):
        self.released.append(uri)


@pytest.fixture
def stack(tmp_path):
    dev = tmp_path / 'dev' / 'disk' / 'by-id'; dev.mkdir(parents=True)
    cfg = P.config.Config(api_token='t0k', data_dir=str(tmp_path / 'data'), dev_dir=str(tmp_path / 'dev'),
                          host_net=str(tmp_path / 'net'), device_wait_seconds=2, writers=2, concurrency=1, backend='file')
    mounter = _FakeMounter(str(tmp_path / 'targets'))
    engine = P.jobs.Engine(cfg, P.images.FileBackend(), mounter)
    app = P.api.create_app(cfg, engine)
    return types.SimpleNamespace(cfg=cfg, engine=engine, mounter=mounter, client=app.test_client(), dev=dev, tmp=tmp_path)


def _wait(stack, job_id, timeout=20):
    import time
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = stack.client.get(f'/jobs/{job_id}', headers={'Authorization': 'Bearer t0k'}).get_json()
        if j['state'] in ('done', 'failed', 'cancelled'):
            return j
        time.sleep(0.05)
    raise AssertionError('job did not finish')


def test_api_requires_token_and_validates(stack):
    assert stack.client.get('/identity').status_code == 401
    assert stack.client.get('/health').status_code == 200
    r = stack.client.post('/jobs/backup', json={'target': {'uri': 'smb://nas/s'}}, headers={'Authorization': 'Bearer t0k'})
    assert r.status_code == 400 and 'missing fields' in r.get_json()['error']
    assert stack.client.post('/jobs/teleport', json={}, headers={'Authorization': 'Bearer t0k'}).status_code == 404
    ident = stack.client.get('/identity', headers={'Authorization': 'Bearer t0k'}).get_json()
    assert ident['service'] == 'hcm-proxy' and ident['backend'] == 'file' and 'backup' in ident['capabilities']['jobs']


def test_backup_full_then_delta_then_flatten_verify_sweep(stack):
    H = {'Authorization': 'Bearer t0k'}
    disk = 'd0dd5d2c-9c7a-44ec-b6bc-d4ce06bb204d'; nvram = 'a26166e5-ddf9-4665-83f8-e6dd5254019a'
    clone_a, clone_n = 'aaaa1111-0000-0000-0000-000000000000', 'bbbb2222-0000-0000-0000-000000000000'
    # snapshot A of a 6 MiB disk: blocks 0,1,4 allocated
    src_a = stack.dev / 'virtio-aaaa1111'; _random_file(str(src_a), 6, 10)
    (stack.dev / 'virtio-bbbb2222').write_bytes(b'N' * MiB)
    full_map = [{'lower': 0, 'upper': 1}, {'lower': 4, 'upper': 4}]
    req = {'target': {'uri': 'smb://nas/share/hcm', 'username': 'u', 'password': 'p'}, 'folder': 'Amsterdam',
           'vm': {'uuid': '3cf115a9-8208-41b2-a67d-0ed5421441c2', 'name': 'exptest'},
           'point': {'kind': 'full', 'seq': 0}, 'plan_id': 4, 'run_id': 20,
           'devices': [{'uuid': disk, 'type': 'VIRTIO_DISK', 'clone_uuid': clone_a, 'size': 6 * MiB, 'map': full_map},
                       {'uuid': nvram, 'type': 'NVRAM', 'clone_uuid': clone_n, 'size': MiB, 'map': [{'lower': 0, 'upper': 0}]}],
           'definition': {'name': 'exptest', 'mem': 4294967296, 'netDevs': [{'macAddress': '7C:4C:58:4B:5C:5E'}]}}
    jid = stack.client.post('/jobs/backup', json=req, headers=H).get_json()['id']
    j = _wait(stack, jid)
    assert j['state'] == 'done', j
    chain = os.path.join(stack.mounter.acquired and stack.mounter.root, P.targets.parse_uri(req['target']['uri']).key, 'hcm', 'Amsterdam', 'exptest_3cf115a9.chain')
    assert os.path.isdir(os.path.join(chain, 'base'))
    base_img = os.path.join(chain, 'base', f'{disk}.qcow2')
    assert os.path.getsize(base_img) == 6 * MiB and os.path.exists(os.path.join(chain, 'base', f'nvram-{nvram}.qcow2'))
    pm = json.load(open(os.path.join(chain, 'base', 'manifest.json')))
    assert pm['definition']['netDevs'][0]['macAddress'] == '7C:4C:58:4B:5C:5E' and pm['devices'][0]['blocks'] == 3
    assert j['result']['bytes_written'] == 4 * MiB and stack.mounter.released == [req['target']['uri']]
    a = open(str(src_a), 'rb').read(); b = open(base_img, 'rb').read()
    assert b[:2 * MiB] == a[:2 * MiB] and b[4 * MiB:5 * MiB] == a[4 * MiB:5 * MiB] and b[2 * MiB:4 * MiB] == bytes(2 * MiB)
    # a second full at the same seq is refused
    j2 = _wait(stack, stack.client.post('/jobs/backup', json=req, headers=H).get_json()['id'])
    assert j2['state'] == 'failed' and 'already exists' in j2['error']
    # snapshot B: block 1 changed, block 4 became a hole, block 5 new
    src_b = stack.dev / 'virtio-cccc3333'; _random_file(str(src_b), 6, 11)
    delta_map = [{'lower': 1, 'upper': 1}, {'lower': 4, 'upper': 4, 'differentHow': 'TO_IS_HOLE'}, {'lower': 5, 'upper': 5}]
    req2 = dict(req, point={'kind': 'delta', 'seq': 1, 'previous_seq': 0}, run_id=21,
                devices=[{'uuid': disk, 'type': 'VIRTIO_DISK', 'clone_uuid': 'cccc3333-0000-0000-0000-000000000000', 'size': 6 * MiB, 'map': delta_map},
                         {'uuid': nvram, 'type': 'NVRAM', 'clone_uuid': clone_n, 'size': MiB, 'map': [{'lower': 0, 'upper': 0}]}])
    j3 = _wait(stack, stack.client.post('/jobs/backup', json=req2, headers=H).get_json()['id'])
    assert j3['state'] == 'done', j3
    delta_img = os.path.join(chain, 'delta-0001', f'{disk}.qcow2'); d = open(delta_img, 'rb').read(); bb = open(str(src_b), 'rb').read()
    assert d[:MiB] == a[:MiB], 'unchanged block comes from the base'
    assert d[MiB:2 * MiB] == bb[MiB:2 * MiB] and d[5 * MiB:6 * MiB] == bb[5 * MiB:6 * MiB] and d[4 * MiB:5 * MiB] == bytes(MiB)
    ch = json.load(open(os.path.join(chain, 'chain.json')))
    assert [p['dir'] for p in ch['points']] == ['base', 'delta-0001'] and ch['vm']['name'] == 'exptest'
    # verify both points, flatten the delta into a restore dir with new file names + definition
    rel_chain = os.path.join('Amsterdam', 'exptest_3cf115a9.chain')
    v = _wait(stack, stack.client.post('/jobs/verify', json={'target': req['target'], 'chain_dir': rel_chain}, headers=H).get_json()['id'])
    assert v['state'] == 'done' and v['result']['ok'] and set(v['result']['points']) == {'base', 'delta-0001'}
    f = _wait(stack, stack.client.post('/jobs/flatten', json={'target': req['target'], 'chain_dir': rel_chain, 'seq': 1, 'out_dir': 'Amsterdam/_restore/exptest_x',
                                                              'files': {disk: 'newuuid.qcow2'}, 'definition_xml': '<domain/>', 'definition_name': 'exptest_x.xml'}, headers=H).get_json()['id'])
    assert f['state'] == 'done', f
    out = os.path.join(os.path.dirname(chain), '_restore', 'exptest_x')
    assert sorted(os.listdir(out)) == sorted(['newuuid.qcow2', f'nvram-{nvram}.qcow2', 'exptest_x.xml'])
    assert open(os.path.join(out, 'newuuid.qcow2'), 'rb').read() == d and f['result']['definition']['name'] == 'exptest'
    # sweep removes restore dirs older than the limit only
    os.utime(out, (0, 0))
    s = _wait(stack, stack.client.post('/jobs/sweep', json={'target': req['target'], 'folder': 'Amsterdam', 'older_than_hours': 1}, headers=H).get_json()['id'])
    assert s['result']['removed'] == ['exptest_x'] and not os.path.exists(out)
    # job state survived as json on disk
    assert os.path.exists(os.path.join(stack.cfg.data_dir, 'jobs', f'{jid}.json'))
    assert stack.client.get('/jobs', headers=H).get_json()[0]['id'] == s['id']


def test_backup_fails_cleanly_when_device_never_appears(stack):
    H = {'Authorization': 'Bearer t0k'}
    req = {'target': {'uri': 'nfs://nas/export'}, 'folder': 'F', 'vm': {'uuid': 'u' * 36, 'name': 'v'}, 'point': {'kind': 'full', 'seq': 0},
           'devices': [{'uuid': 'd' * 36, 'type': 'VIRTIO_DISK', 'clone_uuid': 'ffffffff-0000-0000-0000-000000000000', 'size': MiB, 'map': [{'lower': 0, 'upper': 0}]}]}
    j = _wait(stack, stack.client.post('/jobs/backup', json=req, headers=H).get_json()['id'])
    assert j['state'] == 'failed' and 'did not appear' in j['error'] and stack.mounter.released == ['nfs://nas/export']


def test_block_devices_are_written_with_o_direct(tmp_path, monkeypatch):
    """The first live run OOM-killed the proxy VM through dirty nbd pages; block devices get O_DIRECT, files do not."""
    import stat as _stat
    f = tmp_path / 'plain'; f.write_bytes(b'x')
    opened = []
    real_open = os.open

    def spy_open(path, flags, *a):
        opened.append((path, flags)); return real_open(path, flags & ~getattr(os, 'O_DIRECT', 0), *a)
    monkeypatch.setattr(P.copier.os, 'open', spy_open)
    os.close(P.copier._open_write(str(f)))
    assert not (opened[-1][1] & getattr(os, 'O_DIRECT', 0x4000))
    monkeypatch.setattr(P.copier, '_is_block_device', lambda p: True)
    if not hasattr(os, 'O_DIRECT'):
        monkeypatch.setattr(P.copier.os, 'O_DIRECT', 0x4000, raising=False)
    os.close(P.copier._open_write(str(f)))
    assert opened[-1][1] & P.copier.os.O_DIRECT
