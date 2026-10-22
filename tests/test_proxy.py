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
_CLASH = ('config', 'devices', 'copier', 'images', 'targets', 'manifest', 'identity', 'jobs', 'api', 'mounts', 'bench', 'log')


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
    assert P.manifest.chain_dirname('My VM (prod)', '3cf115a9-8208-41b2-a67d-0ed5421441c2') == 'My_VM__prod__3cf115a9.chain'
    pd = tmp_path / 'base'; pd.mkdir()
    (pd / 'x.qcow2').write_bytes(b'abc')
    P.manifest.write_json(str(pd / 'manifest.json'), {'devices': [{'file': 'x.qcow2', 'sha256': P.manifest.sha256_file(str(pd / 'x.qcow2'))}]})
    assert P.manifest.verify_point(str(pd)) == {'ok': True, 'checked': 1, 'mismatches': [], 'bytes': 3}
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
           'vm': {'uuid': '3cf115a9-8208-41b2-a67d-0ed5421441c2', 'name': 'exptest'}, 'point': {'kind': 'full', 'seq': 0}, 'plan_id': 4, 'run_id': 30,
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
