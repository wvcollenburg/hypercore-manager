"""
support.py — the proxy's support bundle: one zip with what is needed to see why a backup, restore or
file-level mount went wrong, for admins who have no login on the proxy VM.

  logs/proxy.log*     the proxy's own log (log.py keeps a rotating copy on the data volume)
  proxy.json          identity, version and settings (tokens and passwords removed)
  jobs.json           recent jobs with their result or error (credentials removed)
  mounts.json         open file-level mounts, with the partitions that were left out and why
  system/*.txt        kernel messages, block devices, loop and device-mapper devices, mounts, disk
                      space, memory and processes, each as its command printed it (or its error)

Everything that touches the system goes through `run` (injectable), so it is unit-tested without root.
"""
from __future__ import annotations

import dataclasses
import io
import json
import os
import subprocess
import time
import zipfile

REDACTED = '***'
_SECRET_WORDS = ('password', 'token', 'secret', 'credential', 'passphrase', 'private_key')

# (file in the bundle, command); a missing tool or a failure is written into the file instead
COMMANDS = [
    ('system/dmesg.txt', ['dmesg', '-T']),
    ('system/uname.txt', ['uname', '-a']),
    ('system/lsblk.txt', ['lsblk', '-o', 'NAME,SIZE,TYPE,FSTYPE,RO,MOUNTPOINT']),
    ('system/losetup.txt', ['losetup', '-l']),
    ('system/dmsetup.txt', ['dmsetup', 'table']),
    ('system/df.txt', ['df', '-h']),
    ('system/free.txt', ['free', '-m']),
    ('system/ps.txt', ['ps', 'aux']),
]
FILES = [('system/mounts.txt', '/proc/mounts'), ('system/partitions.txt', '/proc/partitions')]


def redact(value):
    """A copy with every value under a secret-looking key replaced."""
    if isinstance(value, dict):
        return {k: (REDACTED if any(w in str(k).lower() for w in _SECRET_WORDS) and v not in (None, '') else redact(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def _command(cmd: list[str], run, timeout: int = 20) -> str:
    try:
        r = run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return f'$ {" ".join(cmd)}\n(not run: {e})\n'
    out = f'$ {" ".join(cmd)}\n{r.stdout or ""}'
    if r.returncode or r.stderr:
        out += f'\n(exit {r.returncode}) {r.stderr or ""}'
    return out


def _json(value) -> str:
    return json.dumps(value, indent=2, default=str, sort_keys=True)


def bundle(cfg, ident: dict, jobs: list[dict], mounts: list[dict], run=subprocess.run,
           log_dir: str | None = None) -> bytes:
    """The zip, as bytes (a few MiB at most: the log files are capped by log.py)."""
    log_dir = log_dir or os.path.join(cfg.data_dir, 'logs')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        settings = dataclasses.asdict(cfg) if dataclasses.is_dataclass(cfg) else dict(vars(cfg))
        z.writestr('proxy.json', _json(redact({'identity': ident, 'settings': settings, 'created': time.strftime('%Y-%m-%dT%H:%M:%S%z')})))
        z.writestr('jobs.json', _json(redact(jobs)))
        z.writestr('mounts.json', _json(redact(mounts)))
        try:
            names = sorted(n for n in os.listdir(log_dir) if n.startswith('proxy.log'))
        except OSError:
            names = []
        for n in names:
            try:
                z.write(os.path.join(log_dir, n), f'logs/{n}')
            except OSError as e:
                z.writestr(f'logs/{n}.error.txt', str(e))
        if not names:
            z.writestr('logs/README.txt', f'no log file in {log_dir} (proxy older than 0.5.1, or the volume is not writable)\n')
        for name, cmd in COMMANDS:
            z.writestr(name, _command(cmd, run))
        for name, path in FILES:
            try:
                with open(path, encoding='utf-8', errors='replace') as fh:
                    z.writestr(name, fh.read())
            except OSError as e:
                z.writestr(name, f'(not read: {e})\n')
    return buf.getvalue()
