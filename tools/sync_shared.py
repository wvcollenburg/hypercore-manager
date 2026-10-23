#!/usr/bin/env python3
"""Copy the canonical modules in shared/ into every service directory.

Each container has its own Docker build context, so modules shared between
services are duplicated on disk. shared/ holds the single source of truth; this
script fans it out, and tests/test_shared_copies.py fails CI when a copy drifts.

Usage:  python3 tools/sync_shared.py          # copy
        python3 tools/sync_shared.py --check  # exit 1 if any copy differs
"""
from __future__ import annotations

import filecmp
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHARED = ROOT / 'shared'

# module name -> service directories that embed it
TARGETS = {
    'hcm_crypto.py':       ['Collector', 'Balancer', 'dashboard', 'sentinel', 'backup'],
    'cluster_registry.py': ['Collector', 'Balancer', 'dashboard', 'sentinel', 'backup'],
    'config_db.py':        ['Collector', 'Balancer', 'dashboard'],
    'hypercore_tasks.py':  ['Collector', 'Balancer', 'dashboard'],
    'hypercore_session.py': ['Collector', 'Balancer', 'sentinel', 'backup'],
}


def pairs():
    for name, dirs in TARGETS.items():
        src = SHARED / name
        if not src.exists():
            continue
        for d in dirs:
            yield src, ROOT / d / name


def check() -> list[str]:
    return [str(dst.relative_to(ROOT)) for src, dst in pairs()
            if not dst.exists() or not filecmp.cmp(src, dst, shallow=False)]


def main() -> int:
    if '--check' in sys.argv:
        stale = check()
        for s in stale:
            print(f'out of date: {s}')
        return 1 if stale else 0
    for src, dst in pairs():
        shutil.copyfile(src, dst)
        print(f'{src.relative_to(ROOT)} -> {dst.relative_to(ROOT)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
