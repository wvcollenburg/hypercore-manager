"""
copier.py — copy changed-block intervals from one block device (or file) to another.

Intervals come from HyperCore's snapDiff: inclusive [lower, upper] in 1 MiB blocks. A block
marked TO_IS_HOLE became empty in the snapshot and is written as zeros so a delta image
overrides whatever the backing image holds there. Reads and writes use pread/pwrite at the same
offsets, so the function works on hot-added devices, nbd devices and plain files alike.
"""
from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable

MiB = 1 << 20
_ZERO = bytes(MiB)


def blocks_in(intervals: Iterable[dict]) -> int:
    return sum(int(iv['upper']) - int(iv['lower']) + 1 for iv in intervals)


def _chunks(intervals: list[dict], max_blocks: int = 64):
    """Split intervals into (first_block, n_blocks, is_hole) work items of bounded size."""
    for iv in intervals:
        lo, hi = int(iv['lower']), int(iv['upper'])
        hole = iv.get('differentHow') == 'TO_IS_HOLE'
        while lo <= hi:
            n = min(max_blocks, hi - lo + 1)
            yield lo, n, hole
            lo += n


def copy_intervals(src: str, dst: str, intervals: list[dict], *, writers: int = 4,
                   progress: Callable[[int, int], None] | None = None,
                   cancelled: Callable[[], bool] | None = None) -> int:
    """
    Copy every block of `intervals` from src to dst at identical offsets.
    Returns the number of bytes written (holes count). Raises RuntimeError when cancelled.
    """
    total = blocks_in(intervals) * MiB
    src_size = os.lseek(os.open(src, os.O_RDONLY), 0, os.SEEK_END) if os.path.exists(src) else 0
    done = 0
    lock = threading.Lock()
    stop = threading.Event()

    def work(item):
        nonlocal done
        first, n, hole = item
        sfd = os.open(src, os.O_RDONLY)
        dfd = os.open(dst, os.O_WRONLY)
        try:
            for blk in range(first, first + n):
                if stop.is_set() or (cancelled and cancelled()):
                    stop.set()
                    return
                off = blk * MiB
                if off >= src_size and not hole:
                    break                       # map points past the end of a shorter device
                data = _ZERO if hole else os.pread(sfd, MiB, off)
                if not data:
                    break
                os.pwrite(dfd, data, off)
                with lock:
                    done += len(data)
                    if progress:
                        progress(done, total)
        finally:
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
