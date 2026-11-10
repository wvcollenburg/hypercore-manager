"""
images.py — qcow2 images on the target, written through qemu-nbd.

Two backends share one interface so the job engine can be tested without root, nbd or qemu:

  NbdBackend   production: qemu-img create (1 MiB clusters = one snapDiff block, optional backing
               file), qemu-nbd attach from a bounded pool of /dev/nbdN slots, intervals written at
               offsets, flush + detach; flatten and check via qemu-img.

Why a delta is correct: the backing file is set when the delta is *created*, so a write that
covers part of a cluster makes qemu copy the rest of that cluster from the backing file (COW)
instead of leaving zeros, and a block written as zeros (a hole) allocates a zero cluster that
masks the backing file. With 1 MiB clusters every snapDiff block is exactly one cluster, so the
COW case only arises for the short tail of a disk whose size is not a whole number of MiB.
selfcheck() proves these two properties on the qemu that is actually installed.
  FileBackend  tests: the "image" is a sparse raw file; a delta starts as a copy of its backing
               file so the resulting content equals what a qcow2 chain would present.

prepare() yields a writable path (device or file) for the duration of the with-block.
open_readonly() does the same read-only (verification, staging for restores).
"""
from __future__ import annotations

import contextlib
import errno
import logging
import mmap
import os
import shutil
import subprocess
import threading
import time

import copier

log = logging.getLogger('proxy.images')


class NbdPool:
    """Hands out /dev/nbdN slots; a slot is held for as long as an image is attached. A slot that
    refuses I/O is retired for the life of the process (see NbdBackend._probe)."""

    def __init__(self, slots: int, dev_dir: str = '/dev'):
        self.all_slots = [f'{dev_dir}/nbd{i}' for i in range(slots)]
        self._free = list(self.all_slots)
        self.retired: list[str] = []
        self._cv = threading.Condition()

    @property
    def size(self) -> int:
        return len(self.all_slots)

    def acquire(self, timeout: float = 600) -> str:
        with self._cv:
            if len(self.retired) >= self.size:
                raise RuntimeError('every nbd slot is retired; restart the proxy container (or its VM)')
            if not self._cv.wait_for(lambda: self._free, timeout=timeout):
                raise RuntimeError('no free nbd slot')
            return self._free.pop(0)

    def release(self, slot: str) -> None:
        with self._cv:
            self._free.append(slot)
            self._cv.notify()

    def retire(self, slot: str) -> None:
        with self._cv:
            if slot not in self.retired:
                self.retired.append(slot)
            self._cv.notify_all()

    @property
    def free(self) -> int:
        with self._cv:
            return len(self._free)


_CHECK_NOISE = ('Leaked cluster', 'ERROR cluster', 'ERROR OFLAG', 'ERROR l2', 'ERROR refcount', 'Image end offset')


def _last_lines(text: str, n: int = 3, limit: int = 400) -> str:
    """The verdict of a tool's output for an error message. qemu-img check lists every leaked or
    corrupt cluster first and ends with allocation statistics; both are left out, the lines in
    between ("N errors were found…", a lock message) are what matters."""
    lines = [l.strip() for l in (text or '').splitlines() if l.strip()]
    kept = [l for l in lines if not l.startswith(_CHECK_NOISE) and '% allocated' not in l] or lines
    return ' | '.join(kept[-n:])[-limit:]


def _run(cmd: list[str], timeout: int = 3600) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        raise RuntimeError(f'{" ".join(cmd)} failed ({r.returncode}): {r.stderr.strip() or r.stdout.strip()}')
    return r.stdout


CACHE_MODES = ('writeback', 'unsafe', 'none', 'writethrough', 'directsync')


