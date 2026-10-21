"""
mounts.py — mount a restore point read-only for file-level recovery.

For every disk image of a point:
  nbd (read-only)  ->  partitions from sfdisk  ->  read-only loop device per partition
  ->  device-mapper snapshot with a sparse copy-on-write file (so journal replay, NTFS logs
      and LVM metadata changes never reach the backup)  ->  mount read-only
LVM physical volumes are imported under a temporary volume-group name (vgimportclone on the
COW device), so a guest whose root VG has the same name as the proxy's own does not collide.

Everything that touches the system goes through `run` (injectable), so the planning and the
browse/zip logic are unit-tested without root; the real thing is exercised on the lab proxy.
"""
from __future__ import annotations

import io
import json
import logging
import os
import shutil
import stat
import subprocess
import threading
import time
import uuid as uuidlib
import zipfile
from contextlib import ExitStack

import manifest

log = logging.getLogger('proxy.mounts')
SECTOR = 512
_FS_OPTS = {
    'ext4': ('ext4', 'ro,noload'), 'ext3': ('ext4', 'ro,noload'), 'ext2': ('ext2', 'ro'),
    'xfs': ('xfs', 'ro,norecovery,nouuid'), 'btrfs': ('btrfs', 'ro'), 'vfat': ('vfat', 'ro'),
    'ntfs': ('ntfs3', 'ro'), 'exfat': ('exfat', 'ro'),
}


def _run(cmd, check=True, timeout=600, run=subprocess.run):
    r = run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and getattr(r, 'returncode', 0):
        raise RuntimeError(f'{" ".join(cmd)} failed ({r.returncode}): {(r.stderr or r.stdout or "").strip()[:300]}')
    return (r.stdout or '').strip()


def partitions_from_sfdisk(sfdisk_json: str, device: str, device_size: int) -> list[dict]:
    """[{name, start_bytes, size_bytes}] from `sfdisk --json`; a disk without a table is one
    whole-device 'partition'."""
    try:
        parts = json.loads(sfdisk_json).get('partitiontable', {}).get('partitions', [])
    except (ValueError, AttributeError):
        parts = []
    out = []
    for i, p in enumerate(parts, 1):
        size = int(p.get('size', 0)) * SECTOR
        if size <= 0:
            continue
        out.append({'name': f'p{i}', 'start_bytes': int(p.get('start', 0)) * SECTOR, 'size_bytes': size, 'type': p.get('type')})
    if not out:
        out.append({'name': 'disk', 'start_bytes': 0, 'size_bytes': device_size, 'type': None})
    return out


def parse_blkid(text: str) -> dict:
    """`blkid -o export` -> {TYPE, LABEL, UUID, ...}"""
    out = {}
    for line in text.splitlines():
        if '=' in line:
            k, _, v = line.partition('=')
            out[k.strip()] = v.strip()
    return out


class Mount:
    def __init__(self, mid: str, root: str):
        self.id = mid
        self.root = root                     # directory under which filesystems are mounted
        self.filesystems: list[dict] = []    # {name, fstype, mountpoint, label, size_bytes}
        self.cleanup: list[list[str]] = []   # commands to run in reverse on unmount
        self.stack = ExitStack()             # keeps nbd attachments alive
        self.created = time.time()
        self.last_used = time.time()
        self.lock = threading.Lock()

    def touch(self):
        self.last_used = time.time()

    def public(self) -> dict:
        return {'id': self.id, 'filesystems': [{k: v for k, v in f.items() if k != 'mountpoint'} for f in self.filesystems],
                'created': self.created, 'last_used': self.last_used}


