"""The qcow2 delta semantics on real qemu (qemu-img + qemu-io, no root, no nbd).

The proxy's correctness argument rests on two properties of the qemu that is installed where the
proxy runs: a cluster written with zeros masks the backing file, and a write that covers part of a
cluster is completed from the backing file (copy-on-write) rather than zero-filled. These tests
prove both on NbdBackend.create()'s exact image layout (1 MiB clusters, backing file set at create
time), writing through qemu-io, which uses the same qcow2 driver as qemu-nbd. They are skipped
where qemu is not installed (the Mac) and run inside the proxy container in CI.
"""
import importlib
import json
import os
import shutil
import subprocess
import sys

import pytest

MiB = 1 << 20
pytestmark = pytest.mark.skipif(not (shutil.which('qemu-img') and shutil.which('qemu-io')), reason='qemu-img / qemu-io not installed')


def _images():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'proxy'))
    try:
        saved = sys.modules.pop('images', None)
        return importlib.import_module('images')
    finally:
        sys.path.pop(0)
        sys.modules.pop('images', None)
        if saved:
            sys.modules['images'] = saved


def _qemu_io(path, *cmds, readonly=False):
    args = ['qemu-io'] + (['-r'] if readonly else []) + ['-f', 'qcow2']
    for c in cmds:
        args += ['-c', c]
    r = subprocess.run(args + [path], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout


def _map(path):
    return json.loads(subprocess.run(['qemu-img', 'map', '--output=json', '-f', 'qcow2', path], capture_output=True, text=True, check=True).stdout)


def test_delta_masks_zeros_and_cows_partial_clusters(tmp_path):
    images = _images()
    be = images.NbdBackend(images.NbdPool(0, str(tmp_path)))
    parent, delta = str(tmp_path / 'parent.qcow2'), str(tmp_path / 'delta.qcow2')
    size = 4 * MiB + 512 * 1024                                         # not a whole number of MiB: a short tail block
    be.create(parent, size)
    _qemu_io(parent, f'write -P 0xaa 0 {size}')
    be.create(delta, size, backing=parent)
    info = json.loads(subprocess.run(['qemu-img', 'info', '--output=json', delta], capture_output=True, text=True, check=True).stdout)
    assert info['cluster-size'] == MiB and info['backing-filename'] == 'parent.qcow2', 'one snapDiff block per cluster; relative backing name'
    # block 1: data; block 2: guest zeroed it (must mask the parent); block 3: only the first half written
    # (the rest must come from the parent, not read as zeros); block 4: the short tail
    _qemu_io(delta, 'write -P 0x5a 1M 1M', 'write -P 0 2M 1M', 'write -P 0x5a 3M 512k', f'write -P 0x11 4M {size - 4 * MiB}')
    _qemu_io(delta, 'read -P 0xaa 0 1M', 'read -P 0x5a 1M 1M', 'read -P 0 2M 1M', 'read -P 0x5a 3M 512k', 'read -P 0xaa 3584k 512k',
             f'read -P 0x11 4M {size - 4 * MiB}', readonly=True)
    assert be.check(delta)
    # exactly the written clusters are allocated in the delta; block 0 reads through to the parent
    own = sorted({b for e in _map(delta) if e.get('depth') == 0 for b in range(e['start'] // MiB, (e['start'] + e['length'] - 1) // MiB + 1)})
    assert own == [1, 2, 3, 4], own
    # the zeroed block is a cluster of the delta itself (a plain write of zeros, as the kernel nbd client
    # sends them, allocates a data cluster), so it masks the parent instead of reading through to 0xAA,
    # which is what a `qemu-img convert` of the same bytes would have done
    at_2m = [e for e in _map(delta) if e['start'] <= 2 * MiB < e['start'] + e['length']][0]
    assert at_2m['depth'] == 0, at_2m
    assert be.allocation_map(delta) == [{'lower': 0, 'upper': 4}], 'through the chain every block has content'


def test_flatten_and_commit_keep_the_content(tmp_path):
    images = _images()
    be = images.NbdBackend(images.NbdPool(0, str(tmp_path)))
    base, d1, d2, flat = (str(tmp_path / n) for n in ('base.qcow2', 'd1.qcow2', 'd2.qcow2', 'flat.qcow2'))
    be.create(base, 3 * MiB); _qemu_io(base, 'write -P 0xaa 0 3M')
    be.create(d1, 3 * MiB, backing=base); _qemu_io(d1, 'write -P 0xbb 1M 1M')
    be.create(d2, 3 * MiB, backing=d1); _qemu_io(d2, 'write -P 0 1M 1M', 'write -P 0xcc 2M 1M')
    be.flatten(d2, flat)
    _qemu_io(flat, 'read -P 0xaa 0 1M', 'read -P 0 1M 1M', 'read -P 0xcc 2M 1M', readonly=True)
    # retention: commit d1 into base, rebase d2 onto base (what run_prune does), content unchanged
    be.commit(d1); be.rebase(d2, base)
    _qemu_io(d2, 'read -P 0xaa 0 1M', 'read -P 0 1M 1M', 'read -P 0xcc 2M 1M', readonly=True)
    _qemu_io(base, 'read -P 0xbb 1M 1M', readonly=True)
