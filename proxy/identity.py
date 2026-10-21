"""
identity.py — who is this proxy? Used by the backup service to find the proxy's own VM on the
cluster (MAC addresses match VirDomain.netDevs) and to learn what the proxy can do.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess

VERSION = '0.1.0'
_SKIP_PREFIXES = ('lo', 'docker', 'veth', 'br-', 'virbr', 'cni', 'flannel', 'tun', 'tap', 'wg')


def host_macs(host_net_dir: str) -> list[dict]:
    """[{ifname, mac}] for physical-looking interfaces found under a bind-mounted /sys/class/net."""
    out = []
    try:
        names = sorted(os.listdir(host_net_dir))
    except OSError:
        return out
    for n in names:
        if n.startswith(_SKIP_PREFIXES):
            continue
        try:
            with open(os.path.join(host_net_dir, n, 'address')) as fh:
                mac = fh.read().strip().upper()
        except OSError:
            continue
        if mac and mac != '00:00:00:00:00:00':
            out.append({'ifname': n, 'mac': mac})
    return out


def _tool_version(cmd: list[str]) -> str | None:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr) else None
    except (OSError, subprocess.SubprocessError, IndexError):
        return None


def identity(cfg, backend_name: str, nbd_free: int | None) -> dict:
    return {
        'service': 'hcm-proxy',
        'version': VERSION,
        'hostname': socket.gethostname(),
        'macs': host_macs(cfg.host_net),
        'backend': backend_name,
        'capabilities': {
            'nbd_slots_free': nbd_free,
            'qemu_img': _tool_version(['qemu-img', '--version']),
            'mount_cifs': shutil.which('mount.cifs') is not None,
            'mount_nfs': shutil.which('mount.nfs') is not None or shutil.which('mount.nfs4') is not None,
            'jobs': ['backup', 'flatten', 'verify', 'sweep', 'prune', 'delete_chain', 'stage'],
            'mounts': True,
            'filesystems': ['ext4', 'ext3', 'ext2', 'xfs', 'btrfs', 'vfat', 'ntfs', 'exfat', 'lvm'],
        },
    }
