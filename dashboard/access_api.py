"""
access_api.py — the dashboard's API for users, groups, roles, assignments, the tenant tree and the
access audit (Settings → Access). Registered by app.py with its route decorator and helpers, so the
same deny-by-default rules apply. Every handler checks the exact target; the decorator only checks
that the permission is held somewhere.

No escalation anywhere (rbac.may_grant): a role can only be handed out, or an assignment removed,
by someone who holds every permission in it on that scope; nobody changes their own assignments.
"""
from __future__ import annotations

import re

from flask import jsonify, request, session

import access_db
import rbac
from access_db import AccessError

_UUID = re.compile(r'^[0-9a-fA-F-]{36}$')


def _uuid_or_none(v):
    v = (v or '').strip() if isinstance(v, str) else v
    if v in (None, '', '*', 'root'):
        return None
    if not isinstance(v, str) or not _UUID.match(v):
        raise AccessError('invalid id')
    return v


def register(app, requires, access, clusters, vm_index_rows):
    """access(): the request's rbac.Access; clusters(): enabled clusters; vm_index_rows(cid): VMs with tags."""

    def actor() -> str:
        return session.get('username') or 'anonymous'

    def me_id() -> str | None:
        return session.get('user_id')

    @app.errorhandler(AccessError)
    def _access_error(e):
        return jsonify({'error': str(e)}), e.status

    # -- helpers --------------------------------------------------------------------------------------
    def may_manage_user(acc: rbac.Access, u: dict) -> bool:
        return acc.can('users.manage', node_id=u.get('home_node_id'))

    def may_manage_group(acc: rbac.Access, g: dict) -> bool:
        return acc.can('groups.manage', node_id=g.get('node_id')) or acc.can('users.manage', node_id=g.get('node_id'))

    def role_usable_at(r: dict, scope: dict, tree: rbac.Tree) -> bool:
        """A role defined at node N is usable at N and below; root roles everywhere."""
        if r['node_id'] is None:
            return True
        kind = scope['kind']
        if kind == 'root':
            return False
        if kind == 'node':
            return tree.is_within(scope['node_id'], r['node_id'])
        return tree.cluster_within(scope['cluster_id'], r['node_id'])

    def node_label(node_id, tree: rbac.Tree) -> str:
        """A node's path; a single-company install hides its default MSP and customer."""
        path = tree.path(node_id)
        single = sum(1 for v in tree.level.values() if v == 'msp') <= 1 and sum(1 for v in tree.level.values() if v == 'customer') <= 1
        if single and len(path) > 1:
            path = path[min(2, len(path) - 1):]
        return ' / '.join(path) or str(node_id)

    def scope_node(scope: dict, tree: rbac.Tree):
        """The tree node a scope sits at (for the audit log and the users.manage check)."""
        if scope['kind'] == 'root':
            return None
        if scope['kind'] == 'node':
            return scope['node_id']
        return tree.cluster_folder.get(scope['cluster_id'])

    def parse_scope(b: dict, tree: rbac.Tree) -> dict:
        s = b.get('scope') or {}
        kind = s.get('kind')
        if kind not in rbac.SCOPE_KINDS:
            raise AccessError('scope.kind must be root, node, cluster, tag or vm')
        out = {'kind': kind}
        if kind == 'node':
            out['node_id'] = _uuid_or_none(s.get('node_id'))
            if out['node_id'] not in tree.parent:
                raise AccessError('unknown node', 404)
        if kind in ('cluster', 'tag', 'vm'):
            cid = _uuid_or_none(s.get('cluster_id'))
            if cid not in {c.cluster_id for c in clusters()} and cid not in tree.cluster_folder:
                raise AccessError('unknown cluster', 404)
            out['cluster_id'] = cid
        if kind == 'tag':
            tag = str(s.get('tag') or '').strip()
            if not tag or len(tag) > 100 or ',' in tag:
                raise AccessError('a tag is required')
            out['tag'] = tag
        if kind == 'vm':
            vm = str(s.get('vm_uuid') or '').strip()
            if not re.fullmatch(r'[0-9A-Za-z-]{1,64}', vm):
                raise AccessError('a VM is required')
            out['vm_uuid'] = vm
            name = next((v.get('name') for v in vm_index_rows(out['cluster_id']) if v.get('uuid') == vm), None)
            out['vm_name'] = name or str(s.get('vm_name') or '')[:200] or None
        return out

    def grant_check(acc: rbac.Access, r: dict, scope: dict) -> None:
        tree = acc.tree
        if not role_usable_at(r, scope, tree):
            raise AccessError('that role is not defined for this part of the tree', 403)
        tags = []
        if scope['kind'] == 'vm':
            tags = next((v.get('tags') or [] for v in vm_index_rows(scope['cluster_id']) if v.get('uuid') == scope['vm_uuid']), [])
        ok, why = rbac.may_grant(acc, r['permissions'], scope['kind'], node_id=scope.get('node_id'),
                                 cluster_id=scope.get('cluster_id'), vm_uuid=scope.get('vm_uuid'), vm_tags=tags)
        if not ok:
            raise AccessError(why, 403)

    def visible_roles(acc: rbac.Access) -> list[dict]:
        nodes = acc.visible_nodes()
        return [r for r in access_db.roles() if r['node_id'] is None or r['node_id'] in nodes or acc.superuser]

    def node_payload(acc: rbac.Access) -> list[dict]:
        visible = acc.visible_nodes() if not acc.is_root_admin() else None
        out = []
        for n in access_db.nodes():
            if visible is not None and n['id'] not in visible:
                continue
            out.append({k: n[k] for k in ('id', 'parent_id', 'level', 'name', 'colour')})
        return out

    # -- overview ------------------------------------------------------------------------------------
    @app.route('/api/access/overview')
    @requires(None)
    def access_overview():
        """Everything Settings → Access shows, narrowed to what this user may manage or see."""
        acc = access()
        tree = acc.tree
        users = [u for u in access_db.users() if may_manage_user(acc, u)] if acc.held_anywhere('users.manage') else []
        groups = [g for g in access_db.groups() if may_manage_group(acc, g)] if \
            (acc.held_anywhere('groups.manage') or acc.held_anywhere('users.manage')) else []
        visible_cids = set(acc.visible_clusters([c.cluster_id for c in clusters()]))
        cl = [{'cluster_id': c.cluster_id, 'name': c.name, 'folder_id': tree.cluster_folder.get(c.cluster_id)}
              for c in clusters() if c.cluster_id in visible_cids]
        return jsonify({
            'nodes': node_payload(acc),
            'clusters': cl,
            'roles': visible_roles(acc),
            'groups': groups,
            'users': [{k: v for k, v in u.items() if k != 'legacy_folders'} for u in users],
            'pending': sum(1 for u in access_db.users() if u['awaiting_access']) if acc.is_root_admin() else 0,
            'new_permissions': access_db.unacknowledged_permissions() if acc.held_anywhere('roles.manage') else [],
            'manage': {p: ['*' if n is None else n for n in acc.top_nodes_with(p)] for p in sorted(rbac.NODE_LEVEL)},
            'single_tenant': sum(1 for n in tree.level.values() if n == 'msp') <= 1
                             and sum(1 for n in tree.level.values() if n == 'customer') <= 1,
        })

    @app.route('/api/access/permissions')
    @requires(None)
    def access_permissions():
        """The permission catalogue and the built-in role templates (for the role editor)."""
        return jsonify({'permissions': [{'key': k, **v} for k, v in rbac.PERMISSIONS.items()], 'areas': list(rbac.AREAS),
                        'builtin': {k: {'name': v['name'], 'permissions': sorted(v['perms'])} for k, v in rbac.BUILTIN_ROLES.items()}})

    # -- tree ----------------------------------------------------------------------------------------
    @app.route('/api/access/nodes', methods=['POST'])
    @requires('tree.manage', 'any')
    def access_node_create():
        b = request.get_json(silent=True) or {}
        acc = access()
        parent = _uuid_or_none(b.get('parent_id'))
        level = b.get('level') or 'folder'
        if not acc.can('tree.manage', node_id=parent):
            raise AccessError('Access denied', 403)
        nid = access_db.create_node(b.get('name'), level, parent, b.get('colour'), actor())
        return jsonify({'ok': True, 'id': nid}), 201

    @app.route('/api/access/nodes/<node_id>', methods=['PUT'])
    @requires('tree.manage', 'any')
    def access_node_update(node_id):
        node_id = _uuid_or_none(node_id)
        acc = access()
        # renaming (or recolouring) the node you sit on is fine; deleting it is only possible from above
        if not acc.can('tree.manage', node_id=node_id):
            raise AccessError('Access denied', 403)
        b = request.get_json(silent=True) or {}
        access_db.rename_node(node_id, b.get('name'), b['colour'] if 'colour' in b else ..., actor())
        return jsonify({'ok': True})

    @app.route('/api/access/nodes/<node_id>', methods=['DELETE'])
    @requires('tree.manage', 'any')
    def access_node_delete(node_id):
        node_id = _uuid_or_none(node_id)
        acc = access()
        # a node can only be deleted from above it: whoever sits on it may not remove their own seat
        if not acc.can('tree.manage', node_id=acc.tree.parent.get(node_id)) or node_id is None:
            raise AccessError('Access denied', 403)
        return jsonify({'ok': True, **access_db.delete_node(node_id, actor())})

    # -- users ---------------------------------------------------------------------------------------
    @app.route('/api/access/users', methods=['POST'])
    @requires('users.manage', 'any')
    def access_user_create():
        b = request.get_json(silent=True) or {}
        acc = access()
        home = _uuid_or_none(b.get('home_node_id'))
        if not acc.can('users.manage', node_id=home):
            raise AccessError('Access denied', 403)
        uid = access_db.create_user(b.get('username'), b.get('password') or '', home, b.get('display_name'), actor())
        return jsonify({'ok': True, 'user_id': uid}), 201

    def managed_user(user_id: str) -> dict:
        user_id = _uuid_or_none(user_id)
        u = access_db.user(user_id) if user_id else None
        if not u or not may_manage_user(access(), u):
            raise AccessError('not found', 404)
        return u

    @app.route('/api/access/users/<user_id>')
    @requires('users.manage', 'any')
    def access_user_get(user_id):
        u = managed_user(user_id)
        tree = access().tree
        rows = access_db.assignments(user_id=u['user_id'])
        for g in u['groups']:
            for a in access_db.assignments(group_id=g):
                rows.append(dict(a, via_group=True))
        for a in rows:
            a['scope_label'] = access_db.scope_label(a, tree)
        legacy = [node_label(f, tree) for f in u.get('legacy_folders') or []]
        return jsonify(dict({k: v for k, v in u.items() if k != 'legacy_folders'}, assignments=rows, legacy_folder_names=legacy))

    @app.route('/api/access/users/<user_id>', methods=['PUT'])
    @requires('users.manage', 'any')
    def access_user_update(user_id):
        u = managed_user(user_id)
        b = request.get_json(silent=True) or {}
        acc = access()
        kw = {}
        if 'enabled' in b:
            if u['user_id'] == me_id():
                raise AccessError('you cannot disable your own account', 409)
            kw['enabled'] = bool(b['enabled'])
        if 'home_node_id' in b:
            home = _uuid_or_none(b['home_node_id'])
            if not acc.can('users.manage', node_id=home):
                raise AccessError('Access denied', 403)
            kw['home_node_id'] = home
        if 'display_name' in b:
            kw['display_name'] = str(b['display_name'] or '')
        access_db.update_user(u['user_id'], actor(), **kw)
        return jsonify({'ok': True})

    @app.route('/api/access/users/<user_id>', methods=['DELETE'])
    @requires('users.manage', 'any')
    def access_user_delete(user_id):
        u = managed_user(user_id)
        if u['user_id'] == me_id():
            raise AccessError('you cannot delete your own account', 409)
        access_db.delete_user(u['user_id'], actor())
        return jsonify({'ok': True})

    @app.route('/api/access/users/<user_id>/password', methods=['PUT'])
    @requires('users.manage', 'any')
    def access_user_password(user_id):
        import user_db
        u = managed_user(user_id)
        pw = (request.get_json(silent=True) or {}).get('new_password') or ''
        if len(pw) < 8:
            raise AccessError('the password needs at least 8 characters')
        user_db.set_password(u['username'], pw)
        conn = access_db._connect()
        try:
            with conn.cursor() as cur:
                access_db.audit(cur, actor(), 'user.password_reset', u['username'], node_id=u['home_node_id'])
            conn.commit()
        finally:
            conn.close()
        return jsonify({'ok': True})

    @app.route('/api/access/pending')
    @requires('users.manage', 'root')
    def access_pending():
        """Accounts the upgrade disabled until their access is set (the upgrade modal, root admins only)."""
        tree = access().tree
        out = []
        for u in access_db.users():
            if u['awaiting_access']:
                out.append({'user_id': u['user_id'], 'username': u['username'], 'display_name': u['display_name'],
                            'legacy_folders': [{'id': f, 'name': node_label(f, tree)} for f in u['legacy_folders'] or []]})
        return jsonify({'pending': out})

    # -- assignments ---------------------------------------------------------------------------------
    @app.route('/api/access/assignments', methods=['POST'])
    @requires('users.manage', 'any')
    def access_assignment_add():
        b = request.get_json(silent=True) or {}
        acc = access()
        tree = acc.tree
        r = access_db.role(_uuid_or_none(b.get('role_id')) or '')
        if not r:
            raise AccessError('unknown role', 404)
        scope = parse_scope(b, tree)
        if b.get('user_id'):
            u = managed_user(b['user_id'])
            if u['user_id'] == me_id():
                raise AccessError('you cannot change your own access', 409)
            principal = {'user_id': u['user_id']}
        elif b.get('group_id'):
            g = access_db.group(_uuid_or_none(b['group_id']) or '')
            if not g or not may_manage_group(acc, g):
                raise AccessError('not found', 404)
            if any(m['user_id'] == me_id() for m in g['members']):
                raise AccessError('you are a member of this group: you cannot change its access', 409)
            principal = {'group_id': g['group_id']}
        else:
            raise AccessError('user_id or group_id required')
        grant_check(acc, r, scope)
        aid = access_db.add_assignment(principal, r['role_id'], scope, actor(), scope_node(scope, tree))
        return jsonify({'ok': True, 'assignment_id': aid}), 201

    @app.route('/api/access/assignments/<assignment_id>', methods=['DELETE'])
    @requires('users.manage', 'any')
    def access_assignment_remove(assignment_id):
        acc = access()
        a = access_db.assignment(_uuid_or_none(assignment_id) or '')
        if not a:
            raise AccessError('not found', 404)
        if a['user_id']:
            u = managed_user(a['user_id'])
            if u['user_id'] == me_id():
                raise AccessError('you cannot change your own access', 409)
        else:
            g = access_db.group(a['group_id'])
            if not g or not may_manage_group(acc, g):
                raise AccessError('not found', 404)
            if any(m['user_id'] == me_id() for m in g['members']):
                raise AccessError('you are a member of this group: you cannot change its access', 409)
        scope = {'kind': a['scope_kind'], 'node_id': a['node_id'], 'cluster_id': a['cluster_id'], 'tag': a['tag'],
                 'vm_uuid': a['vm_uuid']}
        r = access_db.role(a['role_id'])
        grant_check(acc, r, scope)               # only what you could have given yourself
        access_db.remove_assignment(a['assignment_id'], actor(), scope_node(scope, acc.tree))
        return jsonify({'ok': True})

    # -- roles ---------------------------------------------------------------------------------------
    def role_body_check(acc: rbac.Access, node_id, perms) -> None:
        if not acc.can('roles.manage', node_id=node_id):
            raise AccessError('Access denied', 403)
        perms = rbac.expand(perms or [])
        unknown = [p for p in (perms or []) if p not in rbac.PERMISSIONS]
        if unknown:
            raise AccessError('unknown permissions: ' + ', '.join(unknown))
        kind = 'root' if node_id is None else 'node'
        ok, why = rbac.may_grant(acc, perms, kind, node_id=node_id)
        if not ok:
            raise AccessError(why, 403)

    @app.route('/api/access/roles', methods=['POST'])
    @requires('roles.manage', 'any')
    def access_role_create():
        b = request.get_json(silent=True) or {}
        acc = access()
        node = _uuid_or_none(b.get('node_id'))
        role_body_check(acc, node, b.get('permissions'))
        rid = access_db.save_role(None, b.get('name'), b.get('description') or '', node, b.get('permissions') or [], actor(),
                                  copied_from=(str(b.get('copied_from'))[:60] if b.get('copied_from') else None))
        return jsonify({'ok': True, 'role_id': rid}), 201

    @app.route('/api/access/roles/<role_id>', methods=['PUT'])
    @requires('roles.manage', 'any')
    def access_role_update(role_id):
        r = access_db.role(_uuid_or_none(role_id) or '')
        acc = access()
        if not r or not acc.can('roles.manage', node_id=r['node_id']):
            raise AccessError('not found', 404)
        b = request.get_json(silent=True) or {}
        role_body_check(acc, r['node_id'], b.get('permissions'))
        access_db.save_role(r['role_id'], b.get('name') or r['name'], b.get('description') if 'description' in b else r['description'],
                            r['node_id'], b.get('permissions') or [], actor())
        return jsonify({'ok': True})

    @app.route('/api/access/roles/<role_id>', methods=['DELETE'])
    @requires('roles.manage', 'any')
    def access_role_delete(role_id):
        r = access_db.role(_uuid_or_none(role_id) or '')
        if not r or not access().can('roles.manage', node_id=r['node_id']):
            raise AccessError('not found', 404)
        access_db.delete_role(r['role_id'], actor())
        return jsonify({'ok': True})

    @app.route('/api/access/permissions/acknowledge', methods=['POST'])
    @requires('roles.manage', 'any')
    def access_permissions_ack():
        access_db.acknowledge_permissions(actor())
        return jsonify({'ok': True})

    # -- groups --------------------------------------------------------------------------------------
    @app.route('/api/access/groups', methods=['POST'])
    @requires('groups.manage', 'any')
    def access_group_create():
        b = request.get_json(silent=True) or {}
        node = _uuid_or_none(b.get('node_id'))
        if not access().can('groups.manage', node_id=node):
            raise AccessError('Access denied', 403)
        gid = access_db.save_group(None, b.get('name'), b.get('description') or '', node, actor())
        return jsonify({'ok': True, 'group_id': gid}), 201

    def managed_group(group_id: str) -> dict:
        g = access_db.group(_uuid_or_none(group_id) or '')
        if not g or not access().can('groups.manage', node_id=g['node_id']):
            raise AccessError('not found', 404)
        return g

    @app.route('/api/access/groups/<group_id>')
    @requires('groups.manage', 'any')
    def access_group_get(group_id):
        g = managed_group(group_id)
        tree = access().tree
        rows = access_db.assignments(group_id=g['group_id'])
        for a in rows:
            a['scope_label'] = access_db.scope_label(a, tree)
        return jsonify(dict(g, assignments=rows))

    @app.route('/api/access/groups/<group_id>', methods=['PUT'])
    @requires('groups.manage', 'any')
    def access_group_update(group_id):
        g = managed_group(group_id)
        b = request.get_json(silent=True) or {}
        access_db.save_group(g['group_id'], b.get('name') or g['name'], b.get('description') if 'description' in b else g['description'],
                             g['node_id'], actor())
        return jsonify({'ok': True})

    @app.route('/api/access/groups/<group_id>', methods=['DELETE'])
    @requires('groups.manage', 'any')
    def access_group_delete(group_id):
        g = managed_group(group_id)
        if any(m['user_id'] == me_id() for m in g['members']):
            raise AccessError('you are a member of this group: you cannot delete it', 409)
        access_db.delete_group(g['group_id'], actor())
        return jsonify({'ok': True})

    @app.route('/api/access/groups/<group_id>/members', methods=['PUT'])
    @requires('groups.manage', 'any')
    def access_group_member(group_id):
        g = managed_group(group_id)
        b = request.get_json(silent=True) or {}
        u = managed_user(b.get('user_id') or '')
        if u['user_id'] == me_id():
            raise AccessError('you cannot change your own access', 409)
        acc = access()
        if b.get('member', True):
            # joining a group hands over the group's access: the same no-escalation rule as an assignment
            for a in access_db.assignments(group_id=g['group_id']):
                scope = {'kind': a['scope_kind'], 'node_id': a['node_id'], 'cluster_id': a['cluster_id'], 'tag': a['tag'],
                         'vm_uuid': a['vm_uuid']}
                grant_check(acc, access_db.role(a['role_id']), scope)
        access_db.set_group_member(g['group_id'], u['user_id'], bool(b.get('member', True)), actor())
        return jsonify({'ok': True})

    # -- audit ---------------------------------------------------------------------------------------
    @app.route('/api/access/audit')
    @requires('audit.view', 'any')
    def access_audit():
        acc = access()
        nodes = None if acc.can('audit.view') else acc.nodes_with('audit.view')
        try:
            before = int(request.args.get('before') or 0) or None
        except ValueError:
            before = None
        return jsonify({'rows': access_db.audit_rows(nodes, 300, before)})

    @app.route('/api/access/scoped-tags')
    @requires('vm.view')
    def access_scoped_tags():
        """Tags of the selected cluster that give someone access (the VM edit window marks them)."""
        cid = _uuid_or_none(request.args.get('cluster'))
        acc = access()
        if not cid or cid not in set(acc.visible_clusters([cid])):
            raise AccessError('not found', 404)
        tags = access_db.scoped_tags(cid)
        if not acc.can('users.manage', cid):
            tags = {t: [] for t in tags}          # which tags matter, not who holds them
        return jsonify({'tags': tags, 'may_change': acc.can('users.manage', cid)})

    @app.route('/api/access/copy-vm-access', methods=['POST'])
    @requires('users.manage', 'any')
    def access_copy_vm_access():
        """{cluster_id, from_vm, to_vm}: a restore as a new VM or a clone gets a new UUID, so single-VM
        access does not follow it. This gives the same people the same access on the new VM, for every
        assignment the caller could have given (the others are listed as skipped)."""
        b = request.get_json(silent=True) or {}
        acc = access()
        cid = _uuid_or_none(b.get('cluster_id'))
        src, dst = str(b.get('from_vm') or ''), str(b.get('to_vm') or '')
        if not cid or not re.fullmatch(r'[0-9A-Za-z-]{1,64}', src) or not re.fullmatch(r'[0-9A-Za-z-]{1,64}', dst) or src == dst:
            raise AccessError('cluster_id, from_vm and to_vm required')
        if not acc.can('users.manage', cid):
            raise AccessError('not found', 404)
        index = {v.get('uuid'): v for v in vm_index_rows(cid)}
        if dst not in index:
            raise AccessError('the new VM is not on that cluster', 404)
        done, skipped = [], []
        for a in access_db.assignments(cluster_id=cid):
            if a['scope_kind'] != 'vm' or a['vm_uuid'] != src:
                continue
            scope = {'kind': 'vm', 'cluster_id': cid, 'vm_uuid': dst, 'vm_name': index[dst].get('name')}
            who = a['username'] or f"group {a['group_name']}"
            try:
                if a['user_id'] == me_id():
                    raise AccessError('your own access')
                grant_check(acc, access_db.role(a['role_id']), scope)
                principal = {'user_id': a['user_id']} if a['user_id'] else {'group_id': a['group_id']}
                access_db.add_assignment(principal, a['role_id'], scope, actor(), acc.tree.cluster_folder.get(cid))
                done.append(f"{who}: {a['role_name']}")
            except AccessError as e:
                skipped.append(f"{who}: {a['role_name']} ({e})")
        return jsonify({'ok': True, 'copied': done, 'skipped': skipped})

    # -- who has access ------------------------------------------------------------------------------
    @app.route('/api/access/who')
    @requires('users.manage', 'any')
    def access_who():
        """Who may do what on one cluster (?cluster=) or one VM (&vm=), and where each right comes from."""
        acc = access()
        cid = _uuid_or_none(request.args.get('cluster'))
        vm = (request.args.get('vm') or '').strip() or None
        if not cid or not acc.can('users.manage', cid):
            raise AccessError('not found', 404)
        tree = acc.tree
        tags = next((v.get('tags') or [] for v in vm_index_rows(cid) if v.get('uuid') == vm), []) if vm else []
        out = []
        users = access_db.users()
        for u in users:
            grants = access_db.grants_of_principal(user_id=u['user_id'])
            a = rbac.Access(grants, tree)
            perms = a.vm_perms(cid, vm, tags) if vm else a.cluster_perms(cid)
            if not perms and not vm and a.vm_grants(cid):
                perms = frozenset().union(*[g.effective() for g in a.vm_grants(cid)])
            if not perms:
                continue
            def covers(x):
                k = x['scope_kind']
                if k == 'root':
                    return True
                if k == 'node':
                    return tree.cluster_within(cid, x['node_id'])
                if k == 'cluster':
                    return x['cluster_id'] == cid
                if x['cluster_id'] != cid:
                    return False
                if not vm:
                    return True
                return (k == 'vm' and x['vm_uuid'] == vm) or (k == 'tag' and x['tag'] in tags)
            via = [dict(x, scope_label=access_db.scope_label(x, tree)) for x in access_db.assignments(user_id=u['user_id']) if covers(x)]
            for gid in u['groups']:
                via += [dict(x, scope_label=access_db.scope_label(x, tree), via_group=True)
                        for x in access_db.assignments(group_id=gid) if covers(x)]
            out.append({'user_id': u['user_id'], 'username': u['username'], 'enabled': u['enabled'],
                        'permissions': sorted(perms),
                        'via': [{'role': x['role_name'], 'scope': x['scope_label'], 'group': x.get('group_name') if x.get('via_group') else None}
                                for x in via]})
        return jsonify({'cluster_id': cid, 'vm_uuid': vm, 'tags': tags, 'users': out})
