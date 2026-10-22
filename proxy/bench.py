"""
bench.py — measure throughput onto a backup target through the paths a backup and a restore use.

Writes `size_mib` of random data per mode and reports MiB/s. The source is a file on the proxy's
own disk, written before the clock starts and evicted from the page cache before every mode, so
each mode reads it from the VM's disk (on HyperCore: from Scribe over the backplane, as a backup
reads its snapshot clone) and writes it to the target:

  plain            the copier writing a flat file on the share (no qcow2, no nbd): the ceiling
  nbd              a qcow2 image through qemu-nbd with the proxy's current options (what a
                   backup does on this proxy right now)
  nbd_writeback    strict qcow2 (no lazy refcounts) and qemu-nbd --cache=writeback: the
                   original behaviour, every flush honoured
  nbd_lazy         lazy_refcounts=on at create time, cache writeback
  nbd_unsafe       strict qcow2, qemu-nbd --cache=unsafe (no flushes until disconnect)
  nbd_lazy_unsafe  both (the default since the lab measurement of 2026-10-22)
  nbd_none         lazy refcounts, qemu-nbd --cache=none (O_DIRECT on the share; fails where the
                   share refuses it)
  nbd_read         the restore staging path: a qcow2 image on the share read through a read-only
                   nbd attach (the client's cached pages are dropped first)

Each mode is timed from image creation to the moment the image is disconnected and flushed, so
the cost of the final flush is included, as it is in a backup job. A mode that fails is reported
with its error and the others still run. Everything is removed afterwards.
"""
from __future__ import annotations

import os
import shutil
import time

import copier

MiB = 1 << 20
# every nbd_* mode names its full option set, so the figures mean the same whatever the proxy's
# current defaults are; 'nbd' alone is "what a backup does on this proxy right now"
WRITE_MODES = {
    'plain': None,
    'nbd': {},
    'nbd_writeback': {'lazy_refcounts': False, 'cache': 'writeback'},
    'nbd_lazy': {'lazy_refcounts': True, 'cache': 'writeback'},
    'nbd_unsafe': {'lazy_refcounts': False, 'cache': 'unsafe'},
    'nbd_lazy_unsafe': {'lazy_refcounts': True, 'cache': 'unsafe'},
    'nbd_none': {'lazy_refcounts': True, 'cache': 'none'},
}
ALL_MODES = list(WRITE_MODES) + ['nbd_read']
DEFAULT_MODES = ['plain', 'nbd', 'nbd_writeback', 'nbd_lazy', 'nbd_unsafe', 'nbd_lazy_unsafe', 'nbd_read']


def drop_cached_pages(path: str) -> None:
    """Best effort: forget the client's cached pages of a file (and of everything, when the
    container may write drop_caches), so a read measures the share and not the page cache."""
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        finally:
            os.close(fd)
    except (OSError, AttributeError):
        pass
    try:
        with open('/proc/sys/vm/drop_caches', 'w') as fh:
            fh.write('1\n')
    except OSError:
        pass


def _sparse(path: str, size: int) -> None:
    with open(path, 'wb') as fh:
        fh.truncate(size)


def run(backend, root: str, data_dir: str, size_mib: int, modes: list[str], writers: int,
        progress=None, cancelled=None, job_id: str = 'bench') -> dict:
    size = size_mib * MiB
    work = os.path.join(root, '_bench', job_id)
    os.makedirs(work, exist_ok=True)
    os.makedirs(data_dir, exist_ok=True)
    src = os.path.join(data_dir, f'bench-src-{job_id}.bin')
    scratch = os.path.join(data_dir, f'bench-read-{job_id}.bin')
    intervals = [{'lower': 0, 'upper': size_mib - 1}]
    total = len(modes) * size
    results: dict[str, dict] = {}
    try:
        with open(src, 'wb') as fh:                      # random so that nothing can shortcut zeros
            block = os.urandom(MiB)
            for _ in range(size_mib):
                fh.write(block)
        done_before = 0
        for i, mode in enumerate(modes):
            if cancelled and cancelled():
                raise RuntimeError('cancelled')

            def report(n, _b=done_before, _i=i, _m=mode):
                if progress:
                    progress({'modes_total': len(modes), 'modes_done': _i, 'current': _m, 'bytes_total': total, 'bytes_done': _b + n})

            report(0)
            drop_cached_pages(src)                   # the source must come from the VM's disk (Scribe), not its page cache
            try:
                results[mode] = _run_mode(backend, mode, src, scratch, work, size, size_mib, intervals, writers, report, cancelled)
            except RuntimeError as e:
                if str(e) == 'cancelled':
                    raise
                results[mode] = {'error': str(e)[:300]}
            except OSError as e:                         # noqa: PERF203 — one mode must not hide the others
                results[mode] = {'error': str(e)[:300]}
            done_before += size
        if progress:
            progress({'modes_total': len(modes), 'modes_done': len(modes), 'bytes_total': total, 'bytes_done': total})
        return {'size_mib': size_mib, 'writers': writers, 'backend': backend.name, 'options': getattr(backend, 'options', {}), 'modes': results}
    finally:
        shutil.rmtree(work, ignore_errors=True)
        try:
            os.rmdir(os.path.dirname(work))              # _bench itself, when no other bench runs
        except OSError:
            pass
        for f in (src, scratch):
            try:
                os.unlink(f)
            except OSError:
                pass


def _timed(size_mib: int, t0: float) -> dict:
    sec = max(time.time() - t0, 1e-6)
    return {'seconds': round(sec, 2), 'mib_s': round(size_mib / sec, 1), 'bytes': size_mib * MiB}


def _run_mode(backend, mode, src, scratch, work, size, size_mib, intervals, writers, report, cancelled) -> dict:
    if mode == 'plain':
        dst = os.path.join(work, 'plain.bin')
        _sparse(dst, size)
        t0 = time.time()
        copier.copy_intervals(src, dst, intervals, writers=writers, progress=lambda d, t: report(d), cancelled=cancelled)
        return _timed(size_mib, t0)
    if backend.name != 'nbd':
        return {'skipped': 'needs the nbd backend'}
    if mode == 'nbd_read':
        image = os.path.join(work, 'nbd.qcow2')
        if not os.path.exists(image):                    # 'nbd' did not run before: write it, untimed
            with backend.prepare(image, size) as dev:
                copier.copy_intervals(src, dev, intervals, writers=writers, cancelled=cancelled)
        drop_cached_pages(image)
        _sparse(scratch, size)
        t0 = time.time()
        with backend.open_readonly(image) as dev:
            copier.copy_intervals(dev, scratch, intervals, writers=writers, progress=lambda d, t: report(d), cancelled=cancelled)
        return _timed(size_mib, t0)
    opts = WRITE_MODES.get(mode)
    if opts is None:
        return {'skipped': 'unknown mode'}
    image = os.path.join(work, f'{mode}.qcow2')
    t0 = time.time()
    with backend.prepare(image, size, **opts) as dev:
        copier.copy_intervals(src, dev, intervals, writers=writers, progress=lambda d, t: report(d), cancelled=cancelled)
    out = _timed(size_mib, t0)
    out['check'] = backend.check(image)
    return out
