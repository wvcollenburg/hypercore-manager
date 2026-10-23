"""
copier.py — copy changed-block intervals from one block device (or file) to another.

Intervals come from HyperCore's snapDiff: inclusive [lower, upper] in 1 MiB blocks. A block
marked TO_IS_HOLE became empty in the snapshot and is written as zeros so a delta image
overrides whatever the backing image holds there. Reads and writes use pread/pwrite at the same
offsets, so the function works on hot-added devices, nbd devices and plain files alike.

differentHow is "ADDRESS_CHANGED" / "TO_IS_HOLE" by the v2 spec, but clusters up to at least 9.7
send the enum as an integer (0 / 1) through a conversion bug in the REST layer; both forms are
read. An unknown value is treated as changed, which is the safe direction: the block is read from
the device (a hole reads as zeros) and written, so the delta is right either way.
"""
from __future__ import annotations

import mmap
import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable

MiB = 1 << 20


def _is_block_device(path: str) -> bool:
    try:
        return stat.S_ISBLK(os.stat(path).st_mode)
    except OSError:
        return False


def _open_write(path: str) -> int:
    """Block devices are written with O_DIRECT (page-aligned 1 MiB buffers) so the guest's page
    cache cannot fill up with dirty nbd pages; that is what OOM-killed the proxy VM on the first
    live run. Regular files (tests, raw backend) are written normally."""
    flags = os.O_WRONLY
    if _is_block_device(path) and hasattr(os, 'O_DIRECT'):
        flags |= os.O_DIRECT
    return os.open(path, flags)


class _Buf:
    """A page-aligned 1 MiB buffer (mmap) usable for O_DIRECT reads and writes."""

    def __init__(self):
        self.m = mmap.mmap(-1, MiB)
        self.zero = mmap.mmap(-1, MiB)

    def close(self):
        self.m.close()
        self.zero.close()


def blocks_in(intervals: Iterable[dict]) -> int:
    return sum(int(iv['upper']) - int(iv['lower']) + 1 for iv in intervals)


def is_hole(interval: dict) -> bool:
    """TO_IS_HOLE in either wire form (string enum or the integer 1)."""
    how = interval.get('differentHow')
    return how == 'TO_IS_HOLE' or how == 1 or how == '1'


def _chunks(intervals: list[dict], max_blocks: int = 64):
    """Split intervals into (first_block, n_blocks, is_hole) work items of bounded size."""
    for iv in intervals:
        lo, hi = int(iv['lower']), int(iv['upper'])
        hole = is_hole(iv)
        while lo <= hi:
            n = min(max_blocks, hi - lo + 1)
            yield lo, n, hole
            lo += n


def size_of(path: str) -> int:
    """Byte size of a file or block device, 0 when it does not exist. The descriptor is closed
    again: an open descriptor on /dev/nbdN keeps the kernel's nbd configuration alive, and a
    configuration that outlives its qemu-nbd answers every later request with EIO."""
    if not os.path.exists(path):
        return 0
    fd = os.open(path, os.O_RDONLY)
    try:
        return os.lseek(fd, 0, os.SEEK_END)
    finally:
        os.close(fd)


def copy_intervals(src: str, dst: str, intervals: list[dict], *, writers: int = 4,
                   progress: Callable[[int, int], None] | None = None,
                   cancelled: Callable[[], bool] | None = None) -> int:
    """
    Copy every block of `intervals` from src to dst at identical offsets.
    Returns the number of bytes written (holes count). Raises RuntimeError when cancelled.
    """
    total = blocks_in(intervals) * MiB
    src_size, dst_size = size_of(src), size_of(dst)
    done = 0
    lock = threading.Lock()
    stop = threading.Event()

    def work(item):
        nonlocal done
        first, n, hole = item
        sfd = os.open(src, os.O_RDONLY)
        dfd = _open_write(dst)
        buf = _Buf()
        try:
            for blk in range(first, first + n):
                if stop.is_set() or (cancelled and cancelled()):
                    stop.set()
                    return
                off = blk * MiB
                if off >= src_size and not hole:
                    break                       # map points past the end of a shorter device
                if hole:
                    nread = MiB
                    view = buf.zero
                else:
                    nread = os.preadv(sfd, [buf.m], off)
                    view = buf.m
                if nread <= 0:
                    break
                if dst_size and off + nread > dst_size:
                    nread = max(0, dst_size - off)          # destination smaller than the source (NVRAM image)
                    if nread <= 0:
                        break
                if nread < MiB:                 # tail of the device: O_DIRECT needs whole sectors
                    padded = (nread + 511) // 512 * 512
                    view[nread:padded] = bytes(padded - nread)
                    nread = padded
                os.pwritev(dfd, [memoryview(view)[:nread]], off)
                with lock:
                    done += nread
                    if progress:
                        progress(done, total)
        finally:
            buf.close()
            os.close(dfd)
            os.close(sfd)

    with ThreadPoolExecutor(max_workers=max(1, writers)) as pool:
        list(pool.map(work, _chunks(intervals)))
    if stop.is_set():
        raise RuntimeError('copy cancelled')
    fd = os.open(dst, os.O_WRONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return done
