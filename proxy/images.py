"""
images.py — qcow2 images on the target, written through qemu-nbd.

Two backends share one interface so the job engine can be tested without root, nbd or qemu:

  NbdBackend   production: qemu-img create (2 MiB clusters, optional backing file), qemu-nbd
               attach from a bounded pool of /dev/nbdN slots, intervals written at offsets,
               flush + detach; flatten and check via qemu-img.
  FileBackend  tests: the "image" is a sparse raw file; a delta starts as a copy of its backing
               file so the resulting content equals what a qcow2 chain would present.

prepare() yields a writable path (device or file) for the duration of the with-block.
open_readonly() does the same read-only (verification, staging for restores).
"""
from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import threading
import time


class NbdPool:
    """Hands out /dev/nbdN slots; a slot is held for as long as an image is attached."""

    def __init__(self, slots: int, dev_dir: str = '/dev'):
        self._free = [f'{dev_dir}/nbd{i}' for i in range(slots)]
        self._cv = threading.Condition()

    def acquire(self, timeout: float = 600) -> str:
        with self._cv:
            if not self._cv.wait_for(lambda: self._free, timeout=timeout):
                raise RuntimeError('no free nbd slot')
            return self._free.pop(0)

    def release(self, slot: str) -> None:
        with self._cv:
            self._free.append(slot)
            self._cv.notify()

    @property
    def free(self) -> int:
        with self._cv:
            return len(self._free)


def _run(cmd: list[str], timeout: int = 3600) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        raise RuntimeError(f'{" ".join(cmd)} failed ({r.returncode}): {r.stderr.strip() or r.stdout.strip()}')
    return r.stdout


class NbdBackend:
    name = 'nbd'

    def __init__(self, pool: NbdPool):
        self.pool = pool

    def create(self, path: str, size: int, backing: str | None = None) -> None:
        cmd = ['qemu-img', 'create', '-q', '-f', 'qcow2', '-o', 'cluster_size=2M']
        if backing:
            # relative backing name: the chain stays valid when the target is mounted elsewhere
            cmd += ['-F', 'qcow2', '-b', os.path.relpath(backing, os.path.dirname(path))]
        _run(cmd + [path, str(size)])

    @contextlib.contextmanager
    def _attached(self, path: str, readonly: bool):
        slot = self.pool.acquire()
        try:
            cmd = ['qemu-nbd', '-c', slot, '--cache=writeback', '-f', 'qcow2']
            if readonly:
                cmd.append('-r')
            _run(cmd + [path], timeout=120)
            name = os.path.basename(slot)
            for _ in range(60):
                try:
                    with open(f'/sys/block/{name}/size') as fh:
                        if int(fh.read().strip()) > 0:
                            break
                except OSError:
                    pass
                time.sleep(0.5)
            else:
                raise RuntimeError(f'{slot} did not come up')
            try:
                yield slot
            finally:
                subprocess.run(['sync'], check=False)
                subprocess.run(['blockdev', '--flushbufs', slot], check=False, capture_output=True)
                subprocess.run(['udevadm', 'settle'], check=False, capture_output=True, timeout=60)
                _run(['qemu-nbd', '-d', slot], timeout=120)
        finally:
            self.pool.release(slot)

    @contextlib.contextmanager
    def prepare(self, path: str, size: int, backing: str | None = None):
        self.create(path, size, backing)
        with self._attached(path, readonly=False) as dev:
            yield dev

    @contextlib.contextmanager
    def open_readonly(self, path: str):
        with self._attached(path, readonly=True) as dev:
            yield dev

    def flatten(self, src: str, dst: str) -> None:
        _run(['qemu-img', 'convert', '-q', '-f', 'qcow2', '-O', 'qcow2', '-o', 'cluster_size=2M', src, dst])

    def commit(self, delta: str) -> None:
        """Merge a delta into its backing file (retention)."""
        _run(['qemu-img', 'commit', '-q', '-f', 'qcow2', delta])

    def check(self, path: str) -> bool:
        return subprocess.run(['qemu-img', 'check', '-q', path], capture_output=True).returncode == 0

    def raw_to_image(self, raw_path: str, path: str) -> None:
        _run(['qemu-img', 'convert', '-q', '-f', 'raw', '-O', 'qcow2', '-o', 'cluster_size=2M', raw_path, path])


class FileBackend:
    """Raw sparse files instead of qcow2; a delta is seeded with its backing file's content."""
    name = 'file'

    def create(self, path: str, size: int, backing: str | None = None) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if backing:
            shutil.copyfile(backing, path)
        with open(path, 'ab') as fh:
            fh.truncate(size)

    @contextlib.contextmanager
    def prepare(self, path: str, size: int, backing: str | None = None):
        self.create(path, size, backing)
        yield path

    @contextlib.contextmanager
    def open_readonly(self, path: str):
        yield path

    def flatten(self, src: str, dst: str) -> None:
        shutil.copyfile(src, dst)

    def commit(self, delta: str) -> None:
        raise NotImplementedError('file backend keeps no backing chain')

    def check(self, path: str) -> bool:
        return os.path.exists(path)

    def raw_to_image(self, raw_path: str, path: str) -> None:
        shutil.copyfile(raw_path, path)


def make_backend(kind: str, nbd_slots: int, dev_dir: str = '/dev'):
    return FileBackend() if kind == 'file' else NbdBackend(NbdPool(nbd_slots, dev_dir))
