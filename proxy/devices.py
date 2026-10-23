"""
devices.py — hot-added block devices on the proxy VM.

HyperCore attaches a cloned snapshot disk to the proxy VM as a virtio device whose serial is the
first eight hex characters of the new block device's uuid, so the kernel exposes it as
/dev/disk/by-id/virtio-<uuid[:8]>. Nothing here talks to HyperCore; the backup service does the
cloning and tells the proxy which uuid to wait for.
"""
from __future__ import annotations

import os
import time


def by_id_path(device_uuid: str, dev_dir: str = '/dev') -> str:
    return os.path.join(dev_dir, 'disk', 'by-id', f'virtio-{device_uuid[:8]}')


def wait_for_device(device_uuid: str, timeout: float, dev_dir: str = '/dev', present: bool = True,
                    poll: float = 1.0) -> str:
    """Block until the device appears (or disappears). Returns its path; raises TimeoutError."""
    path = by_id_path(device_uuid, dev_dir)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(path) == present:
            return path
        time.sleep(poll)
    raise TimeoutError(f'{path} did not {"appear" if present else "disappear"} within {timeout:.0f}s')


def device_size(path: str) -> int:
    """Size in bytes of a block device or a regular file."""
    fd = os.open(path, os.O_RDONLY)
    try:
        return os.lseek(fd, 0, os.SEEK_END)
    finally:
        os.close(fd)


def is_read_only(path: str) -> bool | None:
    """True/False from sysfs for a block device, None when unknown (regular file, no sysfs)."""
    try:
        name = os.path.basename(os.path.realpath(path))
        with open(f'/sys/block/{name}/ro') as fh:
            return fh.read().strip() == '1'
    except OSError:
        return None
