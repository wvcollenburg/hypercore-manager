"""
media.py — ISOs and disk images on a cluster, and who may see, use and manage them (William, 2026-11-22).

HyperCore keeps media per cluster and knows nothing of tenants. HCM gives each item an owner layer of the
tenant tree (config.media.owner_node; NULL is the top: the installation's own administrators):

  - an upload belongs to the uploader's highest layer that holds media.manage over that cluster;
  - an item HCM did not upload (it was on the cluster before, or came in another way) belongs to the top;
  - an item is seen by whoever holds a use permission (media.manage, vm.create, vm.edit) from a grant at its
    owner layer, or at a layer it is assigned to, and below those within the same tenant: a customer's or a
    folder's upload is seen in its folders, but an MSP's upload is the MSP's own until it assigns it to one of
    its customers (the customers below an MSP are separate tenants). The layers above do not see an item (an
    MSP does not see its customers' uploads), except the top-level administrators, who see everything;
  - the owner layer (or the top) may assign it down the cluster's own branch, so an MSP can only hand it to
    its own customer (or that customer's folder), and may delete it.

A grant's layer is where it sits: root, the node of a node grant, or the folder of the cluster that a
cluster, tag or VM grant is on. Everything here is pure: the database and HyperCore are the callers' job.
"""
from __future__ import annotations

import re

import rbac

TOP = None                       # the owner value for the top (config.media.owner_node NULL)
_UNFILED = object()              # the layer of a cluster grant on a cluster outside the tree: within nothing
USE_PERMS = ('media.manage', 'vm.create', 'vm.edit')
KINDS = ('iso', 'disk')
DISK_SUFFIXES = ('.qcow2', '.vmdk', '.vhd', '.vhdx', '.img', '.raw')
NAME_MAX = 200
_NAME_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9 ._+()\-]*')


def check_name(kind: str, name: str) -> str:
    """The file name an upload gets on the cluster, or ValueError: no path, no control characters, the
    right suffix for its kind (HyperCore wants .iso for an ISO; disk images by their format)."""
    if kind not in KINDS:
        raise ValueError("kind must be 'iso' or 'disk'")
    name = (name or '').strip()
    if not name or len(name) > NAME_MAX or not _NAME_RE.fullmatch(name):
        raise ValueError(f'a file name of letters, digits, spaces and . _ + ( ) -, at most {NAME_MAX} characters')
    low = name.lower()
    if kind == 'iso' and not low.endswith('.iso'):
        raise ValueError('an ISO file name ends in .iso')
    if kind == 'disk' and not low.endswith(DISK_SUFFIXES):
        raise ValueError('a disk image is a ' + ', '.join(DISK_SUFFIXES) + ' file')
    return name


def _covers(acc: rbac.Access, g: rbac.Grant, cid: str) -> bool:
    if g.kind in ('tag', 'vm'):
        return g.cluster_id == cid
    return acc._covers_cluster(g, cid)


def _layer(acc: rbac.Access, g: rbac.Grant):
    if g.kind == 'root':
        return TOP
    if g.kind == 'node':
        return g.node_id
    folder = acc.tree.cluster_folder.get(g.cluster_id)
    return folder if folder else _UNFILED


def layers(acc: rbac.Access, cid: str, perms) -> set:
    """The layers of the user's grants on this cluster that carry one of `perms`."""
    want = set(perms)
    return {_layer(acc, g) for g in acc.grants if want & g.effective() and _covers(acc, g, cid)}


def _within(acc: rbac.Access, layer, top) -> bool:
    """Does an item owned by (or assigned to) `top` reach a grant at `layer`? The top and an MSP reach only
    their own layer (what is below them are other tenants); a customer or folder reaches its whole subtree."""
    if layer is _UNFILED:
        return False
    if top is TOP:
        return layer is TOP
    if acc.tree.level.get(top) == 'msp':
        return layer == top
    return layer is not TOP and top in acc.tree.ancestors(layer)


def is_top(acc: rbac.Access, perms=USE_PERMS) -> bool:
    return acc.superuser or any(g.kind == 'root' and set(perms) & g.effective() for g in acc.grants)


def may_see(acc: rbac.Access, item: dict, cid: str) -> bool:
    """item: {'owner': node id or TOP, 'assigned': [node ids]}."""
    if is_top(acc):
        return True
    mine = layers(acc, cid, USE_PERMS)
    tops = [item.get('owner')] + list(item.get('assigned') or [])
    return any(_within(acc, layer, top) for layer in mine for top in tops)


def may_manage(acc: rbac.Access, item: dict, cid: str) -> bool:
    """Assign and delete: the top, or media.manage held at the owner's own layer."""
    if is_top(acc, ('media.manage',)):
        return True
    return item.get('owner') is not TOP and item.get('owner') in layers(acc, cid, ('media.manage',))


def upload_owner(acc: rbac.Access, cid: str):
    """(True, owner) for someone who may upload to this cluster: their highest media.manage layer over it;
    (False, None) for someone who may not."""
    if is_top(acc, ('media.manage',)):
        return True, TOP
    mine = [layer for layer in layers(acc, cid, ('media.manage',)) if layer is not _UNFILED]
    if not mine:
        return False, None
    return True, min(mine, key=lambda n: len(acc.tree.ancestors(n)))


def assign_targets(acc: rbac.Access, item: dict, cid: str) -> list[str]:
    """The layers this item may be assigned to: down the cluster's own branch, below its owner, top first. Only
    where it opens the item to someone new: below a customer or folder its own folders see it already."""
    if not may_manage(acc, item, cid):
        return []
    folder = acc.tree.cluster_folder.get(cid)
    branch = list(reversed(acc.tree.ancestors(folder))) if folder else []
    owner = item.get('owner')
    if owner is TOP:
        return branch
    if acc.tree.level.get(owner) != 'msp':
        return []
    return branch[branch.index(owner) + 1:] if owner in branch else []


def mounted_isos(vm: dict) -> set[str]:
    """The ISOs inserted in a VM's CD-ROM drives, by HyperCore's path (`scribe/<uuid>` style) and name."""
    out = set()
    for d in vm.get('blockDevs') or []:
        if d.get('type') == 'IDE_CDROM' and d.get('path'):
            out.add(str(d['path']))
            out.add(str(d['path']).rsplit('/', 1)[-1])
    return out
