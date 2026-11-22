"""
rbac.py — who may do what, where (plans/PLAN-roles.md).

Pure logic, no Flask and no database: the permission catalogue, the built-in roles, the tenant tree
and the resolution of a user's assignments into an `Access` object the routes ask questions of.
Loading rows from PostgreSQL lives in access_db.py; enforcement (the route decorator, the forward
tables) in app.py. Keeping this module pure is what lets the rules be unit-tested exhaustively.

Model in one paragraph: the tree is root > MSP > customer > folder > cluster. A role is a set of
permissions. An assignment gives a user (directly or through a group) a role on a scope: the whole
install (root), a tree node, a cluster, a VM tag on a cluster, or one VM. Rights flow downwards. A
permission has a *level*: 'vm' permissions may be granted down to a single VM; 'cluster' permissions
need the whole cluster covered (a tag or VM grant never carries them); 'node' permissions act on the
tree (users, roles, shares, clusters) and need a node or root grant; 'root' permissions only count
when granted on the whole install.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------------------------------
# Permission catalogue. Never rename a key: roles in the database refer to them by name.
# level: vm | cluster | node | root (see the module docstring). implies: held together with it.
# ---------------------------------------------------------------------------------------------------
PERMISSIONS: dict[str, dict] = {
    # overview of a cluster: cards, badges, nodes, drives, charts, Foresight, reports, settings (read)
    'cluster.view':           {'area': 'overview', 'level': 'cluster', 'implies': ['vm.view']},
    'cluster.alerts.mute':    {'area': 'overview', 'level': 'cluster', 'implies': ['cluster.view']},
    # VMs
    'vm.view':                {'area': 'vms', 'level': 'vm', 'implies': []},
    'vm.console':             {'area': 'vms', 'level': 'vm', 'implies': ['vm.view']},
    'vm.power':               {'area': 'vms', 'level': 'vm', 'implies': ['vm.view']},
    'vm.edit':                {'area': 'vms', 'level': 'vm', 'implies': ['vm.view']},
    'vm.snapshot':            {'area': 'vms', 'level': 'vm', 'implies': ['vm.view']},
    'vm.clone':               {'area': 'vms', 'level': 'vm', 'implies': ['vm.view']},
    'vm.delete':              {'area': 'vms', 'level': 'vm', 'implies': ['vm.view']},
    'vm.tags.edit':           {'area': 'vms', 'level': 'vm', 'implies': ['vm.view']},
    'vm.create':              {'area': 'vms', 'level': 'cluster', 'implies': ['vm.view']},
    # ISOs and disk images on a cluster: upload, assign down the tree, delete (dashboard/media.py)
    'media.manage':           {'area': 'vms', 'level': 'cluster', 'implies': ['vm.view']},
    # backup
    'backup.view':            {'area': 'backup', 'level': 'vm', 'implies': ['vm.view']},
    'backup.schedule':        {'area': 'backup', 'level': 'vm', 'implies': ['backup.view']},
    'backup.run':             {'area': 'backup', 'level': 'vm', 'implies': ['backup.view']},
    'backup.browse':          {'area': 'backup', 'level': 'vm', 'implies': ['backup.view']},
    'backup.download':        {'area': 'backup', 'level': 'vm', 'implies': ['backup.browse']},
    'backup.restore.file':    {'area': 'backup', 'level': 'vm', 'implies': ['backup.browse']},
    'backup.restore.full':    {'area': 'backup', 'level': 'vm', 'implies': ['backup.view']},
    'backup.verify':          {'area': 'backup', 'level': 'vm', 'implies': ['backup.view']},
    'backup.delete':          {'area': 'backup', 'level': 'vm', 'implies': ['backup.view']},
    'backup.import':          {'area': 'backup', 'level': 'cluster', 'implies': ['backup.view']},
    # backup infrastructure
    'backup.proxies.manage':  {'area': 'backup_infra', 'level': 'cluster', 'implies': ['cluster.view']},
    'backup.targets.manage':  {'area': 'backup_infra', 'level': 'node', 'implies': []},
    # open console sessions check (#2)
    'console_check.manage':   {'area': 'console_check', 'level': 'cluster', 'implies': ['cluster.view']},
    'console.audit':          {'area': 'console_check', 'level': 'vm', 'implies': ['vm.view']},
    # one-time console access for outsiders
    'guest_console.request':  {'area': 'guest_console', 'level': 'vm', 'implies': ['vm.view']},
    'guest_console.grant':    {'area': 'guest_console', 'level': 'vm', 'implies': ['guest_console.request']},
    # clusters
    'cluster.settings':       {'area': 'clusters', 'level': 'cluster', 'implies': ['cluster.view']},
    'cluster.manage':         {'area': 'clusters', 'level': 'node', 'implies': []},
    # tree and people
    'tree.manage':            {'area': 'people', 'level': 'node', 'implies': []},
    'users.manage':           {'area': 'people', 'level': 'node', 'implies': []},
    'groups.manage':          {'area': 'people', 'level': 'node', 'implies': []},
    'roles.manage':           {'area': 'people', 'level': 'node', 'implies': []},
    'audit.view':             {'area': 'people', 'level': 'node', 'implies': []},
    # the installation itself: updater, global defaults, modules. Root only, never below.
    'install.manage':         {'area': 'install', 'level': 'root', 'implies': []},
}

AREAS = ('overview', 'vms', 'backup', 'backup_infra', 'console_check', 'guest_console', 'clusters', 'people', 'install')
LEVELS = ('vm', 'cluster', 'node', 'root')
VM_LEVEL = frozenset(k for k, v in PERMISSIONS.items() if v['level'] == 'vm')
CLUSTER_LEVEL = frozenset(k for k, v in PERMISSIONS.items() if v['level'] == 'cluster')
NODE_LEVEL = frozenset(k for k, v in PERMISSIONS.items() if v['level'] == 'node')
ROOT_LEVEL = frozenset(k for k, v in PERMISSIONS.items() if v['level'] == 'root')
ALL = frozenset(PERMISSIONS)


def expand(perms) -> frozenset:
    """The permissions plus everything they imply (transitively). Unknown names are dropped: a role
    saved by a newer version keeps working on an older one, minus what that version cannot know."""
    out: set[str] = set()
    todo = [p for p in perms if p in PERMISSIONS]
    while todo:
        p = todo.pop()
        if p in out:
            continue
        out.add(p)
        todo.extend(PERMISSIONS[p]['implies'])
    return frozenset(out)


# ---------------------------------------------------------------------------------------------------
# Built-in roles: read-only templates, defined at root. Their permissions live in code, so a new
# permission reaches them with the update; custom roles are copies and do not change by themselves.
# ---------------------------------------------------------------------------------------------------
BUILTIN_ROLES: dict[str, dict] = {
    'admin': {'name': 'Admin', 'perms': ALL},
    'engineer': {'name': 'Engineer', 'perms': expand([
        'cluster.view', 'cluster.alerts.mute', 'vm.view', 'vm.console', 'vm.power', 'vm.edit', 'vm.snapshot',
        'vm.clone', 'vm.delete', 'vm.tags.edit', 'vm.create', 'media.manage', 'backup.view', 'guest_console.request',
        'console.audit'])},
    'app_manager': {'name': 'App Manager', 'perms': expand([
        'cluster.view', 'vm.view', 'vm.console', 'guest_console.request'])},
    'backup_manager': {'name': 'Backup Manager', 'perms': expand([
        'cluster.view', 'vm.view', 'backup.view', 'backup.schedule', 'backup.run', 'backup.browse',
        'backup.download', 'backup.restore.file', 'backup.restore.full', 'backup.verify', 'backup.delete',
        'backup.import'])},
    'viewer': {'name': 'Viewer', 'perms': expand(['cluster.view', 'vm.view', 'backup.view'])},
}

# what a pre-RBAC non-admin user could do on the clusters of their folders (used only until the
# upgrade modal has given them real assignments; see access_db.legacy_grants)
LEGACY_USER = expand([
    'cluster.view', 'cluster.alerts.mute', 'vm.view', 'backup.view', 'backup.schedule', 'backup.run',
    'backup.browse', 'backup.download', 'backup.restore.file', 'backup.restore.full', 'backup.verify',
    'backup.delete', 'backup.import'])


def role_perms(builtin_key: str | None, stored) -> frozenset:
    """Effective permissions of a role row: the code's set for a built-in, the stored set otherwise."""
    if builtin_key and builtin_key in BUILTIN_ROLES:
        return BUILTIN_ROLES[builtin_key]['perms']
    return expand(stored or ())