class NbdBackend:
    name = 'nbd'

    def __init__(self, pool: NbdPool, lazy_refcounts: bool = True, cache: str = 'unsafe'):
        self.pool = pool
        # lazy_refcounts=on: the qcow2 driver no longer flushes between the refcount and the L2
        # update of every allocated cluster (a real SMB FLUSH per MiB on a CIFS share); an image
        # that was not closed cleanly is repaired on the next open, and `check` after each image
        # still sees a dirty one. cache: qemu-nbd's cache mode for writable attaches.
        self.lazy_refcounts = bool(lazy_refcounts)
        self.cache = cache if cache in CACHE_MODES else 'writeback'

    CLUSTER = '1M'          # one snapDiff block per qcow2 cluster (flatten keeps 2M, HyperCore's own size)

    @property
    def options(self) -> dict:
        return {'lazy_refcounts': self.lazy_refcounts, 'cache': self.cache}

    def create(self, path: str, size: int, backing: str | None = None, lazy_refcounts: bool | None = None) -> None:
        lazy = self.lazy_refcounts if lazy_refcounts is None else bool(lazy_refcounts)
        cmd = ['qemu-img', 'create', '-q', '-f', 'qcow2', '-o', f'cluster_size={self.CLUSTER}' + (',lazy_refcounts=on' if lazy else '')]
        if backing:
            # relative backing name: the chain stays valid when the target is mounted elsewhere
            cmd += ['-F', 'qcow2', '-b', os.path.relpath(backing, os.path.dirname(path))]
        _run(cmd + [path, str(size)])

    sys_block = '/sys/block'

    def reset_all(self) -> int:
        """Disconnect every slot (leftovers of a previous container life; the container is the only
        nbd user on its host). Returns how many disconnects the kernel accepted."""
        n = 0
        for slot in self.pool.all_slots:
            r = subprocess.run(['qemu-nbd', '-d', slot], check=False, capture_output=True, text=True)
            n += (getattr(r, 'returncode', 1) == 0)
        return n

    def _connect(self, slot: str, path: str, readonly: bool, cache: str | None = None) -> None:
        mode = cache if cache in CACHE_MODES else ('writeback' if readonly else self.cache)
        cmd = ['qemu-nbd', '-c', slot, f'--cache={mode}', '-f', 'qcow2']
        if readonly:
            cmd.append('-r')
        _run(cmd + [path], timeout=120)
        name = os.path.basename(slot)
        for _ in range(60):
            try:
                with open(f'{self.sys_block}/{name}/size') as fh:
                    if int(fh.read().strip()) > 0:
                        return
            except OSError:
                pass
            time.sleep(0.5)
        raise RuntimeError(f'{slot} did not come up')

    def _disconnect(self, slot: str, strict: bool) -> None:
        subprocess.run(['sync'], check=False)
        subprocess.run(['blockdev', '--flushbufs', slot], check=False, capture_output=True)
        subprocess.run(['udevadm', 'settle'], check=False, capture_output=True, timeout=60)
        if strict:
            _run(['qemu-nbd', '-d', slot], timeout=120)
        else:
            subprocess.run(['qemu-nbd', '-d', slot], check=False, capture_output=True, timeout=120)
        # `qemu-nbd -d` only tears down the kernel device; the server process that held the image
        # exits on its own afterwards, writing its qcow2 metadata and dropping the image lock as it
        # closes. Anything that opens the image next (qemu-img check, the next point's backing
        # file) must wait for that, or it meets a lock or half-written metadata (0.5.4/0.5.5:
        # ~2-3 % of backups failed their image check two seconds in).
        self._wait_server_gone(slot)
        # qemu writes its qcow2 metadata caches when it closes the image; with --cache=unsafe
        # nothing flushed them to the share yet, so push the page cache out now
        subprocess.run(['sync'], check=False)

    proc_dir = '/proc'
    server_exit_timeout = 60.0

    def _server_pids(self, slot: str) -> list[int]:
        """Live qemu-nbd servers attached to this slot (`qemu-nbd -c <slot> ...`). An exited server
        that is not reaped yet has an empty cmdline and so does not count: its image is closed."""
        pids = []
        try:
            entries = os.listdir(self.proc_dir)
        except OSError:
            return pids
        for e in entries:
            if not e.isdigit():
                continue
            try:
                with open(os.path.join(self.proc_dir, e, 'cmdline'), 'rb') as fh:
                    argv = [a.decode('utf-8', 'replace') for a in fh.read().split(b'\0') if a]
            except OSError:
                continue
            if not argv or os.path.basename(argv[0]) != 'qemu-nbd':
                continue
            for i, a in enumerate(argv):
                if (a == '-c' and i + 1 < len(argv) and argv[i + 1] == slot) or a in (f'-c{slot}', f'--connect={slot}'):
                    pids.append(int(e))
                    break
        return pids

    def _wait_server_gone(self, slot: str) -> None:
        deadline = time.monotonic() + self.server_exit_timeout
        waited = False
        while True:
            pids = self._server_pids(slot)
            if not pids:
                if waited:
                    log.info('qemu-nbd server of %s exited after the disconnect', slot)
                return
            if time.monotonic() >= deadline:
                log.warning('qemu-nbd server of %s (pid %s) still running %.0fs after the disconnect',
                            slot, ', '.join(map(str, pids)), self.server_exit_timeout)
                return
            waited = True
            time.sleep(0.2)

    def _probe(self, slot: str) -> None:
        """Read the first 4 KiB with O_DIRECT. A slot whose previous connection died without a clean
        disconnect (container restart while an image was attached) stays configured in the kernel
        and answers every request with EIO; it must not be used for a backup."""
        fd = os.open(slot, os.O_RDONLY | getattr(os, 'O_DIRECT', 0))
        try:
            buf = mmap.mmap(-1, 4096)
            try:
                if os.preadv(fd, [buf], 0) != 4096:
                    raise OSError(errno.EIO, 'short read')
            finally:
                buf.close()
        finally:
            os.close(fd)

    def _connect_usable(self, path: str, readonly: bool, cache: str | None = None) -> str:
        """A connected slot that passed the probe; poisoned slots are retired and the next one tried."""
        while True:
            slot = self.pool.acquire()
            try:
                self._connect(slot, path, readonly, cache)
                self._probe(slot)
                return slot
            except OSError as e:
                log.warning('%s refuses I/O (%s); the slot is retired until the proxy restarts', slot, e)
                self._disconnect(slot, strict=False)
                self.pool.retire(slot)
                if len(self.pool.retired) >= self.pool.size:
                    raise RuntimeError('every nbd slot refuses I/O; restart the proxy container (or its VM)') from e
            except BaseException:
                self.pool.release(slot)
                raise

    @contextlib.contextmanager
    def _attached(self, path: str, readonly: bool, cache: str | None = None):
        slot = self._connect_usable(path, readonly, cache)
        try:
            yield slot
        finally:
            try:
                self._disconnect(slot, strict=True)
            finally:
                self.pool.release(slot)

    @contextlib.contextmanager
    def prepare(self, path: str, size: int, backing: str | None = None, *, lazy_refcounts: bool | None = None, cache: str | None = None):
        """A writable device for a new image; the keyword overrides exist for the bench job."""
        self.create(path, size, backing, lazy_refcounts=lazy_refcounts)
        with self._attached(path, readonly=False, cache=cache) as dev:
            yield dev

    @contextlib.contextmanager
    def open_readonly(self, path: str, cache: str | None = None):
        with self._attached(path, readonly=True, cache=cache) as dev:
            yield dev

    def flatten(self, src: str, dst: str) -> None:
        _run(['qemu-img', 'convert', '-q', '-f', 'qcow2', '-O', 'qcow2', '-o', 'cluster_size=2M', src, dst])

    def synthesize(self, src: str, dst: str) -> None:
        """A point's image (through its backing chain) as a standalone base of a new chain, in the
        chain's own cluster size so the deltas written on top keep one snapDiff block per cluster."""
        _run(['qemu-img', 'convert', '-q', '-f', 'qcow2', '-O', 'qcow2', '-o', f'cluster_size={self.CLUSTER}', src, dst])

    def commit(self, delta: str) -> None:
        """Merge a delta into its backing file (retention)."""
        _run(['qemu-img', 'commit', '-q', '-f', 'qcow2', delta])

    def rebase(self, image: str, new_backing: str) -> None:
        """Point an image at another backing file whose content is equivalent (unsafe mode: no data copy)."""
        _run(['qemu-img', 'rebase', '-q', '-u', '-f', 'qcow2', '-F', 'qcow2', '-b', os.path.relpath(new_backing, os.path.dirname(image)), image])

    check_lock_retries = 3
    check_lock_wait = 2.0

    def check_detail(self, path: str) -> str | None:
        """qemu-img check of a finished image: None when it is sound, otherwise the reason.
        Exit codes: 0 clean, 1 check could not run (e.g. the image is still locked), 2 corruption,
        3 leaked clusters only (wasted space, no wrong data: passes with a warning). A lock
        conflict is retried a few times and logged, so the bundle shows whether it still occurs."""
        for attempt in range(self.check_lock_retries + 1):
            r = subprocess.run(['qemu-img', 'check', path], capture_output=True, text=True, timeout=3600)
            rc = r.returncode
            msg = (r.stderr or '').strip() or (r.stdout or '').strip()
            if rc == 0:
                return None
            if rc == 3:
                log.warning('qemu-img check: %s has leaked clusters only (no corruption): %s',
                            os.path.basename(path), _last_lines(msg))
                return None
            if rc == 1 and 'lock' in msg.lower() and attempt < self.check_lock_retries:
                log.warning('qemu-img check: %s is still locked, retrying in %.0fs: %s',
                            os.path.basename(path), self.check_lock_wait, _last_lines(msg))
                time.sleep(self.check_lock_wait)
                continue
            detail = f'qemu-img check exit {rc}: {_last_lines(msg) or "no output"}'
            log.error('image check failed for %s: %s', path, detail)
            return detail
        return None                                        # not reached

    def check(self, path: str) -> bool:
        return self.check_detail(path) is None

    def raw_to_image(self, raw_path: str, path: str) -> None:
        _run(['qemu-img', 'convert', '-q', '-f', 'raw', '-O', 'qcow2', '-o', 'cluster_size=2M', raw_path, path])

    def shrink(self, path: str, size: int) -> None:
        _run(['qemu-img', 'resize', '-q', '--shrink', '-f', 'qcow2', path, str(size)])

    def stage_into(self, image: str, dev: str, intervals: list[dict], writers: int, progress=None, cancelled=None, direct: bool = True) -> int:
        """Write a point's image (through its backing chain) onto a zeroed staging device with one
        qemu-img convert: qemu resolves the chain in-process with 16 coroutines and out-of-order
        writes, which measured twice the rate of reading through an nbd attach (lab, 2026-10-22:
        59.5 vs 28.7 MiB/s). The target is written with direct I/O so no dirty pages pile up in
        the guest's page cache; unallocated clusters are skipped because the device reads as
        zeros. Falls back to the copier when the device is smaller than the image (older
        chains). Returns the bytes the intervals cover."""
        import re
        total = copier.blocks_in(intervals) * MiB
        if 0 < copier.size_of(dev) < self.virtual_size(image):
            log.warning('staging device smaller than the image; copying through nbd instead')
            with self.open_readonly(image) as src:
                return copier.copy_intervals(src, dev, intervals, writers=writers, progress=progress, cancelled=cancelled)
        cmd = ['qemu-img', 'convert', '-p', '-n', '-W', '-m', '16', '--target-is-zero', '-f', 'qcow2', '-O', 'raw']
        if direct:
            cmd += ['-t', 'none']
        proc = subprocess.Popen(cmd + [image, dev], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        buf = ''
        try:
            while True:
                chunk = proc.stdout.read(64)
                if not chunk:
                    break
                buf += chunk
                for m in re.finditer(r'\((\d+(?:\.\d+)?)/100%\)', buf):
                    if progress:
                        progress(int(total * float(m.group(1)) / 100), total)
                buf = buf[-64:]
                if cancelled and cancelled():
                    proc.terminate()
                    raise RuntimeError('cancelled')
        finally:
            proc.wait(timeout=600)
        if proc.returncode:
            raise RuntimeError(f'qemu-img convert failed ({proc.returncode}): {proc.stderr.read().strip()[:300]}')
        if progress:
            progress(total, total)
        return total

    def virtual_size(self, path: str) -> int:
        import json
        return int(json.loads(_run(['qemu-img', 'info', '--output=json', '-f', 'qcow2', path]))['virtual-size'])

    def allocation_map(self, path: str) -> list[dict]:
        """Allocated, non-zero extents of an image (through its backing chain) as 1 MiB intervals."""
        import json
        extents = json.loads(_run(['qemu-img', 'map', '--output=json', '-f', 'qcow2', path]))
        return extents_to_intervals((e['start'], e['length']) for e in extents if e.get('data') and not e.get('zero'))

    def selfcheck(self, workdir: str) -> dict:
        """Prove the delta semantics on this host's qemu with a 4 MiB throwaway chain, through the
        same create / nbd-write path a backup uses: a parent of 0xAA; a delta with the parent as
        backing file into which block 1 is written with 0x5A, block 2 with zeros and the first
        half of block 3 with 0x5A. Reading the delta must give AA | 5A | 00 | 5A+AA. Raises
        RuntimeError with the failing read when it does not; returns timings when it does."""
        os.makedirs(workdir, exist_ok=True)
        parent, delta = os.path.join(workdir, 'parent.qcow2'), os.path.join(workdir, 'delta.qcow2')
        t0 = time.time()
        try:
            self.create(parent, 4 * MiB)
            with self._attached(parent, readonly=False) as dev:
                _write_pattern(dev, 0, 4 * MiB, 0xAA)
            self.create(delta, 4 * MiB, backing=parent)
            with self._attached(delta, readonly=False) as dev:
                _write_pattern(dev, 1 * MiB, MiB, 0x5A)
                _write_pattern(dev, 2 * MiB, MiB, 0x00)
                _write_pattern(dev, 3 * MiB, MiB // 2, 0x5A)
            _run(['qemu-io', '-r', '-f', 'qcow2', '-c', 'read -P 0xaa 0 1M', '-c', 'read -P 0x5a 1M 1M',
                  '-c', 'read -P 0 2M 1M', '-c', 'read -P 0x5a 3M 512k', '-c', 'read -P 0xaa 3584k 512k', delta], timeout=120)
            problem = self.check_detail(delta)
            if problem:
                raise RuntimeError(f'self-check delta: {problem}')
        except RuntimeError as e:
            raise RuntimeError(f'proxy self-check failed, this host cannot write a correct delta: {e}') from e
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        return {'ok': True, 'seconds': round(time.time() - t0, 2)}


MiB = 1 << 20


def _write_pattern(dev: str, offset: int, length: int, byte: int) -> None:
    """Write `length` bytes of `byte` at `offset` with O_DIRECT in 1 MiB pieces (as the copier does)."""
    import mmap
    fd = os.open(dev, os.O_WRONLY | getattr(os, 'O_DIRECT', 0))
    try:
        buf = mmap.mmap(-1, MiB)
        try:
            buf[:] = bytes([byte]) * MiB
            while length > 0:
                n = min(MiB, length)
                os.pwritev(fd, [memoryview(buf)[:n]], offset)
                offset += n; length -= n
        finally:
            buf.close()
        os.fsync(fd)
    finally:
        os.close(fd)


def extents_to_intervals(extents, block: int = 1 << 20) -> list[dict]:
    """Byte extents -> merged inclusive 1 MiB block intervals [{lower, upper}]."""
    blocks = set()
    for start, length in extents:
        if length <= 0:
            continue
        for b in range(start // block, (start + length - 1) // block + 1):
            blocks.add(b)
    out, run_start, prev = [], None, None
    for b in sorted(blocks):
        if run_start is None:
            run_start = prev = b
        elif b == prev + 1:
            prev = b
        else:
            out.append({'lower': run_start, 'upper': prev}); run_start = prev = b
    if run_start is not None:
        out.append({'lower': run_start, 'upper': prev})
    return out


class FileBackend:
    """Raw sparse files instead of qcow2; a delta is seeded with its backing file's content."""
    name = 'file'

    def create(self, path: str, size: int, backing: str | None = None) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if backing:
            shutil.copyfile(backing, path)
        with open(path, 'ab') as fh:
            fh.truncate(size)

    options: dict = {}

    @contextlib.contextmanager
    def prepare(self, path: str, size: int, backing: str | None = None, **_options):
        self.create(path, size, backing)
        yield path

    @contextlib.contextmanager
    def open_readonly(self, path: str, cache: str | None = None):
        yield path

    def flatten(self, src: str, dst: str) -> None:
        shutil.copyfile(src, dst)

    def synthesize(self, src: str, dst: str) -> None:
        shutil.copyfile(src, dst)                         # raw files are complete

    def commit(self, delta: str) -> None:
        pass                              # raw 'deltas' are complete; prune copies them over the base itself

    def rebase(self, image: str, new_backing: str) -> None:
        pass

    def check_detail(self, path: str) -> str | None:
        return None if os.path.exists(path) else 'image missing'

    def check(self, path: str) -> bool:
        return self.check_detail(path) is None

    def raw_to_image(self, raw_path: str, path: str) -> None:
        shutil.copyfile(raw_path, path)

    def shrink(self, path: str, size: int) -> None:
        with open(path, 'r+b') as fh:
            fh.truncate(size)

    def selfcheck(self, workdir: str) -> dict:
        return {'ok': True, 'seconds': 0.0}

    def stage_into(self, image: str, dev: str, intervals: list[dict], writers: int, progress=None, cancelled=None, direct: bool = True) -> int:
        return copier.copy_intervals(image, dev, intervals, writers=writers, progress=progress, cancelled=cancelled)

    def allocation_map(self, path: str) -> list[dict]:
        """Non-zero 1 MiB blocks of a raw file."""
        block = 1 << 20; ext = []
        with open(path, 'rb') as fh:
            i = 0
            while True:
                chunk = fh.read(block)
                if not chunk:
                    break
                if chunk.strip(b'\0'):
                    ext.append((i * block, len(chunk)))
                i += 1
        return extents_to_intervals(ext)


def make_backend(kind: str, nbd_slots: int, dev_dir: str = '/dev', lazy_refcounts: bool = True, cache: str = 'unsafe'):
    return FileBackend() if kind == 'file' else NbdBackend(NbdPool(nbd_slots, dev_dir), lazy_refcounts=lazy_refcounts, cache=cache)
