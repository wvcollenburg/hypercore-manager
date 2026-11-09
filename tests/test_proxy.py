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
_CLASH = ('config', 'devices', 'copier', 'images', 'targets', 'manifest', 'identity', 'jobs', 'api', 'mounts', 'bench', 'support', 'log')


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
    assert cmd[:4] == ['mount', '-t', 'cifs', '//nas/share'] and 'credentials=/tmp/cred' in cmd[-1]
    assert 'vers=' not in cmd[-1], 'the dialect is negotiated, not pinned (an SMB 3.1.1-only Samba refuses vers=3.0)'
    assert 'sec=none' not in cmd[-1]
    g = P.targets.build_mount_cmd(t, '/mnt/x', None)[-1]
    assert g.startswith('guest,sec=none,'), 'a guest share is a null session'
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
    assert P.manifest.chain_dirname('My VM (prod)', '3cf115a9-1111-4111-8111-111111111111') == 'My_VM__prod__3cf115a9.chain'
    pd = tmp_path / 'base'; pd.mkdir()
    (pd / 'x.qcow2').write_bytes(b'abc')
    P.manifest.write_json(str(pd / 'manifest.json'), {'devices': [{'file': 'x.qcow2', 'sha256': P.manifest.sha256_file(str(pd / 'x.qcow2'))}]})
    assert P.manifest.verify_point(str(pd)) == {'ok': True, 'checked': 1, 'mismatches': [], 'bytes': 3}
    (pd / 'x.qcow2').write_bytes(b'abd')
    assert P.manifest.verify_point(str(pd))['mismatches'] == ['x.qcow2: checksum differs']


def test_identity_reads_host_macs(tmp_path):
    for n, mac in (('enp3s0', '02:00:5e:4b:5c:5e'), ('lo', '00:00:00:00:00:00'), ('docker0', '02:42:ac:11:00:02'), ('veth1', '12:34:56:78:9a:bc')):
        (tmp_path / n).mkdir(); (tmp_path / n / 'address').write_text(mac + '\n')
    assert P.identity.host_macs(str(tmp_path)) == [{'ifname': 'enp3s0', 'mac': '02:00:5E:4B:5C:5E'}]
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
           'vm': {'uuid': '3cf115a9-1111-4111-8111-111111111111', 'name': 'exptest'},
           'point': {'kind': 'full', 'seq': 0}, 'plan_id': 4, 'run_id': 20,
           'devices': [{'uuid': disk, 'type': 'VIRTIO_DISK', 'clone_uuid': clone_a, 'size': 6 * MiB, 'map': full_map},
                       {'uuid': nvram, 'type': 'NVRAM', 'clone_uuid': clone_n, 'size': MiB, 'map': [{'lower': 0, 'upper': 0}]}],
           'definition': {'name': 'exptest', 'mem': 4294967296, 'netDevs': [{'macAddress': '02:00:5E:4B:5C:5E'}]}}
    jid = stack.client.post('/jobs/backup', json=req, headers=H).get_json()['id']
    j = _wait(stack, jid)
    assert j['state'] == 'done', j
    chain = os.path.join(stack.mounter.acquired and stack.mounter.root, P.targets.parse_uri(req['target']['uri']).key, 'hcm', 'Amsterdam', 'exptest_3cf115a9.chain')
    assert os.path.isdir(os.path.join(chain, 'base'))
    base_img = os.path.join(chain, 'base', f'{disk}.qcow2')
    assert os.path.getsize(base_img) == 6 * MiB and os.path.exists(os.path.join(chain, 'base', f'nvram-{nvram}.qcow2'))
    pm = json.load(open(os.path.join(chain, 'base', 'manifest.json')))
    assert pm['definition']['netDevs'][0]['macAddress'] == '02:00:5E:4B:5C:5E' and pm['devices'][0]['blocks'] == 3
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


def test_prune_commits_forward_and_delete_chain(stack):
    """Three points on files; prune to keep seq >= 2: base ends up holding point 2's content, delta-0002 is gone."""
    H = {'Authorization': 'Bearer t0k'}
    disk = 'd0dd5d2c-9c7a-44ec-b6bc-d4ce06bb204d'
    srcs = []
    for i, seed in enumerate((21, 22, 23)):
        cu = f'{i:08d}-0000-0000-0000-000000000000'; p = stack.dev / f'virtio-{cu[:8]}'; _random_file(str(p), 3, seed); srcs.append((cu, str(p)))
    tgt = {'uri': 'smb://nas/share/hcm'}
    base_req = {'target': tgt, 'folder': 'F', 'vm': {'uuid': 'vm-1', 'name': 'v'}}
    for i, (cu, _) in enumerate(srcs):
        point = {'kind': 'full', 'seq': 0} if i == 0 else {'kind': 'delta', 'seq': i, 'previous_seq': i - 1}
        req = dict(base_req, point=point, devices=[{'uuid': disk, 'type': 'VIRTIO_DISK', 'clone_uuid': cu, 'size': 3 * MiB, 'map': [{'lower': 0, 'upper': 2}]}])
        j = _wait(stack, stack.client.post('/jobs/backup', json=req, headers=H).get_json()['id']); assert j['state'] == 'done', j
    chain = os.path.join(stack.mounter.root, P.targets.parse_uri(tgt['uri']).key, 'hcm', 'F', 'v_vm-1.chain')
    assert sorted(os.listdir(chain)) == ['base', 'chain.json', 'delta-0001', 'delta-0002']
    j = _wait(stack, stack.client.post('/jobs/prune', json={'target': tgt, 'chain_dir': 'F/v_vm-1.chain', 'keep_min_seq': 2}, headers=H).get_json()['id'])
    assert j['state'] == 'done', j
    assert j['result'] == {'dirs': {'2': 'base'}, 'dropped': 2}
    assert sorted(os.listdir(chain)) == ['base', 'chain.json']
    assert open(os.path.join(chain, 'base', f'{disk}.qcow2'), 'rb').read() == open(srcs[2][1], 'rb').read(), 'base now holds point 2'
    ch = json.load(open(os.path.join(chain, 'chain.json')))
    assert [(p['seq'], p['dir']) for p in ch['points']] == [(2, 'base')]
    assert json.load(open(os.path.join(chain, 'base', 'manifest.json')))['seq'] == 2
    d = _wait(stack, stack.client.post('/jobs/delete_chain', json={'target': tgt, 'chain_dir': 'F/v_vm-1.chain'}, headers=H).get_json()['id'])
    assert d['result'] == {'removed': True} and not os.path.exists(chain)


def test_synthesize_builds_a_new_chain_from_a_point_without_touching_the_old_one(stack):
    """A synthetic full: point 1 (a delta) becomes the base of a new chain with the same files,
    devices and definition and fresh checksums; the source chain is left as it was."""
    H = {'Authorization': 'Bearer t0k'}
    disk = 'd0dd5d2c-9c7a-44ec-b6bc-d4ce06bb204d'
    tgt = {'uri': 'smb://nas/share/hcm'}
    base_req = {'target': tgt, 'folder': 'F', 'vm': {'uuid': 'vm-s', 'name': 's'}, 'chain_dir': 'F/s_old.chain', 'definition': {'name': 's'}}
    srcs = []
    for i, seed in enumerate((31, 32)):
        cu = f'{i + 50:08d}-0000-0000-0000-000000000000'; pth = stack.dev / f'virtio-{cu[:8]}'; _random_file(str(pth), 3, seed); srcs.append(str(pth))
        point = {'kind': 'full', 'seq': 0} if i == 0 else {'kind': 'delta', 'seq': 1, 'previous_seq': 0}
        req = dict(base_req, point=point, devices=[{'uuid': disk, 'type': 'VIRTIO_DISK', 'clone_uuid': cu, 'size': 3 * MiB, 'map': [{'lower': 0, 'upper': 2}], 'serial': 7 + i}])
        assert _wait(stack, stack.client.post('/jobs/backup', json=req, headers=H).get_json()['id'])['state'] == 'done'
    root = os.path.join(stack.mounter.root, P.targets.parse_uri(tgt['uri']).key, 'hcm', 'F')
    before = json.load(open(os.path.join(root, 's_old.chain', 'chain.json')))
    assert stack.client.post('/jobs/synthesize', json={'target': tgt, 'chain_dir': 'F/s_old.chain'}, headers=H).status_code == 400
    j = _wait(stack, stack.client.post('/jobs/synthesize', json={'target': tgt, 'chain_dir': 'F/s_old.chain', 'seq': 1, 'new_chain_dir': 'F/s_new.chain',
                                                                 'run_id': 99}, headers=H).get_json()['id'])
    assert j['state'] == 'done', j
    assert j['result']['point_dir'] == 'base' and j['result']['chain_dir'] == 'F/s_new.chain' and j['result']['bytes_written'] == 3 * MiB
    new = os.path.join(root, 's_new.chain')
    assert open(os.path.join(new, 'base', f'{disk}.qcow2'), 'rb').read() == open(srcs[1], 'rb').read()
    pm = json.load(open(os.path.join(new, 'base', 'manifest.json')))
    assert pm['kind'] == 'full' and pm['seq'] == 0 and pm['synthetic_from'] == {'chain_dir': 'F/s_old.chain', 'seq': 1}
    assert pm['definition'] == {'name': 's'} and pm['devices'][0]['serial'] == 8 and pm['run_id'] == 99
    assert [p['dir'] for p in json.load(open(os.path.join(new, 'chain.json')))['points']] == ['base']
    v = _wait(stack, stack.client.post('/jobs/verify', json={'target': tgt, 'chain_dir': 'F/s_new.chain'}, headers=H).get_json()['id'])
    assert v['result']['ok'], v
    after = json.load(open(os.path.join(root, 's_old.chain', 'chain.json')))
    assert after['points'] == before['points'], 'the source chain is untouched'
    again = _wait(stack, stack.client.post('/jobs/synthesize', json={'target': tgt, 'chain_dir': 'F/s_old.chain', 'seq': 1, 'new_chain_dir': 'F/s_new.chain'}, headers=H).get_json()['id'])
    assert again['state'] == 'failed' and 'already has a base' in again['error']
    bad = _wait(stack, stack.client.post('/jobs/synthesize', json={'target': tgt, 'chain_dir': 'F/s_old.chain', 'seq': 1, 'new_chain_dir': '../x'}, headers=H).get_json()['id'])
    assert bad['state'] == 'failed'