# ---------------------------------------------------------------------------------------------------
# Tree
# ---------------------------------------------------------------------------------------------------
NODE_LEVELS = ('msp', 'customer', 'folder')
PARENT_LEVEL = {'msp': None, 'customer': 'msp', 'folder': 'customer'}


@dataclass
class Tree:
    """The tenant tree: node -> parent and level, cluster -> folder. Root is implicit (None)."""
    parent: dict[str, str | None] = field(default_factory=dict)
    level: dict[str, str] = field(default_factory=dict)
    name: dict[str, str] = field(default_factory=dict)
    cluster_folder: dict[str, str | None] = field(default_factory=dict)

    def ancestors(self, node_id: str | None) -> list[str]:
        """The node and every node above it, nearest first (root itself is not a node)."""
        out, seen = [], set()
        while node_id and node_id not in seen and node_id in self.parent:
            out.append(node_id)
            seen.add(node_id)
            node_id = self.parent[node_id]
        return out

    def is_within(self, node_id: str | None, top: str | None) -> bool:
        """True when node_id lies at or below `top` (None = root, which contains everything)."""
        if top is None:
            return True
        return top in self.ancestors(node_id)

    def cluster_within(self, cluster_id: str, top: str | None) -> bool:
        if top is None:
            return True
        return self.is_within(self.cluster_folder.get(cluster_id), top)

    def children(self, node_id: str | None) -> list[str]:
        return [n for n, p in self.parent.items() if p == node_id]

    def subtree(self, node_id: str | None) -> set[str]:
        """Every node at or below node_id (root: every node)."""
        if node_id is None:
            return set(self.parent)
        return {n for n in self.parent if self.is_within(n, node_id)}

    def path(self, node_id: str | None) -> list[str]:
        return list(reversed([self.name.get(n, n) for n in self.ancestors(node_id)]))


