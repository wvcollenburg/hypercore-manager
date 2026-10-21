"""Naming shared with the proxy's manifest module (kept in sync by hand; tested on both sides)."""
from __future__ import annotations


def chain_dirname(vm_name: str, vm_uuid: str, started: str | None = None) -> str:
    """Directory of one chain. `started` (YYYYMMDD-HHMMSS) makes it unique per chain: every full
    starts a new chain of the same VM, and the old one keeps its directory until retention
    removes it."""
    safe = ''.join(c if c.isalnum() or c in '._-' else '_' for c in vm_name)[:64] or 'vm'
    return f'{safe}_{vm_uuid[:8]}{"_" + started if started else ""}.chain'


def point_dirname(kind: str, seq: int) -> str:
    return 'base' if kind == 'full' and seq == 0 else f'{"delta" if kind == "delta" else "full"}-{seq:04d}'
