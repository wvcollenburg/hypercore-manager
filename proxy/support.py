"""
support.py — the proxy's support bundle: one zip with what is needed to see why a backup, restore or
file-level mount went wrong, for admins who have no login on the proxy VM.

  logs/proxy.log*     the proxy's own log (log.py keeps a rotating copy on the data volume)
  proxy.json          identity, version and settings (tokens and passwords removed)
  jobs.json           recent jobs with their result or error (credentials removed)
  mounts.json         open file-level mounts, with the partitions that were left out and why
  system/*.txt        kernel messages, block devices, loop and device-mapper devices, mounts, disk
                      space, memory and processes, each as its command printed it (or its error)
  system/processes.txt  every process from /proc with a summary (states, zombies by name and parent)
  system/limits.txt   the kernel, rlimit and cgroup limits that can refuse a new process or thread

Everything that touches the system goes through `run` (injectable), so it is unit-tested without root.
"""
from __future__ import annotations

import dataclasses
import io
import json
import os
import socket
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


def _read(path: str) -> str | None:
    try:
        with open(path, encoding='utf-8', errors='replace') as fh:
            return fh.read().strip()
    except OSError:
        return None


def process_table(proc: str = '/proc') -> str:
    """Every process the container sees, read from /proc (works without procps): a summary first
    (count, threads, states, zombies by name and parent), then one line per process. Written for the
    0.5.4 thread exhaustion, where qemu could not start a thread and nothing showed what held them."""
    rows = []
    for pid in sorted((p for p in os.listdir(proc) if p.isdigit()), key=int):
        stat = _read(f'{proc}/{pid}/stat')
        if not stat or ')' not in stat:
            continue                                  # gone between listdir and read
        comm = stat[stat.index('(') + 1:stat.rindex(')')]
        f = stat[stat.rindex(')') + 2:].split()
        try:
            state, ppid, threads, rss = f[0], int(f[1]), int(f[17]), int(f[21])
        except (IndexError, ValueError):
            continue
        cmd = (_read(f'{proc}/{pid}/cmdline') or '').replace('\0', ' ').strip()
        rows.append((int(pid), ppid, state, threads, rss * 4096 // (1 << 20), comm, cmd or f'[{comm}]'))
    states, zombies = {}, {}
    for r in rows:
        states[r[2]] = states.get(r[2], 0) + 1
        if r[2] == 'Z':
            key = f'{r[5]} (parent {r[1]})'
            zombies[key] = zombies.get(key, 0) + 1
    names = {}
    for r in rows:
        names[r[5]] = names.get(r[5], 0) + 1
    out = [f'processes: {len(rows)}   threads: {sum(r[3] for r in rows)}',
           'states: ' + ', '.join(f'{s}={n}' for s, n in sorted(states.items())),
           'zombies: ' + (', '.join(f'{k} x{n}' for k, n in sorted(zombies.items(), key=lambda kv: -kv[1])) or 'none'),
           'by name: ' + ', '.join(f'{k} x{n}' for k, n in sorted(names.items(), key=lambda kv: -kv[1])[:15]),
           '', f'{"PID":>7} {"PPID":>7} S {"THR":>4} {"RSS_MiB":>7}  COMMAND']
    out += [f'{r[0]:>7} {r[1]:>7} {r[2]} {r[3]:>4} {r[4]:>7}  {r[6][:200]}' for r in rows]
    return '\n'.join(out) + '\n'


_CGROUP_FILES = ('pids.current', 'pids.max', 'pids.peak', 'memory.current', 'memory.max', 'memory.peak')


def _cgroup_lines(path: str) -> list[str]:
    return [f'  {n} = {v}' for n in _CGROUP_FILES if (v := _read(os.path.join(path, n))) is not None]


def task_limits(proc: str = '/proc', cgroup: str = '/sys/fs/cgroup', host_cgroup: str = '/host/sys/fs/cgroup',
                container_id: str | None = None) -> str:
    """Every limit that can refuse a new process or thread with EAGAIN: the kernel's, the rlimit, the
    container's own cgroup and (through the host's read-only /sys) each cgroup above it."""
    out = ['kernel:']
    for k in ('threads-max', 'pid_max'):
        out.append(f'  {k} = {_read(f"{proc}/sys/kernel/{k}")}')
    loadavg = _read(f'{proc}/loadavg')
    if loadavg:
        out.append(f'  loadavg = {loadavg}   (4th field: running/total tasks on the host)')
    limits = _read(f'{proc}/self/limits') or ''
    out += ['rlimits:'] + [f'  {l}' for l in limits.splitlines() if l.startswith(('Max processes', 'Max stack size', 'Max address space'))]
    out += ['container cgroup (own namespace):'] + (_cgroup_lines(cgroup) or ['  (not readable)'])
    cid = container_id or socket.gethostname()
    found = None
    if cid and os.path.isdir(host_cgroup):
        for root, dirs, _files in os.walk(host_cgroup):
            if root[len(host_cgroup):].count(os.sep) >= 4:
                dirs[:] = []
                continue
            hit = next((d for d in dirs if cid in d), None)
            if hit:
                found = os.path.join(root, hit)
                break
    if found:
        path = found
        while True:
            out.append(f'host cgroup {path[len(host_cgroup):] or "/"}:')
            out += _cgroup_lines(path) or ['  (no limits here)']
            if os.path.normpath(path) == os.path.normpath(host_cgroup):
                break
            path = os.path.dirname(path)
    else:
        out.append(f'host cgroup: no directory naming {cid!r} under {host_cgroup}')
    return '\n'.join(out) + '\n'


def _json(value) -> str:
    return json.dumps(value, indent=2, default=str, sort_keys=True)


def bundle(cfg, ident: dict, jobs: list[dict], mounts: list[dict], run=subprocess.run,
           log_dir: str | None = None, proc: str = '/proc', cgroup: str = '/sys/fs/cgroup',
           host_cgroup: str = '/host/sys/fs/cgroup', container_id: str | None = None) -> bytes:
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
        for name, fn in (('system/processes.txt', lambda: process_table(proc)),
                         ('system/limits.txt', lambda: task_limits(proc, cgroup, host_cgroup, container_id))):
            try:
                z.writestr(name, fn())
            except Exception as e:              # a diagnostic never breaks the bundle
                z.writestr(name, f'(not collected: {e})\n')
        for name, path in FILES:
            try:
                with open(path, encoding='utf-8', errors='replace') as fh:
                    z.writestr(name, fh.read())
            except OSError as e:
                z.writestr(name, f'(not read: {e})\n')
    return buf.getvalue()