# ---------------------------------------------------------------------------------------------------
# Grants and access
# ---------------------------------------------------------------------------------------------------
SCOPE_KINDS = ('root', 'node', 'cluster', 'tag', 'vm')


@dataclass(frozen=True)
class Grant:
    perms: frozenset
    kind: str                          # root | node | cluster | tag | vm
    node_id: str | None = None
    cluster_id: str | None = None
    tag: str | None = None
    vm_uuid: str | None = None

    def effective(self) -> frozenset:
        """What this grant really carries at its scope: VM-level scopes carry only VM-level
        permissions, cluster scopes no node or root permissions, node scopes no root permissions."""
        if self.kind in ('tag', 'vm'):
            return self.perms & VM_LEVEL
        if self.kind == 'cluster':
            return self.perms & (VM_LEVEL | CLUSTER_LEVEL)
        if self.kind == 'node':
            return self.perms - ROOT_LEVEL
        return self.perms


class Access:
    """A user's resolved rights. `superuser` (auth disabled, dev mode) may do everything."""

    def __init__(self, grants: list[Grant], tree: Tree, superuser: bool = False, user_id: str | None = None):
        self.tree = tree
        self.superuser = superuser
        self.user_id = user_id
        self.grants = [g for g in grants if g.effective()]

    # -- coverage ---------------------------------------------------------------------------------
    def _covers_cluster(self, g: Grant, cid: str) -> bool:
        if g.kind == 'root':
            return True
        if g.kind == 'node':
            return self.tree.cluster_within(cid, g.node_id)
        if g.kind == 'cluster':
            return g.cluster_id == cid
        return False

    def _covers_node(self, g: Grant, node_id: str | None) -> bool:
        if g.kind == 'root':
            return True
        if g.kind == 'node':
            return node_id is not None and self.tree.is_within(node_id, g.node_id)
        return False

    # -- questions --------------------------------------------------------------------------------
    def is_root_admin(self) -> bool:
        return self.superuser or any(g.kind == 'root' and ALL <= g.perms for g in self.grants)

    def held_anywhere(self, perm: str) -> bool:
        return self.superuser or any(perm in g.effective() for g in self.grants)

    def perms_anywhere(self) -> frozenset:
        if self.superuser:
            return ALL
        out: set[str] = set()
        for g in self.grants:
            out |= g.effective()
        return frozenset(out)

    def cluster_perms(self, cid: str) -> frozenset:
        """Permissions held on the whole cluster (root, node or cluster grants)."""
        if self.superuser:
            return ALL - ROOT_LEVEL - NODE_LEVEL
        out: set[str] = set()
        for g in self.grants:
            if self._covers_cluster(g, cid):
                out |= g.effective() & (VM_LEVEL | CLUSTER_LEVEL)
        return frozenset(out)

    def vm_grants(self, cid: str) -> list[Grant]:
        """The tag and VM grants on this cluster."""
        return [g for g in self.grants if g.kind in ('tag', 'vm') and g.cluster_id == cid]

    def vm_perms(self, cid: str, vm_uuid: str, vm_tags=()) -> frozenset:
        """Permissions on one VM: the cluster-wide ones plus its tag and VM grants."""
        out = set(self.cluster_perms(cid))
        tags = set(vm_tags or ())
        for g in self.vm_grants(cid):
            if (g.kind == 'vm' and g.vm_uuid == vm_uuid) or (g.kind == 'tag' and g.tag in tags):
                out |= g.effective()
        return frozenset(out)

    def can(self, perm: str, cluster_id: str | None = None, vm_uuid: str | None = None, vm_tags=(),
            node_id: str | None = None) -> bool:
        """The one question routes ask. With a VM: may this user do `perm` on that VM. With only a
        cluster: on the whole cluster. With a node: on that tree node. With nothing: on the install."""
        if self.superuser:
            return True
        if perm not in PERMISSIONS:
            return False
        level = PERMISSIONS[perm]['level']
        if level == 'root':
            return any(g.kind == 'root' and perm in g.perms for g in self.grants)
        if level == 'node' or (cluster_id is None and vm_uuid is None):
            if cluster_id is not None and node_id is None:
                node_id = self.tree.cluster_folder.get(cluster_id)
                if node_id is None:                      # an unfiled cluster belongs to root only
                    return any(g.kind == 'root' and perm in g.effective() for g in self.grants)
            if node_id is None:
                return any(g.kind == 'root' and perm in g.effective() for g in self.grants)
            return any(perm in g.effective() and self._covers_node(g, node_id) for g in self.grants)
        if vm_uuid is not None and level == 'vm':
            return perm in self.vm_perms(cluster_id, vm_uuid, vm_tags)
        return perm in self.cluster_perms(cluster_id)

    def scope_for(self, perm: str, cluster_ids) -> dict:
        """Per cluster where `perm` is held at all: 'all' when the whole cluster is covered, else
        {'vms': [...], 'tags': [...]} from the tag and VM grants. Clusters without it are left out."""
        out: dict = {}
        for cid in cluster_ids:
            if perm in self.cluster_perms(cid):
                out[cid] = 'all'
                continue
            if PERMISSIONS.get(perm, {}).get('level') != 'vm':
                continue
            vms, tags = set(), set()
            for g in self.vm_grants(cid):
                if perm in g.effective():
                    (vms.add(g.vm_uuid) if g.kind == 'vm' else tags.add(g.tag))
            if vms or tags:
                out[cid] = {'vms': sorted(vms), 'tags': sorted(tags)}
        return out

    def visible_clusters(self, cluster_ids) -> list[str]:
        """Clusters where the user holds anything at all (whole cluster or some of its VMs)."""
        out = []
        for cid in cluster_ids:
            if self.superuser or self.cluster_perms(cid) or self.vm_grants(cid):
                out.append(cid)
        return out

    def nodes_with(self, perm: str) -> set:
        """Tree nodes where `perm` holds (every node of a subtree granted). None in the set = root."""
        if self.superuser:
            return {None} | set(self.tree.parent)
        out: set = set()
        for g in self.grants:
            if perm not in g.effective():
                continue
            if g.kind == 'root':
                out |= {None} | set(self.tree.parent)
            elif g.kind == 'node':
                out |= self.tree.subtree(g.node_id)
        return out

    def top_nodes_with(self, perm: str) -> list:
        """The highest nodes where `perm` holds (None = root): where this user 'sits' for it."""
        nodes = self.nodes_with(perm)
        if None in nodes:
            return [None]
        return [n for n in nodes if not any(a in nodes for a in self.tree.ancestors(n)[1:])]

    def visible_nodes(self) -> set:
        """Tree nodes this user may see: every node at or below a node they hold something on, and the
        folder of a cluster they hold something on. Never the layers above that: a customer does not
        see which MSP serves it (a consultant may sell on an MSP's behalf without pointing at it)."""
        if self.superuser or any(g.kind == 'root' for g in self.grants):
            return set(self.tree.parent)
        out: set = set()
        for g in self.grants:
            if g.kind == 'node':
                out |= self.tree.subtree(g.node_id)
            elif g.cluster_id and self.tree.cluster_folder.get(g.cluster_id):
                out.add(self.tree.cluster_folder[g.cluster_id])
        return out

    def nodes_above(self) -> set:
        """The nodes over the visible ones: they exist for the rules (roles defined there apply below)
        but are never named to this user."""
        vis = self.visible_nodes()
        out: set = set()
        for n in vis:
            out |= set(self.tree.ancestors(n)[1:])
        return out - vis

    def path(self, node_id: str | None) -> list[str]:
        """A node's path as this user may see it: the hidden layers above are left out."""
        vis = self.visible_nodes()
        return list(reversed([self.tree.name.get(n, n) for n in self.tree.ancestors(node_id) if n in vis]))