class MountManager:
    def __init__(self, cfg, backend, mounter, run=subprocess.run, idle_seconds: int = 1800):
        self.cfg, self.backend, self.mounter, self.run = cfg, backend, mounter, run
        self.idle_seconds = idle_seconds
        self.mounts: dict[str, Mount] = {}
        self._lock = threading.Lock()
        self.base = os.path.join(cfg.data_dir, 'flr')
        os.makedirs(self.base, exist_ok=True)

    # ------------------------------------------------------------------ mount
    def mount(self, target: dict, chain_dir: str, seq: int) -> Mount:
        root = self.mounter.acquire(target['uri'], target.get('username'), target.get('password'))
        mid = uuidlib.uuid4().hex[:12]
        m = Mount(mid, os.path.join(self.base, mid))
        os.makedirs(m.root, exist_ok=True)
        m.cleanup.append(['__release_target__', target['uri']])
        try:
            cdir = os.path.join(root, chain_dir)
            chain = manifest.load_chain(cdir)
            point = next((p for p in chain['points'] if p['seq'] == int(seq)), None)
            if not point:
                raise RuntimeError(f'point {seq} not found')
            pm = manifest.read_json(os.path.join(cdir, point['dir'], manifest.POINT_FILE)) or {}
            disks = [d for d in pm.get('devices', []) if d.get('type', 'VIRTIO_DISK') not in ('NVRAM', 'VTPM')]
            if not disks:
                raise RuntimeError('the point has no disk images')
            for di, d in enumerate(disks, 1):
                image = os.path.join(cdir, point['dir'], d['file'])
                dev = m.stack.enter_context(self.backend.open_readonly(image))
                self._attach_disk(m, dev, f'disk{di}', int(d.get('size') or 0))
            if not m.filesystems:
                raise RuntimeError('no mountable filesystem found in this point')
        except Exception:
            self._teardown(m)
            raise
        with self._lock:
            self.mounts[mid] = m
        log.info('point mounted', extra={'mount': mid, 'filesystems': [f['name'] for f in m.filesystems]})
        return m

    def _attach_disk(self, m: Mount, dev: str, disk_name: str, size_hint: int) -> None:
        size = int(_run(['blockdev', '--getsize64', dev], run=self.run) or size_hint or 0) if os.path.exists(dev) else size_hint
        table = _run(['sfdisk', '--json', dev], check=False, run=self.run)
        for part in partitions_from_sfdisk(table, dev, size):
            name = f'{disk_name}-{part["name"]}'
            try:
                cow = self._cow_device(m, dev, part, name)
            except RuntimeError as e:
                log.warning('partition %s skipped: %s', name, e)
                continue
            info = parse_blkid(_run(['blkid', '-p', '-o', 'export', cow], check=False, run=self.run))
            fstype = (info.get('TYPE') or '').lower()
            if fstype == 'lvm2_member':
                self._attach_lvm(m, cow, name)
            elif fstype in _FS_OPTS:
                self._mount_fs(m, cow, name, fstype, info.get('LABEL'), part['size_bytes'])
            else:
                log.info('partition %s: unsupported or empty filesystem %r', name, fstype)

    def _cow_device(self, m: Mount, dev: str, part: dict, name: str) -> str:
        """read-only loop over the partition + dm snapshot with a sparse COW file -> writable, private view."""
        loop = _run(['losetup', '--find', '--show', '--read-only', '--offset', str(part['start_bytes']), '--sizelimit', str(part['size_bytes']), dev], run=self.run)
        m.cleanup.append(['losetup', '-d', loop])
        cow_file = os.path.join(m.root, f'{name}.cow')
        with open(cow_file, 'wb') as fh:
            fh.truncate(max(64 << 20, part['size_bytes'] // 50))     # 2 % of the partition, at least 64 MiB, sparse
        cow_loop = _run(['losetup', '--find', '--show', cow_file], run=self.run)
        m.cleanup.append(['losetup', '-d', cow_loop])
        dm = f'hcmflr-{m.id}-{name}'
        sectors = part['size_bytes'] // SECTOR
        # no udev in the container: no /dev/mapper/<name> symlink appears, so address the kernel node /dev/dm-<minor>
        _run(['dmsetup', 'create', dm, '--noudevsync', '--table', f'0 {sectors} snapshot {loop} {cow_loop} N 8'], run=self.run)
        m.cleanup.append(['dmsetup', 'remove', '--noudevsync', '--retry', dm])
        minor = _run(['dmsetup', 'info', '-c', '--noheadings', '-o', 'minor', dm], run=self.run).strip()
        return f'/dev/dm-{minor}' if minor.isdigit() else f'/dev/mapper/{dm}'

    def _mount_fs(self, m: Mount, dev: str, name: str, fstype: str, label: str | None, size_bytes: int) -> None:
        mtype, opts = _FS_OPTS[fstype]
        mp = os.path.join(m.root, name)
        os.makedirs(mp, exist_ok=True)
        try:
            _run(['mount', '-t', mtype, '-o', opts, dev, mp], run=self.run)
        except RuntimeError as e:
            if fstype == 'ntfs':
                _run(['mount', '-t', 'ntfs-3g', '-o', 'ro', dev, mp], run=self.run)   # kernel ntfs3 missing: userspace driver
            else:
                raise e
        m.cleanup.append(['umount', mp])
        m.filesystems.append({'name': name, 'fstype': fstype, 'mountpoint': mp, 'label': label, 'size_bytes': size_bytes})

    def _attach_lvm(self, m: Mount, pv: str, name: str) -> None:
        vg = f'hcmflr{m.id}'[:30]
        # --devices restricts LVM to the COW copy: when the proxy backs up *itself* the same PV uuid is
        # live on its own root disk and LVM would otherwise ignore the copy as a duplicate
        dev = ['--devices', pv]
        try:
            _run(['vgimportclone', '--basevgname', vg] + dev + [pv], run=self.run)
            vg_name = _run(['pvs', '--noheadings', '-o', 'vg_name'] + dev + [pv], run=self.run).strip() or vg
            _run(['vgchange', '-ay'] + dev + [vg_name], run=self.run)
            m.cleanup.append(['vgchange', '-an'] + dev + [vg_name])
            # /dev/<vg>/<lv> and /dev/mapper/* are udev symlinks that never appear inside the container:
            # address logical volumes by their kernel device-mapper minor
            rows = [l.split() for l in _run(['lvs', '--noheadings', '--separator', ' ', '-o', 'lv_name,lv_kernel_minor'] + dev + [vg_name], run=self.run).splitlines() if l.strip()]
            lvs = [(r[0], f'/dev/dm-{r[1]}') for r in rows if len(r) == 2 and r[1].isdigit()]
        except RuntimeError as e:
            log.warning('LVM import on %s failed: %s', name, e)
            return
        if not lvs:
            log.warning('LVM import on %s: no active logical volume found', name)
        for lv_label, lv in lvs:
            info = parse_blkid(_run(['blkid', '-p', '-o', 'export', lv], check=False, run=self.run))
            fstype = (info.get('TYPE') or '').lower()
            lv_name = f'{name}-{lv_label}'
            if fstype not in _FS_OPTS:
                log.info('logical volume %s: unsupported or empty filesystem %r', lv_name, fstype)
            if fstype in _FS_OPTS:
                try:
                    size = int(_run(['blockdev', '--getsize64', lv], run=self.run) or 0)
                except RuntimeError:
                    size = 0
                self._mount_fs(m, lv, lv_name, fstype, info.get('LABEL'), size)

    # ---------------------------------------------------------------- browse
    def get(self, mid: str) -> Mount:
        m = self.mounts.get(mid)
        if not m:
            raise KeyError(mid)
        m.touch()
        return m

    def _resolve(self, m: Mount, path: str) -> tuple[dict | None, str]:
        """'/<fs name>/rest' -> (filesystem, absolute path inside its mount point); '/' -> (None, '')"""
        parts = [p for p in (path or '/').split('/') if p and p not in ('.', '..')]
        if not parts:
            return None, ''
        fs = next((f for f in m.filesystems if f['name'] == parts[0]), None)
        if not fs:
            raise FileNotFoundError(path)
        abs_path = os.path.normpath(os.path.join(fs['mountpoint'], *parts[1:]))
        if not abs_path.startswith(fs['mountpoint']):
            raise FileNotFoundError(path)
        return fs, abs_path

    def ls(self, mid: str, path: str) -> dict:
        m = self.get(mid)
        fs, abs_path = self._resolve(m, path)
        if fs is None:
            return {'path': '/', 'entries': [{'name': f['name'], 'kind': 'dir', 'size': f['size_bytes'], 'fstype': f['fstype'], 'label': f.get('label')} for f in m.filesystems]}
        entries = []
        with os.scandir(abs_path) as it:
            for e in it:
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                kind = 'dir' if stat.S_ISDIR(st.st_mode) else 'link' if stat.S_ISLNK(st.st_mode) else 'file' if stat.S_ISREG(st.st_mode) else 'other'
                entries.append({'name': e.name, 'kind': kind, 'size': st.st_size if kind == 'file' else None, 'mtime': int(st.st_mtime)})
        entries.sort(key=lambda x: (x['kind'] != 'dir', x['name'].lower()))
        return {'path': '/' + '/'.join(p for p in path.split('/') if p), 'entries': entries}

    def open_file(self, mid: str, path: str):
        """(file object, size, basename) for streaming a regular file."""
        m = self.get(mid)
        fs, abs_path = self._resolve(m, path)
        if fs is None or not os.path.isfile(abs_path):
            raise FileNotFoundError(path)
        return open(abs_path, 'rb'), os.path.getsize(abs_path), os.path.basename(abs_path)

    def iter_zip(self, mid: str, paths: list[str], chunk: int = 1 << 20):
        """Stream a zip of files and directories (recursively), without materialising it."""
        m = self.get(mid)
        buf = _StreamBuffer()
        zf = zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED, allowZip64=True)
        for p in paths:
            fs, abs_path = self._resolve(m, p)
            if fs is None:
                continue
            arc_root = '/'.join(x for x in p.split('/') if x)
            if os.path.isdir(abs_path):
                for dirpath, _, files in os.walk(abs_path):
                    for f in files:
                        full = os.path.join(dirpath, f)
                        if os.path.isfile(full):
                            zf.write(full, os.path.join(arc_root, os.path.relpath(full, abs_path)))
                            yield from buf.drain()
            elif os.path.isfile(abs_path):
                zf.write(abs_path, arc_root)
                yield from buf.drain()
        zf.close()
        yield from buf.drain()

    # --------------------------------------------------------------- unmount
    def unmount(self, mid: str) -> None:
        with self._lock:
            m = self.mounts.pop(mid, None)
        if m:
            self._teardown(m)

    def _teardown(self, m: Mount) -> None:
        for cmd in reversed(m.cleanup):
            try:
                if cmd[0] == '__release_target__':
                    self.mounter.release(cmd[1])
                else:
                    _run(cmd, check=False, run=self.run, timeout=120)
            except Exception:                           # noqa: BLE001
                log.warning('cleanup step failed: %s', cmd, exc_info=True)
        try:
            m.stack.close()
        except Exception:                               # noqa: BLE001
            log.warning('nbd detach failed for mount %s', m.id, exc_info=True)
        shutil.rmtree(m.root, ignore_errors=True)

    def sweep_idle(self) -> int:
        now = time.time()
        idle = [mid for mid, m in list(self.mounts.items()) if now - m.last_used > self.idle_seconds]
        for mid in idle:
            log.info('unmounting idle point mount %s', mid)
            self.unmount(mid)
        return len(idle)

    def unmount_all(self) -> None:
        for mid in list(self.mounts):
            self.unmount(mid)


class _StreamBuffer(io.RawIOBase):
    """A write sink for ZipFile whose content is drained in chunks by a generator."""

    def __init__(self):
        self._chunks: list[bytes] = []
        self._pos = 0

    def writable(self):
        return True

    def write(self, b):
        data = bytes(b)
        self._chunks.append(data)
        self._pos += len(data)
        return len(data)

    def tell(self):
        return self._pos

    def seekable(self):
        return False

    def drain(self):
        chunks, self._chunks = self._chunks, []
        for c in chunks:
            yield c
