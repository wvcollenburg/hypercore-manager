"""Naming shared with the proxy's manifest module (kept in sync by hand; tested on both sides)."""
from __future__ import annotations


def chain_dirname(vm_name: str, vm_uuid: str) -> str:
    safe = ''.join(c if c.isalnum() or c in '._-' else '_' for c in vm_name)[:64] or 'vm'
    return f'{safe}_{vm_uuid[:8]}.chain'


def point_dirname(kind: str, seq: int) -> str:
    return 'base' if kind == 'full' and seq == 0 else f'{"delta" if kind == "delta" else "full"}-{seq:04d}'