def vm_in_scope(scope_entry, vm_uuid: str, vm_tags=()) -> bool:
    """For one cluster's entry of Access.scope_for: is this VM in it."""
    if scope_entry == 'all':
        return True
    if not scope_entry:
        return False
    return vm_uuid in scope_entry.get('vms', ()) or bool(set(vm_tags or ()) & set(scope_entry.get('tags', ())))


# ---------------------------------------------------------------------------------------------------
# No escalation
# ---------------------------------------------------------------------------------------------------
def may_grant(access: Access, perms, kind: str, node_id=None, cluster_id=None, vm_uuid=None, vm_tags=()) -> tuple[bool, str]:
    """May this user hand out a role with `perms` on this scope? Every permission in it must be one
    they hold there themselves, and the scope must lie inside theirs. Returns (ok, reason)."""
    if access.superuser:
        return True, ''
    perms = expand(perms)
    if kind == 'root':
        missing = [p for p in perms if not any(g.kind == 'root' and p in g.perms for g in access.grants)]
        return (not missing, f'not yours to give on the whole installation: {", ".join(sorted(missing))}' if missing else '')
    if kind == 'node':
        g = Grant(perms, 'node', node_id=node_id)
        missing = [p for p in g.effective() if not access.can(p, node_id=node_id) and not _held_on_subtree(access, p, node_id)]
    elif kind == 'cluster':
        g = Grant(perms, 'cluster', cluster_id=cluster_id)
        missing = [p for p in g.effective() if p not in access.cluster_perms(cluster_id)]
    else:
        g = Grant(perms, kind, cluster_id=cluster_id, vm_uuid=vm_uuid)
        if kind == 'tag':
            # a tag grant reaches whatever carries the tag later: only from someone covering the cluster
            missing = [p for p in g.effective() if p not in access.cluster_perms(cluster_id)]
        else:
            missing = [p for p in g.effective() if not access.can(p, cluster_id, vm_uuid, vm_tags)]
    if missing:
        return False, 'you do not hold these yourself on that scope: ' + ', '.join(sorted(missing))
    if not g.effective():
        return False, 'the role carries nothing at that scope'
    return True, ''


def _held_on_subtree(access: Access, perm: str, node_id) -> bool:
    """A node permission for a node grant: held on that node itself (VM/cluster-level permissions in a
    node grant are checked on the node too, since they reach every cluster below it)."""
    return any(perm in g.effective() and access._covers_node(g, node_id) for g in access.grants)
