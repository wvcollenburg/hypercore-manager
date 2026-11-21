"""
backup_scope.py — which backup rows a user sees (plans/PLAN-backup.md: backups belong to the folder).

Schedules span the clusters of a folder, so the Backup tab lists plans, restore points, runs, restores
and file-recovery mounts of every cluster in the selected cluster's folder. Each row is judged by its
own cluster: it shows when the user holds the right on that cluster, and, where they hold it on some
VMs only, when its VM is one of them. Pure functions, so the rules are tested without the app.
"""
from __future__ import annotations

import re

import rbac


def folder_scopes(acc: rbac.Access, perm: str, cid: str) -> dict:
    """{cluster_id: scope} for the clusters of cid's folder (only cid itself when it is in none) on which
    the user holds `perm` on the whole cluster ('all') or on some of its VMs (a scope to narrow by)."""
    folder = acc.tree.cluster_folder.get(cid)
    cids = [c for c, f in acc.tree.cluster_folder.items() if f == folder] if folder else [cid]
    return {c: sc for c, sc in acc.scope_for(perm, cids).items() if sc}


def narrow(sub: str, data, scopes: dict, cid: str, vm_index_rows):
    """A folder-wide list answer narrowed row by row. vm_index_rows(cluster_id) gives the VMs with their
    tags, read only for a cluster where the user holds tag grants. Rows without a cluster belong to cid."""
    if data is None:
        return data
    tag_cache: dict = {}

    def tags(c):
        if c not in tag_cache:
            tag_cache[c] = {v.get('uuid'): v.get('tags') or [] for v in vm_index_rows(c)}
        return tag_cache[c]

    def keep(row, key='vm_uuid', cluster_level=False):
        c = str(row.get('cluster_id') or cid)
        sc = scopes.get(c)
        if not sc:
            return False
        if sc == 'all':
            return True
        if cluster_level:                    # imports belong to a cluster, not to a VM
            return False
        u = row.get(key)
        return rbac.vm_in_scope(sc, u, tags(c).get(u, []) if sc.get('tags') else [])

    def rows(xs, key='vm_uuid', cluster_level=False):
        return [x for x in (xs or []) if isinstance(x, dict) and keep(x, key, cluster_level)]

    if sub in ('plans', 'chains', 'restores', 'flr/mounts'):
        return rows(data)
    if sub in ('runs', 'runs/status'):
        return dict(data, runs=rows(data.get('runs')), restores=rows(data.get('restores')),
                    imports=rows(data.get('imports'), cluster_level=True))
    if sub == 'policies':
        whole = scopes.get(cid) == 'all'
        pols = []
        for p in data.get('policies') or []:
            members = p.get('members') or []
            mine = rows(members)
            # an empty schedule shows to whoever holds the right on the whole selected cluster (its maker)
            if mine or (not members and whole):
                pols.append(dict(p, members=mine))
        return dict(data, policies=pols)
    if re.fullmatch(r'policies/\d+/night', sub):
        items = rows(data.get('items'))
        return dict(data, items=items) if items else {'items': []}
    return data
