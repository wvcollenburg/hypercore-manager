"""
versioning.py — semver helpers shared by the in-app updater.

Kept free of Flask/DB imports so the logic is unit-testable on its own.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

SEMVER_RE = re.compile(r'^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$')


def semver_key(v: str) -> Optional[tuple]:
    """Sort key for semver-like version strings ('1.2.3', 'v1.2.3', '1.2.3-rc.1').

    Pre-release suffixes sort *below* the matching release per the SemVer spec,
    so v0.9.0-rc.1 < v0.9.0. Returns None for unparseable input.
    """
    if v is None:
        return None
    m = SEMVER_RE.match(v.strip())
    if not m:
        return None
    major, minor, patch, pre = m.groups()
    pre_key = (0, pre) if pre else (1, '')
    return (int(major), int(minor), int(patch), pre_key)


def latest_semver_tag(tags: Iterable[str]) -> str:
    """Return the highest semver-like tag from *tags*, or '' if none parse."""
    valid = [(t.strip(), semver_key(t)) for t in tags if t and t.strip()]
    valid = [(t, k) for t, k in valid if k is not None]
    if not valid:
        return ''
    valid.sort(key=lambda x: x[1])
    return valid[-1][0]


def is_newer(remote: str, current: str) -> bool:
    """True when *remote* parses as a strictly higher version than *current*."""
    rk, ck = semver_key(remote or ''), semver_key(current or '')
    return bool(rk and ck and rk > ck)


def origin_to_web_url(origin: str) -> str:
    """The browsable URL of a git remote: 'git@github.com:org/repo.git' and
    'ssh://git@github.com/org/repo.git' become 'https://github.com/org/repo'; an https
    remote loses its '.git' and any credentials. '' when the remote is empty."""
    o = (origin or '').strip()
    if not o:
        return ''
    m = re.match(r'^(?:ssh://)?(?:[\w.-]+@)?([\w.-]+)[:/](.+?)(?:\.git)?/?$', o) if '://' not in o or o.startswith('ssh://') else None
    if m:
        return f'https://{m.group(1)}/{m.group(2)}'
    m = re.match(r'^(https?)://(?:[^@/]+@)?([^/]+)/(.+?)(?:\.git)?/?$', o)
    if m:
        return f'{m.group(1)}://{m.group(2)}/{m.group(3)}'
    return o
