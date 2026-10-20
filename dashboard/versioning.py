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