def test_extents_to_intervals_merges_and_rounds():
    f = P.images.extents_to_intervals
    assert f([(0, MiB), (MiB, MiB)]) == [{'lower': 0, 'upper': 1}]
    assert f([(MiB // 2, MiB)]) == [{'lower': 0, 'upper': 1}], 'partial blocks are rounded outwards'
    assert f([(5 * MiB, MiB), (9 * MiB, 2 * MiB), (0, 0)]) == [{'lower': 5, 'upper': 5}, {'lower': 9, 'upper': 10}]
    assert f([]) == []


def test_stage_writes_allocated_blocks_into_a_staging_device_and_flatten_filters(stack):
    H = {'Authorization': 'Bearer t0k'}
    disk = 'd0dd5d2c-9c7a-44ec-b6bc-d4ce06bb204d'; nvram = 'a26166e5-ddf9-4665-83f8-e6dd5254019a'
    src = stack.dev / 'virtio-aaaa0001'; _random_file(str(src), 4, 31)
    with open(str(src), 'r+b') as fh:            # block 2 is all zeros: must not be copied
        fh.seek(2 * MiB); fh.write(bytes(MiB))
    (stack.dev / 'virtio-bbbb0001').write_bytes(b'N' * MiB)
    tgt = {'uri': 'smb://nas/share/hcm'}
    req = {'target': tgt, 'folder': 'F', 'vm': {'uuid': 'vm-9', 'name': 'r'}, 'point': {'kind': 'full', 'seq': 0},
           'devices': [{'uuid': disk, 'type': 'VIRTIO_DISK', 'clone_uuid': 'aaaa0001-0000-0000-0000-000000000000', 'size': 4 * MiB, 'map': [{'lower': 0, 'upper': 3}]},
                       {'uuid': nvram, 'type': 'NVRAM', 'clone_uuid': 'bbbb0001-0000-0000-0000-000000000000', 'size': MiB, 'map': [{'lower': 0, 'upper': 0}]}]}
    assert _wait(stack, stack.client.post('/jobs/backup', json=req, headers=H).get_json()['id'])['state'] == 'done'
    # staging device appears as a (zeroed) hot-added disk on the proxy
    staging = stack.dev / 'virtio-cccc0001'; staging.write_bytes(bytes(4 * MiB))
    j = _wait(stack, stack.client.post('/jobs/stage', json={'target': tgt, 'chain_dir': 'F/r_vm-9.chain', 'seq': 0,
                                                            'devices': [{'uuid': disk, 'staging_clone_uuid': 'cccc0001-0000-0000-0000-000000000000'}]}, headers=H).get_json()['id'])
    assert j['state'] == 'done', j
    assert j['result']['devices'][0]['blocks'] == 3 and j['result']['bytes_written'] == 3 * MiB
    assert open(str(staging), 'rb').read() == open(str(src), 'rb').read()
    # flatten restricted to the NVRAM only, renamed, with a definition file
    f = _wait(stack, stack.client.post('/jobs/flatten', json={'target': tgt, 'chain_dir': 'F/r_vm-9.chain', 'seq': 0, 'out_dir': 'F/_restore/r',
                                                              'only_uuids': [nvram], 'files': {nvram: 'NEW.qcow2'}, 'definition_xml': '<domain/>', 'definition_name': 'r.xml'}, headers=H).get_json()['id'])
    out = os.path.join(stack.mounter.root, P.targets.parse_uri(tgt['uri']).key, 'hcm', 'F', '_restore', 'r')
    assert sorted(os.listdir(out)) == ['NEW.qcow2', 'r.xml'] and f['result']['files'][0]['uuid'] == nvram


def test_a_disk_image_takes_the_whole_clone_and_a_delta_copies_a_grown_tail(stack):
    """HyperCore shows a guest its disk rounded up to whole MiB. An older backup service capped disk
    images at the API capacity, cutting off that last partial MiB (and the backup GPT in it): the
    cap now applies to NVRAM/vTPM only, and the first delta on such a chain copies the missing end."""
    H = {'Authorization': 'Bearer t0k'}
    disk = 'b1b1b1b1-0000-4000-8000-000000000001'
    src = stack.dev / 'virtio-eeee0001'
    data = bytearray(random.Random(7).randbytes(3 * MiB)); src.write_bytes(bytes(data))
    tgt = {'uri': 'smb://nas/share/hcm'}
    full = {'target': tgt, 'folder': 'F', 'vm': {'uuid': 'vm-t', 'name': 't'}, 'point': {'kind': 'full', 'seq': 0},
            'devices': [{'uuid': disk, 'type': 'VIRTIO_DISK', 'clone_uuid': 'eeee0001-0000-0000-0000-000000000000', 'size': 3 * MiB,
                         'image_size': 2 * MiB + 4096, 'map': [{'lower': 0, 'upper': 2}]}]}
    j = _wait(stack, stack.client.post('/jobs/backup', json=full, headers=H).get_json()['id']); assert j['state'] == 'done', j
    assert j['result']['devices'][0]['size'] == 3 * MiB, 'an image_size from an older backup service no longer caps a disk'
    # a chain written capped: shorten the base image and its manifest, as the old code left it
    chain = os.path.join(stack.mounter.root, P.targets.parse_uri(tgt['uri']).key, 'hcm', 'F', 't_vm-t.chain')
    base = os.path.join(chain, 'base', next(f for f in os.listdir(os.path.join(chain, 'base')) if f.endswith('.qcow2')))
    open(base, 'r+b').truncate(2 * MiB + 4096)
    import json as _j
    m = _j.load(open(os.path.join(chain, 'base', 'manifest.json'))); m['devices'][0]['size'] = 2 * MiB + 4096
    _j.dump(m, open(os.path.join(chain, 'base', 'manifest.json'), 'w'))
    # the next delta: nothing changed on the disk (empty snapDiff), yet the end the base lacks is copied
    delta = dict(full, point={'kind': 'delta', 'seq': 1, 'previous_seq': 0},
                 devices=[dict(full['devices'][0], image_size=0, map=[])])
    j = _wait(stack, stack.client.post('/jobs/backup', json=delta, headers=H).get_json()['id']); assert j['state'] == 'done', j
    d = j['result']['devices'][0]
    assert d['size'] == 3 * MiB and d['blocks'] == 1, 'the block holding the old end, up to the new end'
    img = os.path.join(chain, j['result']['point_dir'], d['file'])
    assert open(img, 'rb').read()[2 * MiB:] == bytes(data[2 * MiB:])
    assert P.jobs.grown_tail(None, 5) is None and P.jobs.grown_tail(3 * MiB, 3 * MiB) is None
    assert P.jobs.grown_tail(100_000_000_000, 100_000_595_968) == {'lower': 95367, 'upper': 95367}


def test_copier_clamps_to_a_smaller_destination(tmp_path):
    src, dst = str(tmp_path / 'src'), str(tmp_path / 'dst')
    _random_file(src, 1, 44); open(dst, 'wb').truncate(540672)
    written = P.copier.copy_intervals(src, dst, [{'lower': 0, 'upper': 0}], writers=1)
    assert written == 540672 and os.path.getsize(dst) == 540672
    assert open(dst, 'rb').read() == open(src, 'rb').read()[:540672]


def test_backup_honours_image_size_and_flatten_shrinks(stack):
    H = {'Authorization': 'Bearer t0k'}
    nv = 'a26166e5-ddf9-4665-83f8-e6dd5254019a'
    (stack.dev / 'virtio-dddd0001').write_bytes(b'V' * MiB)
    tgt = {'uri': 'smb://nas/share/hcm'}
    req = {'target': tgt, 'folder': 'F', 'vm': {'uuid': 'vm-n', 'name': 'n'}, 'point': {'kind': 'full', 'seq': 0},
           'devices': [{'uuid': nv, 'type': 'NVRAM', 'clone_uuid': 'dddd0001-0000-0000-0000-000000000000', 'size': MiB, 'image_size': 540672, 'map': [{'lower': 0, 'upper': 0}]}]}
    j = _wait(stack, stack.client.post('/jobs/backup', json=req, headers=H).get_json()['id']); assert j['state'] == 'done', j
    chain = os.path.join(stack.mounter.root, P.targets.parse_uri(tgt['uri']).key, 'hcm', 'F', 'n_vm-n.chain')
    assert os.path.getsize(os.path.join(chain, 'base', f'nvram-{nv}.qcow2')) == 540672 and j['result']['devices'][0]['size'] == 540672
    # an older chain with a 1 MiB NVRAM: flatten shrinks it to the requested size
    big = os.path.join(chain, 'base', f'nvram-{nv}.qcow2'); open(big, 'ab').truncate(MiB)
    import json as _j
    m = _j.load(open(os.path.join(chain, 'base', 'manifest.json'))); m['devices'][0]['size'] = MiB; _j.dump(m, open(os.path.join(chain, 'base', 'manifest.json'), 'w'))
    f = _wait(stack, stack.client.post('/jobs/flatten', json={'target': tgt, 'chain_dir': 'F/n_vm-n.chain', 'seq': 0, 'out_dir': 'F/_restore/n', 'sizes': {nv: 540672}}, headers=H).get_json()['id'])
    assert f['state'] == 'done', f
    assert os.path.getsize(os.path.join(os.path.dirname(chain), '_restore', 'n', f'nvram-{nv}.qcow2')) == 540672


# ----------------------------------------------------------------------------- file-level recovery (mount manager)
SFDISK = '{"partitiontable": {"label": "gpt", "unit": "sectors", "partitions": [{"node": "/dev/nbd1p1", "start": 2048, "size": 1363968, "type": "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"}, {"node": "/dev/nbd1p2", "start": 1366016, "size": 3907584, "type": "0FC63DAF-8483-4772-8E79-3D69D8477DE4"}]}}'


def test_sfdisk_and_blkid_parsing():
    parts = P.mounts.partitions_from_sfdisk(SFDISK, '/dev/nbd1', 15_000_928_256)
    assert parts[0] == {'name': 'p1', 'start_bytes': 2048 * 512, 'size_bytes': 1363968 * 512, 'type': 'C12A7328-F81F-11D2-BA4B-00A0C93EC93B'}
    assert parts[1]['name'] == 'p2' and parts[1]['start_bytes'] == 1366016 * 512
    whole = P.mounts.partitions_from_sfdisk('', '/dev/nbd2', 123)
    assert whole == [{'name': 'disk', 'start_bytes': 0, 'size_bytes': 123, 'type': None}]
    assert P.mounts.parse_blkid('DEVNAME=/dev/loop3\nUUID=a-b\nTYPE=ext4\nLABEL=root\n') == {'DEVNAME': '/dev/loop3', 'UUID': 'a-b', 'TYPE': 'ext4', 'LABEL': 'root'}


class _FakeRun:
    """Answers the system commands the mount manager issues and records them; mounts become plain directories."""
    def __init__(self, tmp, fstypes):
        self.tmp, self.fstypes, self.cmds, self.loops, self.dms = tmp, fstypes, [], 0, []
    def __call__(self, cmd, **kw):
        self.cmds.append(cmd); out = ''
        if cmd[0] == 'blockdev': out = '2000683008'
        elif cmd[0] == 'sfdisk': out = SFDISK
        elif cmd[0] == 'losetup': self.loops += 1; out = f'/dev/loop{self.loops}'
        elif cmd[0] == 'dmsetup' and cmd[1] == 'create': self.dms.append(cmd[2])
        elif cmd[0] == 'dmsetup' and cmd[1] == 'info': out = str(self.dms.index(cmd[-1]) + 1)       # minor = creation order
        elif cmd[0] == 'blkid':
            dev = cmd[-1]; idx = f'p{dev.rsplit("-", 1)[-1]}' if dev.startswith('/dev/dm-') else 'p2'; out = f'TYPE={self.fstypes.get(idx, "ext4")}\nLABEL={idx}\n'
        elif cmd[0] == 'mount':
            mp = cmd[-1]; (pathlib := __import__('pathlib')).Path(mp, 'etc').mkdir(parents=True, exist_ok=True)
            pathlib.Path(mp, 'etc', 'hostname').write_text('exptest\n'); pathlib.Path(mp, 'etc', 'hosts').write_text('127.0.0.1 localhost\n')
        return types.SimpleNamespace(returncode=0, stdout=out, stderr='')


@pytest.fixture
def flr(tmp_path):
    cfg = P.config.Config(api_token='t0k', data_dir=str(tmp_path / 'data'), dev_dir=str(tmp_path / 'dev'), backend='file')
    mounter = _FakeMounter(str(tmp_path / 'targets'))
    # a chain with one point and one disk image (content irrelevant: the fake `run` fabricates partitions)
    root = mounter.acquire('smb://nas/share/hcm', None, None)
    chain = os.path.join(root, 'F', 'v_vm-1.chain'); os.makedirs(os.path.join(chain, 'base'))
    open(os.path.join(chain, 'base', 'disk.qcow2'), 'wb').truncate(4 * MiB)
    P.manifest.write_json(os.path.join(chain, 'base', 'manifest.json'), {'seq': 0, 'devices': [{'uuid': 'd', 'type': 'VIRTIO_DISK', 'file': 'disk.qcow2', 'size': 4 * MiB},
                                                                                              {'uuid': 'n', 'type': 'NVRAM', 'file': 'nvram.qcow2', 'size': 540672}]})
    P.manifest.save_chain(chain, {'points': [{'seq': 0, 'dir': 'base', 'kind': 'full'}]})
    run = _FakeRun(tmp_path, {'p1': 'vfat', 'p2': 'ext4'})
    mm = P.mounts.MountManager(cfg, P.images.FileBackend(), mounter, run=run, idle_seconds=1)
    return types.SimpleNamespace(mm=mm, run=run, cfg=cfg, mounter=mounter)


def test_mount_browse_zip_and_unmount(flr):
    m = flr.mm.mount({'uri': 'smb://nas/share/hcm'}, 'F/v_vm-1.chain', 0)
    assert [f['name'] for f in m.filesystems] == ['disk1-p1', 'disk1-p2'] and m.filesystems[1]['fstype'] == 'ext4'
    cmds = flr.run.cmds
    assert any(c[0] == 'losetup' and '--read-only' in c and '--offset' in c and c[c.index('--offset') + 1] == str(2048 * 512) for c in cmds), 'read-only loop at the partition offset'
    assert any(c[0] == 'dmsetup' and c[1] == 'create' and '--noudevsync' in c and ' snapshot /dev/loop' in c[-1] for c in cmds), 'copy-on-write overlay'
    assert any(c[0] == 'blkid' and '-p' in c and c[-1] == '/dev/dm-1' for c in cmds), 'probed via the kernel dm node'
    assert any(c[:4] == ['mount', '-t', 'ext4', '-o'] and c[4] == 'ro,noload' for c in cmds), 'ext4 mounted read-only without journal replay'
    top = flr.mm.ls(m.id, '/')
    assert [e['name'] for e in top['entries']] == ['disk1-p1', 'disk1-p2'] and top['entries'][1]['fstype'] == 'ext4'
    lst = flr.mm.ls(m.id, '/disk1-p2/etc')
    assert [e['name'] for e in lst['entries']] == ['hostname', 'hosts'] and lst['entries'][0]['kind'] == 'file' and lst['entries'][0]['size'] == 8
    fh, size, name = flr.mm.open_file(m.id, '/disk1-p2/etc/hostname'); assert fh.read() == b'exptest\n' and size == 8 and name == 'hostname'; fh.close()
    escaped = flr.mm.ls(m.id, '/disk1-p2/../../etc')          # '..' is neutralised, never escapes the mount
    assert [e['name'] for e in escaped['entries']] == ['hostname', 'hosts']
    with pytest.raises(FileNotFoundError):
        flr.mm.open_file(m.id, '/nope/x')
    import zipfile, io
    data = b''.join(flr.mm.iter_zip(m.id, ['/disk1-p2/etc', '/disk1-p1']))
    names = sorted(zipfile.ZipFile(io.BytesIO(data)).namelist())
    assert names == ['disk1-p1/etc/hostname', 'disk1-p1/etc/hosts', 'disk1-p2/etc/hostname', 'disk1-p2/etc/hosts']
    flr.mm.unmount(m.id)
    tail = [c[0] for c in flr.run.cmds[-8:]]
    assert tail.count('umount') == 2 and 'dmsetup' in tail and 'losetup' in tail and 'smb://nas/share/hcm' in flr.mounter.released
    assert not os.path.exists(m.root) and m.id not in flr.mm.mounts


def test_nothing_mountable_names_the_reason_per_partition(flr):
    """The proxy's log is out of reach for most admins: the error itself says why each partition
    was left out."""
    orig = flr.run.__call__
    def run(cmd, **kw):
        if cmd[0] == 'blkid':
            return types.SimpleNamespace(returncode=0, stdout='TYPE=zfs_member\n' if cmd[-1] == '/dev/dm-1' else '', stderr='')
        return orig(cmd, **kw)
    flr.mm.run = run
    with pytest.raises(RuntimeError) as e:
        flr.mm.mount({'uri': 'smb://nas/share/hcm'}, 'F/v_vm-1.chain', 0)
    assert str(e.value) == ("no mountable filesystem found in this point (disk1-p1: unsupported filesystem 'zfs_member'; "
                            "disk1-p2: no filesystem recognised (blkid returned nothing))")


def test_an_unreadable_partition_table_and_a_failing_mount_are_reported(flr):
    orig = flr.run.__call__
    def run(cmd, **kw):
        if cmd[0] == 'sfdisk':
            return types.SimpleNamespace(returncode=1, stdout='', stderr='')
        if cmd[0] == 'blkid':
            return types.SimpleNamespace(returncode=0, stdout='PTUUID=x\nPTTYPE=gpt\n', stderr='')
        return orig(cmd, **kw)
    flr.mm.run = run
    with pytest.raises(RuntimeError, match=r'disk1-disk: a gpt partition table that could not be read'):
        flr.mm.mount({'uri': 'smb://nas/share/hcm'}, 'F/v_vm-1.chain', 0)
    # a filesystem that will not mount is left out with its reason; the others stay browsable
    mounts = []
    def run2(cmd, **kw):
        if cmd[0] == 'mount':
            mounts.append(cmd)
            if len(mounts) == 1:
                return types.SimpleNamespace(returncode=32, stdout='', stderr='wrong fs type, bad option, bad superblock')
        return orig(cmd, **kw)
    flr.mm.run = run2
    m = flr.mm.mount({'uri': 'smb://nas/share/hcm'}, 'F/v_vm-1.chain', 0)
    assert [f['name'] for f in m.filesystems] == ['disk1-p2']
    assert m.public()['skipped'][0]['name'] == 'disk1-p1' and 'mount failed' in m.public()['skipped'][0]['reason']
    assert 'bad superblock' in m.public()['skipped'][0]['reason']


def test_a_gpt_disk_read_as_its_protective_mbr_is_reported_with_what_sector_1_holds(flr, monkeypatch):
    pmbr = '{"partitiontable": {"label": "dos", "unit": "sectors", "partitions": [{"node": "/dev/nbd2p1", "start": 1, "size": 195312499, "type": "ee"}]}}'
    orig = flr.run.__call__
    def run(cmd, **kw):
        if cmd[0] == 'sfdisk':
            return types.SimpleNamespace(returncode=0, stdout=pmbr, stderr='GPT PMBR size mismatch\n')
        if cmd[:2] == ['blockdev', '--getss']:
            return types.SimpleNamespace(returncode=0, stdout='512', stderr='')
        return orig(cmd, **kw)
    flr.mm.run = run
    monkeypatch.setattr(P.mounts.MountManager, '_sector', staticmethod(lambda dev, lba: b'\x00' * 512))
    with pytest.raises(RuntimeError) as e:
        flr.mm.mount({'uri': 'smb://nas/share/hcm'}, 'F/v_vm-1.chain', 0)
    assert ('disk1: only the protective MBR of a GPT disk could be read; sector 1 holds zeros, sector size 512; '
            'sfdisk: GPT PMBR size mismatch') in str(e.value)
    assert not any(c[0] == 'losetup' for c in flr.run.cmds), 'nothing attached for a table that was not read'
    assert P.mounts.describe_sector(b'EFI PART' + b'\x00' * 504) == 'a GPT header (EFI PART)'
    assert P.mounts.describe_sector(b'\xeb\x63\x90' + b'\x00' * 509).startswith('other data: eb 63 90')


def _gpt_disk(disk_sectors, parts):
    """A GPT disk image (bytes up to the end of the partition table) with valid checksums."""
    import struct, zlib, uuid as _u
    ents = bytearray(128 * 128)
    for i, (first, last, tguid) in enumerate(parts):
        struct.pack_into('<16s16sQQ', ents, i * 128, _u.UUID(tguid).bytes_le, _u.uuid4().bytes_le, first, last)
    hdr = bytearray(92)
    struct.pack_into('<8sIIIIQQQQ16sQIII', hdr, 0, b'EFI PART', 0x10000, 92, 0, 0, 1, disk_sectors - 1, 34, disk_sectors - 34,
                     _u.uuid4().bytes_le, 2, 128, 128, zlib.crc32(bytes(ents)) & 0xffffffff)
    struct.pack_into('<I', hdr, 16, zlib.crc32(bytes(hdr)) & 0xffffffff)
    mbr = bytearray(512); mbr[510:512] = b'\x55\xaa'
    return bytes(mbr) + bytes(hdr) + bytes(420) + bytes(ents)


def test_a_gpt_that_sfdisk_refuses_for_a_short_image_is_read_directly(flr, monkeypatch):
    """container01: a 100,000,595,968-byte disk backed up as a 100,000,000,000-byte image. Its GPT
    claims more disk than the image has, so sfdisk falls back to the protective MBR; the checksums
    hold and every partition fits, so the proxy reads the table itself."""
    esp, linux = 'C12A7328-F81F-11D2-BA4B-00A0C93EC93B', '0FC63DAF-8483-4772-8E79-3D69D8477DE4'
    disk = _gpt_disk(195313664, [(2048, 2203647, esp), (2203648, 6397951, linux), (6397952, 195311615, linux)])
    pmbr = '{"partitiontable": {"label": "dos", "unit": "sectors", "partitions": [{"node": "/dev/nbd2p1", "start": 1, "size": 195312499, "type": "ee"}]}}'
    orig = flr.run.__call__
    def run(cmd, **kw):
        if cmd[0] == 'sfdisk':
            return types.SimpleNamespace(returncode=0, stdout=pmbr, stderr='')
        if cmd[0] == 'blockdev' and cmd[1] == '--getsize64':
            return types.SimpleNamespace(returncode=0, stdout='100000000000', stderr='')
        return orig(cmd, **kw)
    flr.mm.run = run
    flr.run.fstypes = {'p1': 'vfat', 'p2': 'ext4', 'p3': 'ext4'}
    monkeypatch.setattr(P.mounts.MountManager, '_read', staticmethod(lambda dev, off, n: disk[off:off + n]))
    m = flr.mm.mount({'uri': 'smb://nas/share/hcm'}, 'F/v_vm-1.chain', 0)
    assert [f['name'] for f in m.filesystems] == ['disk1-p1', 'disk1-p2', 'disk1-p3']
    loops = [c for c in flr.run.cmds if c[0] == 'losetup' and '--offset' in c]
    assert loops[0][loops[0].index('--offset') + 1] == str(2048 * 512)
    # a broken partition array is not trusted
    bad = bytearray(disk); bad[1024 + 40] ^= 0xFF
    assert P.mounts.parse_gpt(bytes(bad[512:1024]), lambda off, n: bytes(bad[off:off + n]), 100_000_000_000) is None
    # a partition beyond the image is left out, the others kept
    tight = P.mounts.parse_gpt(disk[512:1024], lambda off, n: disk[off:off + n], 6397952 * 512)
    assert [p['name'] for p in tight] == ['p1', 'p2']


BITLK_DUMP = """Info for BITLK device /dev/dm-3.
Version:        2
GUID:           0d2f9a6e-1111-4c4c-9b9b-123456789abc
Sector size:    512 [bytes]
Volume size:    49015734272 [bytes]
Created:        Sat Oct 31 15:02:11 2026
Description:    W11BTEST C: 31-10-2026
Cipher name:    aes
Cipher mode:    xts-plain64
Cipher key:     128 bits

Keyslots:
 0: VMK
        GUID: 9f8e7d6c-0000-4111-8222-333344445555
        Protection:     VMK protected with TPM
        Salt:           00112233445566778899aabbccddeeff
        Key data size:  44 [bytes]
 1: VMK
        GUID: 4a3b9c1d-2e5f-4071-8899-aabbccddeeff
        Protection:     VMK protected with recovery passphrase
        Salt:           ffeeddccbbaa99887766554433221100
        Key data size:  44 [bytes]
 2: FVEK
        Key data size:  44 [bytes]
"""


def test_bitlk_dump_gives_the_recovery_key_id_and_the_description():
    d = P.mounts.parse_bitlk_dump(BITLK_DUMP)
    assert d == {'key_ids': ['4A3B9C1D-2E5F-4071-8899-AABBCCDDEEFF'], 'description': 'W11BTEST C: 31-10-2026'}
    assert P.mounts.parse_bitlk_dump('') == {'key_ids': [], 'description': None}


def _crypt_run(flr, inner='ntfs', good='111111-222222-333333-444444-555555-666666-777777-888888'):
    """p2 is a BitLocker volume; cryptsetup opens it only with `good`, read on stdin."""
    flr.run.fstypes = {'p1': 'vfat', 'p2': 'BitLocker'}
    orig = flr.run.__call__
    seen = {'inputs': [], 'cmds': []}
    def run(cmd, **kw):
        seen['cmds'].append(cmd)
        if cmd[:2] == ['cryptsetup', 'open']:
            seen['inputs'].append(kw.get('input'))
            ok = kw.get('input') == good + '\n'
            return types.SimpleNamespace(returncode=0 if ok else 2, stdout='', stderr='' if ok else 'No key available with this passphrase.')
        if cmd[:2] == ['cryptsetup', 'bitlkDump']:
            return types.SimpleNamespace(returncode=0, stdout=BITLK_DUMP, stderr='')
        if cmd[:2] == ['dmsetup', 'info'] and cmd[-1].endswith('-open'):
            return types.SimpleNamespace(returncode=0, stdout='77', stderr='')
        if cmd[0] == 'blkid' and cmd[-1] == '/dev/dm-77':
            return types.SimpleNamespace(returncode=0, stdout=f'TYPE={inner}\nLABEL=Data\n', stderr='')
        return orig(cmd, **kw)
    flr.mm.run = run
    return seen, good


def test_a_bitlocker_volume_is_reported_locked_without_a_key(flr):
    _crypt_run(flr)
    m = flr.mm.mount({'uri': 'smb://nas/share/hcm'}, 'F/v_vm-1.chain', 0)
    assert [f['name'] for f in m.filesystems] == ['disk1-p1'], 'the unencrypted partition stays browsable'
    locked = m.public()['locked']
    assert [(v['name'], v['kind'], v['reason']) for v in locked] == [('disk1-p2', 'BitLocker', 'key needed')]
    assert locked[0]['key_ids'] == ['4A3B9C1D-2E5F-4071-8899-AABBCCDDEEFF'] and locked[0]['description'].startswith('W11BTEST C:')
    flr.mm.unmount(m.id)


def test_a_bitlocker_volume_opens_read_only_with_its_recovery_key(flr):
    seen, good = _crypt_run(flr)
    m = flr.mm.mount({'uri': 'smb://nas/share/hcm'}, 'F/v_vm-1.chain', 0, keys=['wrong-one', ' ' + good + ' '])
    assert [f['name'] for f in m.filesystems] == ['disk1-p1', 'disk1-p2'] and m.filesystems[1]['fstype'] == 'ntfs'
    assert m.locked == [] and m.keys == [], 'keys are dropped once the mount is made'
    opens = [c for c in seen['cmds'] if c[0] == 'cryptsetup']
    assert all(c[:4] == ['cryptsetup', 'open', '--readonly', '--type'] and c[4] == 'bitlk' for c in opens)
    assert seen['inputs'] == ['wrong-one\n', good + '\n'], 'keys go on stdin, tried in turn, trimmed'
    assert not any(good in ' '.join(c) for c in seen['cmds']), 'a key never appears on a command line'
    assert any(c[:3] == ['mount', '-t', 'ntfs3'] and c[-2] == '/dev/dm-77' for c in flr.run.cmds), 'the opened volume is mounted'
    flr.mm.unmount(m.id)
    removes = [c[-1] for c in flr.run.cmds if c[:2] == ['dmsetup', 'remove']]
    assert removes.index(next(r for r in removes if r.endswith('-open'))) < removes.index(next(r for r in removes if r.endswith('disk1-p2'))), \
        'the opened volume is closed before the snapshot under it'


def test_luks_opens_writable_on_the_private_view_so_lvm_inside_can_be_imported(flr):
    """Ubuntu's encrypted layout: LUKS holding LVM. vgimportclone writes the temporary VG name, so
    the LUKS volume opens writable on the copy-on-write view (never on the backup); BitLocker stays
    read-only."""
    seen, good = _crypt_run(flr, inner='LVM2_member')
    flr.run.fstypes = {'p1': 'vfat', 'p2': 'crypto_LUKS'}
    orig = flr.mm.run
    def run(cmd, **kw):
        if cmd[0] == 'pvs': return types.SimpleNamespace(returncode=0, stdout='  hcmflrabc\n', stderr='')
        if cmd[0] == 'lvs': return types.SimpleNamespace(returncode=0, stdout='  ubuntu-lv 9\n', stderr='')
        if cmd[0] == 'blkid' and cmd[-1] == '/dev/dm-9': return types.SimpleNamespace(returncode=0, stdout='TYPE=ext4\n', stderr='')
        return orig(cmd, **kw)
    flr.mm.run = run
    m = flr.mm.mount({'uri': 'smb://nas/share/hcm'}, 'F/v_vm-1.chain', 0, keys=[good])
    assert [f['name'] for f in m.filesystems] == ['disk1-p1', 'disk1-p2-ubuntu-lv']
    opens = [c for c in seen['cmds'] if c[:2] == ['cryptsetup', 'open']]
    assert opens and '--readonly' not in opens[0] and opens[0][2:4] == ['--type', 'luks']
    assert any(c[0] == 'vgimportclone' and '/dev/dm-77' in c for c in seen['cmds']), 'LVM is imported from the opened volume'
    flr.mm.unmount(m.id)


def test_partitions_empty_by_design_are_not_reported(flr):
    msr = '{"partitiontable": {"label": "gpt", "partitions": [{"start": 2048, "size": 32768, "type": "E3C9E316-0B5C-4DB8-817D-F92DF00215AE"}, {"start": 34816, "size": 3907584, "type": "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7"}]}}'
    orig = flr.run.__call__
    def run(cmd, **kw):
        if cmd[0] == 'sfdisk': return types.SimpleNamespace(returncode=0, stdout=msr, stderr='')
        if cmd[0] == 'blkid': return types.SimpleNamespace(returncode=0, stdout='' if cmd[-1] == '/dev/dm-1' else 'TYPE=ntfs\n', stderr='')
        return orig(cmd, **kw)
    flr.mm.run = run
    m = flr.mm.mount({'uri': 'smb://nas/share/hcm'}, 'F/v_vm-1.chain', 0)
    assert [f['name'] for f in m.filesystems] == ['disk1-p2'] and m.skipped == [], 'the Microsoft reserved partition is not a "not mounted" line'
    flr.mm.unmount(m.id)


def test_only_locked_volumes_and_a_wrong_key_give_a_locked_answer(flr):
    seen, good = _crypt_run(flr)
    flr.run.fstypes = {'p1': 'crypto_LUKS', 'p2': 'BitLocker'}
    with pytest.raises(P.mounts.LockedVolumes) as e:
        flr.mm.mount({'uri': 'smb://nas/share/hcm'}, 'F/v_vm-1.chain', 0, keys=['nope'])
    assert [(v['name'], v['kind'], v['reason']) for v in e.value.locked] == [('disk1-p1', 'LUKS', 'no key matched'),
                                                                            ('disk1-p2', 'BitLocker', 'no key matched')]
    assert 'key_ids' not in e.value.locked[0] and e.value.locked[1]['key_ids'] == ['4A3B9C1D-2E5F-4071-8899-AABBCCDDEEFF']
    assert 'nope' not in str(e.value)
    flr.run.dms = []                  # the fake numbers dm devices by creation; start again for the next mount
    app = P.api.create_app(flr.cfg, types.SimpleNamespace(backend=P.images.FileBackend(), list=lambda: [], get=lambda i: None, jobs={}), flr.mm)
    r = app.test_client().post('/mounts', json={'target': {'uri': 'smb://nas/share/hcm'}, 'chain_dir': 'F/v_vm-1.chain', 'seq': 0},
                               headers={'Authorization': 'Bearer t0k'})
    assert r.status_code == 422 and r.get_json()['locked'][0]['reason'] == 'key needed'
    r = app.test_client().post('/mounts', json={'target': {'uri': 'smb://nas/share/hcm'}, 'chain_dir': 'F/v_vm-1.chain', 'seq': 0, 'keys': 'x'},
                               headers={'Authorization': 'Bearer t0k'})
    assert r.status_code == 400


def test_lvm_member_is_imported_under_a_temporary_vg(flr):
    flr.run.fstypes = {'p1': 'vfat', 'p2': 'LVM2_member'}
    orig = flr.run.__call__
    def run(cmd, **kw):
        if cmd[0] == 'pvs': return types.SimpleNamespace(returncode=0, stdout='  hcmflrabc\n', stderr='')
        if cmd[0] == 'lvs': return types.SimpleNamespace(returncode=0, stdout='  root 9\n', stderr='')
        if cmd[0] == 'blkid' and cmd[-1] == '/dev/dm-9': return types.SimpleNamespace(returncode=0, stdout='TYPE=ext4\n', stderr='')
        return orig(cmd, **kw)
    flr.mm.run = run
    m = flr.mm.mount({'uri': 'smb://nas/share/hcm'}, 'F/v_vm-1.chain', 0)
    assert [f['name'] for f in m.filesystems] == ['disk1-p1', 'disk1-p2-root']
    assert any(c[:4] == ['mount', '-t', 'ext4', '-o'] and c[-2] == '/dev/dm-9' for c in flr.run.cmds), 'LV mounted via its kernel node'
    assert any(c[0] == 'vgimportclone' and c[1] == '--basevgname' and '--devices' in c for c in flr.run.cmds), 'the guest VG gets a temporary name, LVM sees only the copy'
    assert any(c[:2] == ['vgchange', '-ay'] and c[-1] == 'hcmflrabc' and '--devices' in c for c in flr.run.cmds)
    flr.mm.unmount(m.id)
    assert any(c[:2] == ['vgchange', '-an'] and c[-1] == 'hcmflrabc' for c in flr.run.cmds)


def test_idle_mounts_are_swept_and_api_routes_work(flr):
    import time
    app = P.api.create_app(flr.cfg, types.SimpleNamespace(backend=P.images.FileBackend(), list=lambda: [], get=lambda i: None, jobs={}), flr.mm)
    c = app.test_client(); H = {'Authorization': 'Bearer t0k'}
    r = c.post('/mounts', json={'target': {'uri': 'smb://nas/share/hcm'}, 'chain_dir': 'F/v_vm-1.chain', 'seq': 0}, headers=H)
    assert r.status_code == 201, r.get_json(); mid = r.get_json()['id']
    assert c.get(f'/mounts/{mid}/ls?path=/disk1-p2/etc', headers=H).get_json()['entries'][0]['name'] == 'hostname'
    f = c.get(f'/mounts/{mid}/file?path=/disk1-p2/etc/hosts', headers=H)
    assert f.status_code == 200 and f.data == b'127.0.0.1 localhost\n' and 'hosts' in f.headers['Content-Disposition']
    z = c.get(f'/mounts/{mid}/zip?paths=/disk1-p2/etc&name=etc.zip', headers=H)
    assert z.status_code == 200 and z.mimetype == 'application/zip' and len(z.data) > 100
    assert c.get(f'/mounts/{mid}/ls?path=/missing', headers=H).status_code == 404
    assert c.post('/mounts', json={'target': {'uri': 'smb://nas/share/hcm'}, 'chain_dir': 'F/v_vm-1.chain', 'seq': 7}, headers=H).status_code == 502
    flr.mm.mounts[mid].last_used -= 5
    assert flr.mm.sweep_idle() == 1 and c.get(f'/mounts/{mid}/ls', headers=H).status_code == 404


def test_iso_command_size_and_stream(flr, monkeypatch):
    m = flr.mm.mount({'uri': 'smb://nas/share/hcm'}, 'F/v_vm-1.chain', 0)
    cmd = flr.mm.iso_command(m.id, ['/disk1-p2/etc', '/disk1-p2/etc/hosts'])
    assert cmd[0] == 'genisoimage' and '-graft-points' in cmd and cmd[-2].startswith('disk1-p2/etc=') and cmd[-1].startswith('disk1-p2/etc/hosts=')
    assert '-print-size' in flr.mm.iso_command(m.id, ['/disk1-p2/etc'], print_size=True)
    with pytest.raises(FileNotFoundError):
        flr.mm.iso_command(m.id, ['/disk1-p2/nope'])
    real = flr.mm.run
    def run(cmd, **kw):
        if cmd[0] == 'genisoimage': return types.SimpleNamespace(returncode=0, stdout='Total extents scheduled to be written = 182\n182\n', stderr='')
        return real(cmd, **kw)
    flr.mm.run = run
    assert flr.mm.iso_size(m.id, ['/disk1-p2/etc']) == 182 * 2048

    class FakeProc:
        def __init__(self, *a, **k): self.stdout = io.BytesIO(b'ISO' * 1000); self.stderr = io.BytesIO(b'')
        def wait(self, timeout=None): return 0
    import io
    data = b''.join(flr.mm.iter_iso(m.id, ['/disk1-p2/etc'], chunk=1024, popen=FakeProc))
    assert data == b'ISO' * 1000
    flr.mm.unmount(m.id)


def test_self_signed_certificate_and_fingerprint(tmp_path):
    import shutil
    if not shutil.which('openssl'):
        pytest.skip('openssl not installed')
    sys.path.insert(0, _PROXY)
    try:
        saved = sys.modules.pop('tls', None); tlsm = __import__('importlib').import_module('tls')
    finally:
        sys.path.pop(0); sys.modules.pop('tls', None)
        if saved: sys.modules['tls'] = saved
    crt, key = tlsm.ensure_self_signed(str(tmp_path / 'tls'))
    assert os.path.exists(crt) and os.path.exists(key) and (os.stat(key).st_mode & 0o777) == 0o600
    fp = tlsm.fingerprint(crt); assert fp and len(fp) == 64
    assert tlsm.ensure_self_signed(str(tmp_path / 'tls')) == (crt, key), 'kept across restarts'
    cfg = P.config.Config(data_dir=str(tmp_path), tls='off'); assert tlsm.context_for(cfg) == (None, None)
    cfg = P.config.Config(data_dir=str(tmp_path), tls='auto'); ctx, fp2 = tlsm.context_for(cfg); assert ctx == (crt, key) and fp2 == fp
    with pytest.raises(RuntimeError):
        tlsm.context_for(P.config.Config(data_dir=str(tmp_path), tls='cert', tls_cert='/nope.crt', tls_key='/nope.key'))


# ---------------------------------------------------------------- stale nbd state (lab incident 2026-10-21)
def test_copy_intervals_closes_every_descriptor(tmp_path):
    src = tmp_path / 'src'; dst = tmp_path / 'dst'
    src.write_bytes(os.urandom(2 << 20)); dst.write_bytes(b'\0' * (2 << 20))
    before = len(os.listdir('/dev/fd'))
    for _ in range(5):
        P.copier.copy_intervals(str(src), str(dst), [{'lower': 0, 'upper': 1}], writers=2)
    assert len(os.listdir('/dev/fd')) == before       # the size probes used to leak one descriptor per call
    assert P.copier.size_of(str(src)) == 2 << 20 and P.copier.size_of(str(tmp_path / 'missing')) == 0


def test_nbd_backend_retires_a_slot_that_refuses_io(tmp_path, monkeypatch):
    pool = P.images.NbdPool(3, dev_dir=str(tmp_path))
    be = P.images.NbdBackend(pool)
    calls = []
    monkeypatch.setattr(be, '_connect', lambda slot, path, ro, cache=None: calls.append(('connect', slot)))
    monkeypatch.setattr(be, '_disconnect', lambda slot, strict: calls.append(('disconnect', slot, strict)))
    bad = {f'{tmp_path}/nbd0', f'{tmp_path}/nbd1'}

    def probe(slot):
        if slot in bad:
            raise OSError(5, 'Input/output error')
    monkeypatch.setattr(be, '_probe', probe)
    with be._attached('img.qcow2', False) as slot:
        assert slot == f'{tmp_path}/nbd2'
    assert pool.retired == [f'{tmp_path}/nbd0', f'{tmp_path}/nbd1'] and pool.free == 1
    assert ('disconnect', f'{tmp_path}/nbd0', False) in calls and ('disconnect', f'{tmp_path}/nbd2', True) in calls
    bad.add(f'{tmp_path}/nbd2')
    with pytest.raises(RuntimeError, match='every nbd slot'):
        with be._attached('img.qcow2', False):
            pass
    with pytest.raises(RuntimeError, match='retired'):
        pool.acquire()


def test_nbd_backend_releases_slot_when_connect_fails(tmp_path, monkeypatch):
    pool = P.images.NbdPool(1, dev_dir=str(tmp_path))
    be = P.images.NbdBackend(pool)

    def boom(slot, path, ro, cache=None):
        raise RuntimeError('qemu-nbd failed')
    monkeypatch.setattr(be, '_connect', boom)
    with pytest.raises(RuntimeError, match='qemu-nbd'):
        with be._attached('img.qcow2', False):
            pass
    assert pool.free == 1 and not pool.retired


def test_mount_manager_cleans_leftovers_of_a_previous_life(tmp_path):
    cfg = types.SimpleNamespace(data_dir=str(tmp_path))
    base = str(tmp_path / 'flr')
    outputs = {'vgs': ' hcmflr12ab \n ubuntu-vg\n', 'dmsetup': 'hcmflr-12ab-sda2\t(252:3)\nubuntu--vg-ubuntu--lv\t(252:0)\n',
               'losetup': f'/dev/loop7: []: (/dev/nbd3), offset 1048576\n/dev/loop8: [64512]:17 ({base}/12ab/cow-sda2)\n/dev/loop0: [64512]:1 (/var/lib/snapd/x.snap)\n'}
    cmds = []

    def run(cmd, capture_output=True, text=True, timeout=600):
        cmds.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout=outputs.get(cmd[0], ''), stderr='')
    mm = P.mounts.MountManager(cfg, P.images.FileBackend(), types.SimpleNamespace(release=lambda u: None), run=run)
    os.makedirs(os.path.join(base, '12ab', 'mnt'), exist_ok=True)
    n = mm.cleanup_leftovers()
    assert n['vgs'] == 1 and n['dm'] == 1 and n['loops'] == 2
    assert ['vgchange', '-an', 'hcmflr12ab'] in cmds and ['dmsetup', 'remove', '--noudevsync', '--retry', 'hcmflr-12ab-sda2'] in cmds
    assert ['losetup', '-d', '/dev/loop7'] in cmds and ['losetup', '-d', '/dev/loop8'] in cmds and ['losetup', '-d', '/dev/loop0'] not in cmds
    assert not os.path.exists(os.path.join(base, '12ab'))


def test_backup_honours_the_chain_dir_named_by_the_service(stack):
    """Two fulls of one VM land in two directories: the service names each chain; '..' is refused."""
    H = {'Authorization': 'Bearer t0k'}
    (stack.dev / 'virtio-eeee4444').write_bytes(b'E' * MiB)
    req = {'target': {'uri': 'nfs://nas/export'}, 'folder': 'F', 'vm': {'uuid': 'vm-e', 'name': 'e'}, 'point': {'kind': 'full', 'seq': 0},
           'chain_dir': 'F/e_vm-e_20261021-190000.chain',
           'devices': [{'uuid': 'disk-e', 'type': 'VIRTIO_DISK', 'clone_uuid': 'eeee4444-0000-0000-0000-000000000000', 'size': MiB, 'map': [{'lower': 0, 'upper': 0}]}]}
    j = _wait(stack, stack.client.post('/jobs/backup', json=req, headers=H).get_json()['id'])
    assert j['state'] == 'done', j
    root = os.path.join(stack.mounter.root, P.targets.parse_uri(req['target']['uri']).key)
    assert os.path.exists(os.path.join(root, 'F', 'e_vm-e_20261021-190000.chain', 'base', 'disk-e.qcow2'))
    assert not os.path.exists(os.path.join(root, 'F', 'e_vm-e.chain'))
    j2 = _wait(stack, stack.client.post('/jobs/backup', json=dict(req, chain_dir='F/e_vm-e_20261021-190500.chain'), headers=H).get_json()['id'])
    assert j2['state'] == 'done', j2                      # a second full of the same VM: its own directory, no 'already exists'
    j3 = _wait(stack, stack.client.post('/jobs/backup', json=dict(req, chain_dir='../escape.chain'), headers=H).get_json()['id'])
    assert j3['state'] == 'failed' and 'relative' in j3['error']


# ----------------------------------------------------------------------------- SDK rules (2026-10-22 audit)
def test_hole_is_recognised_in_both_wire_forms():
    """v2 spec says the string enum; clusters up to 9.7 send the integer through a REST conversion bug."""
    assert P.copier.is_hole({'differentHow': 'TO_IS_HOLE'}) and P.copier.is_hole({'differentHow': 1}) and P.copier.is_hole({'differentHow': '1'})
    assert not P.copier.is_hole({'differentHow': 'ADDRESS_CHANGED'}) and not P.copier.is_hole({'differentHow': 0}) and not P.copier.is_hole({})
    assert [c[2] for c in P.copier._chunks([{'lower': 0, 'upper': 0, 'differentHow': 0}, {'lower': 1, 'upper': 1, 'differentHow': 1}])] == [False, True]


def test_resized_device_gets_a_full_inside_a_delta_point(stack):
    """A device flagged `full` by the service (its capacity changed) is written without a backing
    file even though the previous point has an image of it: the whole allocation map is copied and
    nothing reads through to the old, differently sized image."""
    H = {'Authorization': 'Bearer t0k'}
    disk = 'd0dd5d2c-9c7a-44ec-b6bc-d4ce06bb204d'
    src_a = stack.dev / 'virtio-aaaa1111'; _random_file(str(src_a), 4, 1)
    req = {'target': {'uri': 'smb://nas/share/hcm', 'username': 'u', 'password': 'p'}, 'folder': 'Amsterdam', 'chain_dir': 'Amsterdam/r.chain',
           'vm': {'uuid': '3cf115a9-1111-4111-8111-111111111111', 'name': 'exptest'}, 'point': {'kind': 'full', 'seq': 0}, 'plan_id': 4, 'run_id': 30,
           'devices': [{'uuid': disk, 'type': 'VIRTIO_DISK', 'clone_uuid': 'aaaa1111-0000-0000-0000-000000000000', 'size': 4 * MiB, 'map': [{'lower': 0, 'upper': 3}]}],
           'definition': {'name': 'exptest'}}
    assert _wait(stack, stack.client.post('/jobs/backup', json=req, headers=H).get_json()['id'])['state'] == 'done'
    # the disk grew to 6 MiB; the service sends the allocation map (blocks 0 and 5 hold data) with full=True
    src_b = stack.dev / 'virtio-cccc3333'; _random_file(str(src_b), 6, 2)
    with open(str(src_b), 'r+b') as fh:
        fh.seek(MiB); fh.write(bytes(4 * MiB))
    amap = [{'lower': 0, 'upper': 0}, {'lower': 5, 'upper': 5}]
    req2 = dict(req, point={'kind': 'delta', 'seq': 1, 'previous_seq': 0}, run_id=31,
                devices=[{'uuid': disk, 'type': 'VIRTIO_DISK', 'clone_uuid': 'cccc3333-0000-0000-0000-000000000000', 'size': 6 * MiB, 'map': amap, 'full_map': amap, 'full': True}])
    j = _wait(stack, stack.client.post('/jobs/backup', json=req2, headers=H).get_json()['id'])
    assert j['state'] == 'done', j
    chain = os.path.join(stack.mounter.root, P.targets.parse_uri(req['target']['uri']).key, 'hcm', 'Amsterdam', 'r.chain')
    img = open(os.path.join(chain, 'delta-0001', f'{disk}.qcow2'), 'rb').read(); b = open(str(src_b), 'rb').read()
    assert len(img) == 6 * MiB and img == b, 'the image is the resized device, not the old image with a patch on top'
    pm = json.load(open(os.path.join(chain, 'delta-0001', 'manifest.json')))
    assert pm['devices'][0]['full'] is True and j['result']['devices'][0]['full'] is True


def test_backup_runs_the_self_check_first(stack, monkeypatch):
    """A reader whose qemu cannot write a correct delta fails before any device is read."""
    H = {'Authorization': 'Bearer t0k'}
    monkeypatch.setattr(stack.engine.backend, 'selfcheck', lambda workdir: (_ for _ in ()).throw(RuntimeError('proxy self-check failed, this host cannot write a correct delta')))
    req = {'target': {'uri': 'smb://nas/share/hcm', 'username': 'u', 'password': 'p'}, 'folder': 'Amsterdam', 'vm': {'uuid': 'x', 'name': 'exptest'},
           'point': {'kind': 'full', 'seq': 0}, 'devices': [{'uuid': 'd', 'type': 'VIRTIO_DISK', 'clone_uuid': 'zzzz9999-0000-0000-0000-000000000000', 'size': MiB, 'map': [{'lower': 0, 'upper': 0}]}],
           'definition': {}}
    j = _wait(stack, stack.client.post('/jobs/backup', json=req, headers=H).get_json()['id'])
    assert j['state'] == 'failed' and 'self-check failed' in j['error']
    assert P.identity.identity(stack.cfg, 'file', None)['capabilities']['full_flag'] is True


def _hand_made_chain(stack, target_uri, rel_chain, point_files):
    """chain.json + one point dir per entry of point_files ({dir: {file: bytes}}) with manifests."""
    root = stack.mounter.acquire(target_uri, None, None)
    chain_dir = os.path.join(root, rel_chain)
    points = []
    for seq, (d, files) in enumerate(point_files.items()):
        pd = os.path.join(chain_dir, d); os.makedirs(pd, exist_ok=True)
        devs = []
        for name, data in files.items():
            with open(os.path.join(pd, name), 'wb') as fh:
                fh.write(data)
            # size is the device's virtual size in a real manifest, not the file's: 100 MiB here
            devs.append({'uuid': name.split('.')[0], 'type': 'VIRTIO_DISK', 'file': name, 'size': 100 * MiB, 'sha256': P.manifest.sha256_file(os.path.join(pd, name))})
        P.manifest.write_json(os.path.join(pd, 'manifest.json'), {'kind': 'full' if seq == 0 else 'delta', 'seq': seq, 'dir': d, 'devices': devs})
        points.append({'kind': 'full' if seq == 0 else 'delta', 'seq': seq, 'dir': d, 'previous_seq': seq - 1 if seq else None})
    P.manifest.save_chain(chain_dir, {'vm': {'uuid': 'v', 'name': 'vm'}, 'points': points})
    return chain_dir


def test_verify_upto_seq_reports_progress_and_names_the_corrupt_file(stack):
    H = {'Authorization': 'Bearer t0k'}
    target = {'uri': 'smb://nas/share/hcm'}
    chain_dir = _hand_made_chain(stack, target['uri'], 'Amsterdam/vm_v.chain',
                                 {'base': {'disk.qcow2': os.urandom(2 * MiB), 'nvram-n.qcow2': b'N' * 4096},
                                  'delta-0001': {'disk.qcow2': os.urandom(MiB)},
                                  'delta-0002': {'disk.qcow2': os.urandom(MiB)}})
    # damage the newest delta after the fact
    with open(os.path.join(chain_dir, 'delta-0002', 'disk.qcow2'), 'r+b') as fh:
        fh.seek(1000); fh.write(b'XXXX')
    # a restore of seq 1 checks base + delta-0001 only and passes
    v = _wait(stack, stack.client.post('/jobs/verify', json={'target': target, 'chain_dir': 'Amsterdam/vm_v.chain', 'upto_seq': 1}, headers=H).get_json()['id'])
    assert v['state'] == 'done' and v['result']['ok'] and set(v['result']['points']) == {'base', 'delta-0001'}
    assert v['result']['bytes'] == 3 * MiB + 4096 and v['progress'] == {'points_total': 2, 'points_done': 2, 'bytes_total': 3 * MiB + 4096, 'bytes_done': 3 * MiB + 4096}
    assert v['result']['points']['base']['bytes'] == 2 * MiB + 4096
    # the whole chain fails on the damaged file, which is named
    v = _wait(stack, stack.client.post('/jobs/verify', json={'target': target, 'chain_dir': 'Amsterdam/vm_v.chain'}, headers=H).get_json()['id'])
    assert v['state'] == 'done' and not v['result']['ok']
    assert v['result']['points']['delta-0002'] == {'ok': False, 'checked': 1, 'mismatches': ['disk.qcow2: checksum differs'], 'bytes': MiB}
    assert v['result']['points']['delta-0001']['ok'] and v['progress']['points_done'] == 3
    # a single point by seq still works; a missing file is reported as such
    os.unlink(os.path.join(chain_dir, 'base', 'nvram-n.qcow2'))
    v = _wait(stack, stack.client.post('/jobs/verify', json={'target': target, 'chain_dir': 'Amsterdam/vm_v.chain', 'seq': 0}, headers=H).get_json()['id'])
    assert list(v['result']['points']) == ['base'] and v['result']['points']['base']['mismatches'] == ['nvram-n.qcow2: missing']
    # an empty selection is not "ok"
    v = _wait(stack, stack.client.post('/jobs/verify', json={'target': target, 'chain_dir': 'Amsterdam/vm_v.chain', 'seq': 7}, headers=H).get_json()['id'])
    assert v['state'] == 'done' and v['result'] == {'ok': False, 'points': {}, 'bytes': 0, 'duration_s': v['result']['duration_s']}


def test_sha256_file_progress_and_cancel(tmp_path):
    f = tmp_path / 'f'; f.write_bytes(b'a' * 5000)
    seen = []
    assert P.manifest.sha256_file(str(f), chunk=2048, progress=seen.append) == __import__('hashlib').sha256(b'a' * 5000).hexdigest()
    assert seen == [2048, 4096, 5000]
    with pytest.raises(RuntimeError):
        P.manifest.sha256_file(str(f), chunk=2048, cancelled=lambda: True)


def test_nbd_backend_options_reach_qemu(tmp_path, monkeypatch):
    """lazy refcounts go into qemu-img create, the cache mode into qemu-nbd -c; per-call overrides
    (the bench job) win over the backend's settings; read-only attaches stay writeback."""
    calls = []
    monkeypatch.setattr(P.images, '_run', lambda cmd, timeout=3600: calls.append(cmd) or '')
    be = P.images.NbdBackend(P.images.NbdPool(1, str(tmp_path)), lazy_refcounts=True, cache='unsafe')
    assert be.options == {'lazy_refcounts': True, 'cache': 'unsafe'}
    be.create(str(tmp_path / 'a.qcow2'), 4 * MiB)
    assert calls[-1][:7] == ['qemu-img', 'create', '-q', '-f', 'qcow2', '-o', 'cluster_size=1M,lazy_refcounts=on']
    be.create(str(tmp_path / 'b.qcow2'), 4 * MiB, lazy_refcounts=False)
    assert calls[-1][6] == 'cluster_size=1M'
    monkeypatch.setattr(be, '_probe', lambda slot: None)
    monkeypatch.setattr(be, 'sys_block', str(tmp_path / 'sys'))
    (tmp_path / 'sys' / 'nbd0').mkdir(parents=True); (tmp_path / 'sys' / 'nbd0' / 'size').write_text('8192')
    be._connect(f'{tmp_path}/nbd0', 'x.qcow2', readonly=False)
    assert '--cache=unsafe' in calls[-1]
    be._connect(f'{tmp_path}/nbd0', 'x.qcow2', readonly=False, cache='none')
    assert '--cache=none' in calls[-1]
    be._connect(f'{tmp_path}/nbd0', 'x.qcow2', readonly=True)
    assert '--cache=writeback' in calls[-1] and '-r' in calls[-1]
    assert P.images.NbdBackend(P.images.NbdPool(1, str(tmp_path)), cache='bogus').cache == 'writeback'
    assert P.images.make_backend('nbd', 2, str(tmp_path), lazy_refcounts=True, cache='none').options == {'lazy_refcounts': True, 'cache': 'none'}
    cfg = P.config.Config(api_token='', data_dir=str(tmp_path), host_net=str(tmp_path))
    assert cfg.qcow2_lazy_refcounts is True and cfg.nbd_cache == 'unsafe', 'measured defaults (Lab 241 → CIFS: 75 vs 38 MiB/s)'


def test_bench_job_measures_plain_on_the_file_backend_and_cleans_up(stack):
    H = {'Authorization': 'Bearer t0k'}
    target = {'uri': 'smb://nas/share/hcm'}
    assert stack.client.post('/jobs/bench', json={}, headers=H).status_code == 400
    j = _wait(stack, stack.client.post('/jobs/bench', json={'target': target, 'size_mib': 4, 'modes': ['plain', 'nbd', 'nbd_read', 'bogus']}, headers=H).get_json()['id'], timeout=60)
    assert j['state'] == 'done', j
    r = j['result']
    assert r['size_mib'] == 16 and r['writers'] == 2 and r['backend'] == 'file', 'size is clamped to 16 MiB at least'
    assert r['modes']['plain']['bytes'] == 16 * MiB and r['modes']['plain']['mib_s'] > 0 and r['modes']['plain']['seconds'] > 0
    assert r['modes']['nbd'] == {'skipped': 'needs the nbd backend'} and r['modes']['nbd_read'] == {'skipped': 'needs the nbd backend'}
    assert 'bogus' not in r['modes']
    assert j['progress']['modes_done'] == 3 and j['progress']['bytes_done'] == j['progress']['bytes_total'] == 3 * 16 * MiB
    root = stack.mounter.acquire(target['uri'], None, None)
    assert not os.path.exists(os.path.join(root, '_bench')), 'the bench directory is removed from the target'
    assert not [f for f in os.listdir(stack.cfg.data_dir) if f.startswith('bench-')], 'source and scratch files removed'
    j = _wait(stack, stack.client.post('/jobs/bench', json={'target': target, 'modes': ['bogus']}, headers=H).get_json()['id'])
    assert j['state'] == 'failed' and 'no known modes' in j['error']
    ident = stack.client.get('/identity', headers=H).get_json()
    assert 'bench' in ident['capabilities']['jobs'] and ident['options'] == {'lazy_refcounts': True, 'nbd_cache': 'unsafe', 'writers': 2}


def test_bench_restore_profile_is_selected_and_skips_without_nbd(stack):
    H = {'Authorization': 'Bearer t0k'}
    j = _wait(stack, stack.client.post('/jobs/bench', json={'target': {'uri': 'smb://nas/share/hcm'}, 'size_mib': 16, 'profile': 'restore'}, headers=H).get_json()['id'], timeout=60)
    assert j['state'] == 'done', j
    assert set(j['result']['modes']) == set(P.bench.RESTORE_MODES) == set(P.bench.READ_MODES)   # json sorts keys
    assert all(v == {'skipped': 'needs the nbd backend'} for v in j['result']['modes'].values())


def test_nbd_stage_into_runs_one_convert_with_direct_io_and_reports_progress(tmp_path, monkeypatch):
    import subprocess as sp
    cmds = []

    class _Proc:
        def __init__(self, cmd, **kw):
            cmds.append(cmd); self.returncode = 0
            import io
            self.stdout = io.StringIO('    (0.00/100%)\r    (50.00/100%)\r    (100.00/100%)\r')
            self.stderr = io.StringIO('')
        def wait(self, timeout=None): return 0
        def terminate(self): pass
    monkeypatch.setattr(P.images.subprocess, 'Popen', _Proc)
    be = P.images.NbdBackend(P.images.NbdPool(1, str(tmp_path)))
    monkeypatch.setattr(be, 'virtual_size', lambda p: 4 * MiB)
    img = tmp_path / 'p.qcow2'; img.write_bytes(bytes(4 * MiB)); dev = tmp_path / 'dev'; dev.write_bytes(bytes(4 * MiB))
    seen = []
    n = be.stage_into(str(img), str(dev), [{'lower': 0, 'upper': 2}], 4, progress=lambda d, t: seen.append((d, t)))
    assert n == 3 * MiB and seen[-1] == (3 * MiB, 3 * MiB) and (3 * MiB // 2, 3 * MiB) in seen
    c = cmds[0]
    assert c[:2] == ['qemu-img', 'convert'] and '-n' in c and '--target-is-zero' in c and ['-t', 'none'] == c[c.index('-t'):c.index('-t') + 2]
    assert c[c.index('-m') + 1] == '16' and '-W' in c and c[-2:] == [str(img), str(dev)]
    # a device smaller than the image falls back to the copier through nbd (older chains)
    small = tmp_path / 'small'; small.write_bytes(bytes(2 * MiB))
    called = []
    import contextlib
    @contextlib.contextmanager
    def ro(path, cache=None):
        called.append(path); yield str(img)
    monkeypatch.setattr(be, 'open_readonly', ro)
    be.stage_into(str(img), str(small), [{'lower': 0, 'upper': 0}], 4)
    assert called == [str(img)] and len(cmds) == 1
    # a failing convert is an error with qemu's message
    class _Bad(_Proc):
        def __init__(self, cmd, **kw):
            super().__init__(cmd); self.returncode = 1
            import io; self.stderr = io.StringIO('qemu-img: Could not open')
    monkeypatch.setattr(P.images.subprocess, 'Popen', _Bad)
    with pytest.raises(RuntimeError, match='Could not open'):
        be.stage_into(str(img), str(dev), [{'lower': 0, 'upper': 0}], 4)


# ----------------------------------------------------------------------------- image update hook
def test_update_hook_takes_a_pushed_image(tmp_path):
    drop = tmp_path / 'update'; drop.mkdir()
    cfg = P.config.Config(api_token='t0k', data_dir=str(tmp_path / 'data'), dev_dir=str(tmp_path / 'dev'), backend='file', update_dir=str(drop))
    engine = P.jobs.Engine(cfg, P.images.FileBackend(), _FakeMounter(str(tmp_path / 'targets')))
    client = P.api.create_app(cfg, engine).test_client()
    H = {'Authorization': 'Bearer t0k'}
    assert client.get('/identity', headers=H).get_json()['capabilities']['update'] is True
    assert client.post('/update', data=b'x', headers=H).status_code == 400, 'the tag is required'
    assert client.post('/update', data=b'', headers={**H, 'X-Image-Tag': 't'}).status_code == 400, 'a body is required'
    r = client.post('/update', data=b'tarball bytes', headers={**H, 'X-Image-Tag': 'hcm-proxy:0.5.0'})
    assert r.status_code == 202 and r.get_json() == {'accepted': True, 'tag': 'hcm-proxy:0.5.0', 'bytes': 13}
    assert (drop / 'image.tar').read_bytes() == b'tarball bytes' and (drop / 'image.tag').read_text() == 'hcm-proxy:0.5.0\n'
    import time as _t
    for _ in range(60):
        if (drop / 'ready').exists(): break
        _t.sleep(0.1)
    assert (drop / 'ready').exists(), 'the marker is armed after the answer, so the host restart cannot cut the answer off'
    assert not (drop / 'image.tar.part').exists()


def test_update_hook_absent_without_a_directory(stack):
    H = {'Authorization': 'Bearer t0k'}
    assert stack.client.get('/identity', headers=H).get_json()['capabilities']['update'] is False
    r = stack.client.post('/update', data=b'x', headers={**H, 'X-Image-Tag': 't'})
    assert r.status_code == 501 and 'no update hook' in r.get_json()['error']


# ----------------------------------------------------------------------------- support bundle
def test_support_bundle_has_logs_jobs_mounts_and_system_state_without_secrets(tmp_path):
    import zipfile, io
    logs = tmp_path / 'data' / 'logs'; logs.mkdir(parents=True)
    (logs / 'proxy.log').write_text('{"message": "disk1-p1 not mounted: ext4 mount failed"}\n')
    (logs / 'proxy.log.1').write_text('older\n')
    cfg = P.config.Config(api_token='s3cret-token', data_dir=str(tmp_path / 'data'), backend='file')
    jobs = [{'id': 'j1', 'kind': 'backup', 'state': 'error', 'error': 'boom',
             'params': {'target': {'uri': 'smb://nas/share', 'username': 'bk', 'password': 'hunter2'}}}]
    seen = []
    def run(cmd, **kw):
        seen.append(cmd[0])
        if cmd[0] == 'dmesg':
            return types.SimpleNamespace(returncode=0, stdout='[Sat Oct 31] EXT4-fs (dm-3): unsupported feature\n', stderr='')
        if cmd[0] == 'ps':
            raise FileNotFoundError('ps')
        return types.SimpleNamespace(returncode=0, stdout=f'{cmd[0]} output\n', stderr='')
    data = P.support.bundle(cfg, {'version': '0.5.1', 'hostname': 'px'}, jobs, [{'id': 'm1', 'skipped': [{'name': 'disk1-p1', 'reason': 'x'}]}], run=run)
    z = zipfile.ZipFile(io.BytesIO(data))
    names = set(z.namelist())
    assert {'proxy.json', 'jobs.json', 'mounts.json', 'logs/proxy.log', 'logs/proxy.log.1', 'system/dmesg.txt', 'system/lsblk.txt'} <= names
    assert 'EXT4-fs (dm-3)' in z.read('system/dmesg.txt').decode()
    assert 'not run' in z.read('system/ps.txt').decode(), 'a missing tool is noted, not fatal'
    everything = b''.join(z.read(n) for n in names).decode()
    assert 's3cret-token' not in everything and 'hunter2' not in everything, 'no secrets in the bundle'
    assert json.loads(z.read('jobs.json'))[0]['params']['target'] == {'uri': 'smb://nas/share', 'username': 'bk', 'password': '***'}
    assert json.loads(z.read('mounts.json'))[0]['skipped'][0]['name'] == 'disk1-p1'


def _fake_proc(root, procs):
    """procs: (pid, comm, state, ppid, threads, cmdline)"""
    for pid, comm, state, ppid, threads, cmd in procs:
        d = root / str(pid); d.mkdir(parents=True)
        rest = [state, str(ppid)] + ['0'] * 15 + [str(threads), '0', '0', '0', '2560']   # f[17] threads, f[21] rss pages
        (d / 'stat').write_text(f'{pid} ({comm}) ' + ' '.join(rest) + '\n')
        (d / 'cmdline').write_text('\0'.join(cmd.split()) if cmd else '')
    (root / 'sys' / 'kernel').mkdir(parents=True)
    (root / 'sys' / 'kernel' / 'threads-max').write_text('31318\n')
    (root / 'sys' / 'kernel' / 'pid_max').write_text('4194304\n')
    (root / 'loadavg').write_text('0.10 0.20 0.30 2/4911 812\n')
    (root / 'self').mkdir()
    (root / 'self' / 'limits').write_text('Limit  Soft  Hard  Units\nMax processes  15659  15659  processes\nMax open files 1024 4096 files\n')


def test_support_bundle_counts_zombies_and_reads_every_task_limit(tmp_path):
    """0.5.4 ran out of threads after ~1000 jobs and the bundle could not say why: no procps, no cgroup
    numbers. processes.txt and limits.txt read /proc and the cgroup files directly."""
    import zipfile, io
    proc = tmp_path / 'proc'
    _fake_proc(proc, [(1, 'tini', 'S', 0, 1, '/usr/bin/tini -- python proxy.py'),
                      (7, 'python', 'S', 1, 9, 'python proxy.py'),
                      (40, 'qemu-nbd', 'Z', 1, 1, ''), (41, 'qemu-nbd', 'Z', 1, 1, ''),
                      (42, 'ntfs-3g', 'Z', 1, 1, ''), (50, 'qemu-nbd', 'S', 1, 3, 'qemu-nbd -c /dev/nbd3 -r x.qcow2')])
    table = P.support.process_table(str(proc))
    assert 'processes: 6   threads: 16' in table and 'states: S=3, Z=3' in table
    assert 'zombies: qemu-nbd (parent 1) x2, ntfs-3g (parent 1) x1' in table
    assert 'qemu-nbd -c /dev/nbd3 -r x.qcow2' in table and '[qemu-nbd]' in table, 'a zombie has no cmdline: its name stands in'
    own = tmp_path / 'cg'; own.mkdir()
    (own / 'pids.current').write_text('4900\n'); (own / 'pids.max').write_text('max\n'); (own / 'memory.max').write_text('1073741824\n')
    host = tmp_path / 'host'
    scope = host / 'system.slice' / 'docker-9a59ab723977ffff.scope'; scope.mkdir(parents=True)
    (scope / 'pids.current').write_text('4900\n'); (scope / 'pids.max').write_text('4915\n')
    (host / 'system.slice' / 'pids.current').write_text('5200\n')
    limits = P.support.task_limits(str(proc), str(own), str(host), container_id='9a59ab723977')
    assert 'threads-max = 31318' in limits and 'pid_max = 4194304' in limits and '2/4911' in limits
    assert 'Max processes  15659' in limits and 'Max open files' not in limits
    assert 'pids.current = 4900' in limits and 'memory.max = 1073741824' in limits
    i = limits.index('host cgroup /system.slice/docker-9a59ab723977ffff.scope:')
    assert 'pids.max = 4915' in limits[i:] and 'host cgroup /system.slice:' in limits[i:] and 'host cgroup /:' in limits[i:]
    assert 'no directory naming' in P.support.task_limits(str(proc), str(own), str(host), container_id='nothere')
    cfg = P.config.Config(api_token='t', data_dir=str(tmp_path / 'data'), backend='file')
    run = lambda cmd, **kw: types.SimpleNamespace(returncode=0, stdout='', stderr='')
    z = zipfile.ZipFile(io.BytesIO(P.support.bundle(cfg, {}, [], [], run=run, proc=str(proc), cgroup=str(own),
                                                    host_cgroup=str(host), container_id='9a59ab723977')))
    assert 'Z=3' in z.read('system/processes.txt').decode() and 'pids.max = 4915' in z.read('system/limits.txt').decode()
    z = zipfile.ZipFile(io.BytesIO(P.support.bundle(cfg, {}, [], [], run=run, proc=str(tmp_path / 'none'))))
    assert z.read('system/processes.txt').startswith(b'(not collected'), 'a diagnostic never breaks the bundle'


def test_proxy_image_runs_under_an_init_that_reaps_orphans():
    import pathlib
    df = (pathlib.Path(__file__).resolve().parents[1] / 'proxy' / 'Dockerfile').read_text()
    assert 'ENTRYPOINT ["/usr/bin/tini", "--"]' in df and ' tini ' in df and ' procps ' in df


def test_support_bundle_route_needs_the_token(tmp_path):
    cfg = P.config.Config(api_token='t0k', data_dir=str(tmp_path / 'data'), backend='file')
    app = P.api.create_app(cfg, types.SimpleNamespace(backend=P.images.FileBackend(), list=lambda: [], get=lambda i: None, jobs={}))
    c = app.test_client()
    assert c.get('/support/bundle').status_code == 401
    r = c.get('/support/bundle', headers={'Authorization': 'Bearer t0k'})
    assert r.status_code == 200 and r.mimetype == 'application/zip' and 'attachment; filename="hcm-proxy-' in r.headers['Content-Disposition']
    assert r.data[:2] == b'PK'
    assert P.identity.VERSION == '0.5.5'


def test_the_proxy_keeps_a_rotating_log_file(tmp_path):
    import importlib.util, logging
    try:
        import pythonjsonlogger  # noqa: F401
    except ImportError:          # not in the test requirements: a plain formatter stands in for the JSON one
        jl = types.ModuleType('pythonjsonlogger.jsonlogger')
        class JsonFormatter(logging.Formatter):
            def format(self, record):
                d = {'message': record.getMessage()}; self.add_fields(d, record, {}); return json.dumps(d)
            def add_fields(self, log_record, record, message_dict): pass
        jl.JsonFormatter = JsonFormatter
        pkg = types.ModuleType('pythonjsonlogger'); pkg.jsonlogger = jl
        sys.modules.setdefault('pythonjsonlogger', pkg); sys.modules.setdefault('pythonjsonlogger.jsonlogger', jl)
    spec = importlib.util.spec_from_file_location('proxy_log_under_test', os.path.join(_PROXY, 'log.py'))
    plog = importlib.util.module_from_spec(spec); spec.loader.exec_module(plog)
    root = logging.getLogger(); before = list(root.handlers), root.level
    try:
        plog.configure('INFO', str(tmp_path / 'logs'))
        logging.getLogger('proxy.mounts').warning('disk1-p1 not mounted: test')
        for h in root.handlers: h.flush()
        text = (tmp_path / 'logs' / 'proxy.log').read_text()
        assert 'disk1-p1 not mounted: test' in text and '"level": "WARNING"' in text
    finally:
        for h in root.handlers:
            if h not in before[0]: h.close()
        root.handlers[:] = before[0]; root.setLevel(before[1])
