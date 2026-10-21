"""
targets.py — mount a backup target (SMB or NFS share) inside the proxy for the duration of a job.

The backup service sends the target URI and credentials inside each job; they never persist on
the proxy beyond a root-only credentials file that is removed once the mount is up. Mounts are
shared between concurrent jobs on the same target and released when the last job finishes.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from urllib.parse import unquote, urlparse


@dataclass(frozen=True)
class Target:
    scheme: str           # smb | nfs
    host: str
    share: str            # smb: share name; nfs: export root path
    subpath: str          # path below the share/export, may be ''

    @property
    def key(self) -> str:
        return hashlib.sha1(f'{self.scheme}://{self.host}/{self.share}'.encode()).hexdigest()[:12]


def parse_uri(uri: str) -> Target:
    u = urlparse(uri.strip())
    scheme = u.scheme.lower()
    if scheme not in ('smb', 'nfs'):
        raise ValueError(f'unsupported target scheme: {u.scheme!r}')
    parts = [unquote(p) for p in u.path.split('/') if p]
    if not u.hostname or not parts:
        raise ValueError(f'target URI needs host and share: {uri!r}')
    if scheme == 'smb':
        return Target('smb', u.hostname, parts[0], '/'.join(parts[1:]))
    # NFS: mount the whole path (sub-directories of an export can be mounted directly)
    return Target('nfs', u.hostname, '/'.join(parts), '')


def smb_username(user: str | None) -> str | None:
    """user@domain and DOMAIN/user -> DOMAIN\\user, as the SMB client expects."""
    if not user:
        return None
    if '@' in user and '\\' not in user:
        name, dom = user.split('@', 1)
        return f'{dom}\\{name}'
    return user.replace('/', '\\')


def build_mount_cmd(t: Target, mount_point: str, credentials_file: str | None = None) -> list[str]:
    if t.scheme == 'smb':
        opts = ['vers=3.0', 'uid=0', 'gid=0', 'file_mode=0644', 'dir_mode=0755', 'noperm', 'actimeo=1']
        opts.insert(0, f'credentials={credentials_file}' if credentials_file else 'guest')
        return ['mount', '-t', 'cifs', f'//{t.host}/{t.share}', mount_point, '-o', ','.join(opts)]
    return ['mount', '-t', 'nfs', f'{t.host}:/{t.share}', mount_point, '-o', 'vers=4,hard,timeo=600']


class Mounter:
    """Reference-counted mounts under <root>/mnt/<key>. `run` is injectable for tests."""

    def __init__(self, root: str, run=subprocess.run):
        self.root = root
        self._run = run
        self._lock = threading.Lock()
        self._refs: dict[str, int] = {}

    def mount_point(self, t: Target) -> str:
        return os.path.join(self.root, 'mnt', t.key)

    def acquire(self, uri: str, username: str | None, password: str | None) -> str:
        """Mount if needed and return the directory for `uri` (mount point + subpath)."""
        t = parse_uri(uri)
        mp = self.mount_point(t)
        with self._lock:
            if self._refs.get(t.key, 0) == 0 and not os.path.ismount(mp):
                os.makedirs(mp, exist_ok=True)
                cred = None
                try:
                    if t.scheme == 'smb' and username:
                        fd, cred = tempfile.mkstemp(prefix='cred-', dir=self.root)
                        user = smb_username(username)
                        dom, _, name = user.rpartition('\\')
                        with os.fdopen(fd, 'w') as fh:
                            fh.write(f'username={name}\npassword={password or ""}\n' + (f'domain={dom}\n' if dom else ''))
                        os.chmod(cred, 0o600)
                    r = self._run(build_mount_cmd(t, mp, cred), capture_output=True, text=True)
                    if getattr(r, 'returncode', 0):
                        raise RuntimeError(f'mount failed: {(r.stderr or r.stdout or "").strip()}')
                finally:
                    if cred and os.path.exists(cred):
                        os.unlink(cred)
            self._refs[t.key] = self._refs.get(t.key, 0) + 1
        path = os.path.join(mp, t.subpath) if t.subpath else mp
        os.makedirs(path, exist_ok=True)
        return path

    def release(self, uri: str) -> None:
        t = parse_uri(uri)
        with self._lock:
            n = self._refs.get(t.key, 0) - 1
            self._refs[t.key] = max(n, 0)
            if n <= 0 and os.path.ismount(self.mount_point(t)):
                self._run(['umount', self.mount_point(t)], capture_output=True, text=True)
