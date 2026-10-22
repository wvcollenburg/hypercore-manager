"""
manifest.py — integrity records for backup chains.

Every point directory carries manifest.json (files with sha256 and size, device map, serials,
the VM definition the service supplied) and the chain root carries chain.json (ordered points).
"""
from __future__ import annotations

import hashlib
import json
import os
import time

CHAIN_FILE = 'chain.json'
POINT_FILE = 'manifest.json'


def sha256_file(path: str, chunk: int = 16 << 20, progress=None, cancelled=None) -> str:
    """progress(bytes_read_so_far) after every chunk; cancelled() polled between chunks."""
    h = hashlib.sha256()
    done = 0
    with open(path, 'rb') as fh:
        while True:
            if cancelled and cancelled():
                raise RuntimeError('cancelled')
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
            done += len(b)
            if progress:
                progress(done)
    return h.hexdigest()


def write_json(path: str, data: dict) -> None:
    tmp = path + '.tmp'
    with open(tmp, 'w') as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def read_json(path: str) -> dict | None:
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def point_dirname(kind: str, seq: int) -> str:
    return 'base' if kind == 'full' and seq == 0 else f'{ "delta" if kind == "delta" else "full"}-{seq:04d}'


def chain_dirname(vm_name: str, vm_uuid: str, started: str | None = None) -> str:
    """Directory of one chain. `started` (YYYYMMDD-HHMMSS) makes it unique per chain: every full
    starts a new chain of the same VM, and the old one keeps its directory until retention
    removes it."""
    safe = ''.join(c if c.isalnum() or c in '._-' else '_' for c in vm_name)[:64] or 'vm'
    return f'{safe}_{vm_uuid[:8]}{"_" + started if started else ""}.chain'


def load_chain(chain_dir: str) -> dict:
    return read_json(os.path.join(chain_dir, CHAIN_FILE)) or {'points': []}


def save_chain(chain_dir: str, chain: dict) -> None:
    chain['updated_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    write_json(os.path.join(chain_dir, CHAIN_FILE), chain)


def point_bytes(point_dir: str) -> int:
    """Bytes a verify of this point will read: the files' sizes on disk. (The manifest's `size`
    is the device's virtual size, which a qcow2 delta is far below.)"""
    m = read_json(os.path.join(point_dir, POINT_FILE)) or {}
    total = 0
    for d in m.get('devices', []):
        try:
            total += os.path.getsize(os.path.join(point_dir, d['file']))
        except OSError:
            pass
    return total


def verify_point(point_dir: str, progress=None, cancelled=None) -> dict:
    """Re-hash every file named in the point manifest. Returns {ok, checked, mismatches, bytes}.
    progress(bytes_read_in_this_point) is called as the files are read."""
    m = read_json(os.path.join(point_dir, POINT_FILE))
    if not m:
        return {'ok': False, 'checked': 0, 'mismatches': ['manifest.json missing or unreadable'], 'bytes': 0}
    bad = []
    done = 0
    for d in m.get('devices', []):
        p = os.path.join(point_dir, d['file'])
        if not os.path.exists(p):
            bad.append(f'{d["file"]}: missing')
            continue
        base = done
        digest = sha256_file(p, progress=(lambda n, _b=base: progress(_b + n)) if progress else None, cancelled=cancelled)
        done += os.path.getsize(p)
        if digest != d['sha256']:
            bad.append(f'{d["file"]}: checksum differs')
    return {'ok': not bad, 'checked': len(m.get('devices', [])), 'mismatches': bad, 'bytes': done}
