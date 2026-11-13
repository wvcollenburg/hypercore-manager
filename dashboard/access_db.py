"""
access_db.py — the rows rbac.py reasons about: the tenant tree, roles, groups, assignments, users'
access state, and the access audit log.

Kept apart from rbac.py so the rules stay pure (and unit-testable) and the SQL stays in one place.
Reads are cached briefly per process (a page load fires a dozen API calls); every write calls
`invalidate()`. Before migration 044 has run (the first seconds after an upgrade), the legacy model
applies: admin = everything, user = the former folder-member rights on their folders.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time

import rbac

log = logging.getLogger(__name__)

_TTL = 60.0                 # an upper bound only: any change bumps the version below, which every worker sees
_VERSION_TTL = 1.0          # how long a worker trusts its last look at the version
_lock = threading.Lock()
_ver: dict = {'at': 0.0, 'v': None}
_tree_cache: dict = {'at': 0.0, 'tree': None}
_grants_cache: dict[str, tuple[float, list]] = {}
_state_cache: dict[str, tuple[float, dict | None]] = {}
_roles_cache: dict = {'at': 0.0, 'roles': None}

TEMPLATE_CLUSTER = '00000000-0000-0000-0000-000000000001'
_MISSING = ('42P01', '42703')            # undefined table / column: migration 044 not applied yet


class AccessError(Exception):
    """A refused change (bad input, conflicts, rule broken); the message is shown to the user."""
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def invalidate() -> None:
    """Drop this worker's caches and bump the shared version, so the other gunicorn workers drop theirs
    within _VERSION_TTL (a removed right must not linger in another worker's cache)."""
    with _lock:
        _tree_cache['at'] = 0.0
        _roles_cache['at'] = 0.0
        _grants_cache.clear()
        _state_cache.clear()
        _ver['at'] = 0.0
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                _bump(cur)
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — before migration 044 there is no table to bump
        log.debug('access version not bumped: %s', exc)


def _bump(cur) -> None:
    cur.execute('''INSERT INTO config.rbac_state (key, value) VALUES ('version', '1')
                   ON CONFLICT (key) DO UPDATE SET value = (COALESCE(NULLIF(config.rbac_state.value, ''), '0')::bigint + 1)::text,
                                                   at = NOW()''')


_tl = threading.local()


def begin_request() -> None:
    """Called before every dashboard request: the first cache lookup of the request re-reads the shared
    version (one small query per request), so a change made through another worker is seen at once."""
    _tl.fresh = False


def _version():
    if getattr(_tl, 'fresh', None) is True:
        return _tl.v
    now = time.monotonic()
    if getattr(_tl, 'fresh', None) is None:                 # outside a request (startup, threads): a short TTL
        with _lock:
            if now - _ver['at'] < _VERSION_TTL:
                return _ver['v']
    v = None
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM config.rbac_state WHERE key = 'version'")
                row = cur.fetchone()
                v = row['value'] if row else '0'
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — no table yet (before 044): caches fall back to their TTL
        v = None
    with _lock:
        _ver.update(at=now, v=v)
    if getattr(_tl, 'fresh', None) is False:
        _tl.fresh, _tl.v = True, v
    return v





def _connect():
    import user_db
    return user_db._connect()


def _missing(exc) -> bool:
    return getattr(exc, 'pgcode', None) in _MISSING


def rbac_ready() -> bool:
    """True once migration 044 has run."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('config.assignments') IS NOT NULL AS ok")
            return bool(cur.fetchone()['ok'])
    finally:
        conn.close()


# ---------------------------------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------------------------------
def load_tree() -> rbac.Tree:
    now = time.monotonic()
    ver = _version()
    with _lock:
        if _tree_cache['tree'] is not None and now - _tree_cache['at'] < _TTL and _tree_cache.get('v') == ver:
            return _tree_cache['tree']
    tree = rbac.Tree()
    conn = _connect()
    try:
        with conn.cursor() as cur:
            try:
                cur.execute('SELECT folder_id::text AS id, parent_id::text AS parent, level, name FROM config.folders')
                rows = cur.fetchall()
            except Exception as exc:  # noqa: BLE001
                if not _missing(exc):
                    raise
                conn.rollback()
                cur.execute("SELECT folder_id::text AS id, NULL AS parent, 'folder' AS level, name FROM config.folders")
                rows = cur.fetchall()
            for r in rows:
                tree.parent[r['id']] = r['parent']
                tree.level[r['id']] = r['level']
                tree.name[r['id']] = r['name']
            cur.execute('SELECT cluster_id::text AS id, folder_id::text AS folder FROM config.clusters WHERE cluster_id <> %s',
                        (TEMPLATE_CLUSTER,))
            for r in cur.fetchall():
                tree.cluster_folder[r['id']] = r['folder']
    finally:
        conn.close()
    with _lock:
        _tree_cache.update(at=now, tree=tree, v=ver)
    return tree


def _legacy_grants(cur, user_id: str) -> list[rbac.Grant]:
    cur.execute('SELECT role FROM config.users WHERE user_id = %s', (user_id,))
    row = cur.fetchone()
    if row is None:
        return []
    if row['role'] == 'admin':
        return [rbac.Grant(rbac.ALL, 'root')]
    cur.execute('SELECT folder_id::text AS id FROM config.folder_members WHERE user_id = %s', (user_id,))
    return [rbac.Grant(rbac.LEGACY_USER, 'node', node_id=r['id']) for r in cur.fetchall()]


def _grant_of(r) -> rbac.Grant:
    perms = rbac.role_perms(r['builtin_key'], r['permissions'])
    return rbac.Grant(perms, r['scope_kind'], node_id=r['node_id'], cluster_id=r['cluster_id'], tag=r['tag'],
                      vm_uuid=r['vm_uuid'])


_GRANT_SQL = '''SELECT a.scope_kind, a.node_id::text AS node_id, a.cluster_id::text AS cluster_id, a.tag, a.vm_uuid,
                       r.builtin_key, r.permissions
                  FROM config.assignments a JOIN config.roles r ON r.role_id = a.role_id
                 WHERE a.user_id = %(u)s
                    OR a.group_id IN (SELECT group_id FROM config.group_members WHERE user_id = %(u)s)'''


def load_grants(user_id: str) -> list[rbac.Grant]:
    now = time.monotonic()
    ver = _version()
    with _lock:
        hit = _grants_cache.get(user_id)
        if hit and now - hit[0] < _TTL and hit[2] == ver:
            return hit[1]
    conn = _connect()
    try:
        with conn.cursor() as cur:
            try:
                _heal_root_admin(cur, user_id)
                cur.execute(_GRANT_SQL, {'u': user_id})
                grants = [_grant_of(r) for r in cur.fetchall()]
                conn.commit()
            except Exception as exc:  # noqa: BLE001
                if not _missing(exc):
                    raise
                conn.rollback()
                grants = _legacy_grants(cur, user_id)
    finally:
        conn.close()
    with _lock:
        _grants_cache[user_id] = (now, grants, ver)
    return grants


def _heal_root_admin(cur, user_id: str) -> None:
    """The seeded first admin (fresh install: created after the migrations ran) has role 'admin' but no
    assignment yet. role = 'admin' is kept equal to "holds Admin on the whole install", so the missing
    assignment is added; removing that assignment through the UI sets role back to 'user'."""
    cur.execute('''SELECT 1 FROM config.users u
                    WHERE u.user_id = %s AND u.role = 'admin'
                      AND NOT EXISTS (SELECT 1 FROM config.assignments a JOIN config.roles r ON r.role_id = a.role_id
                                       WHERE a.user_id = u.user_id AND a.scope_kind = 'root' AND r.builtin_key = 'admin')''',
                (user_id,))
    if cur.fetchone():
        cur.execute('''INSERT INTO config.assignments (user_id, role_id, scope_kind, created_by)
                       SELECT %s, role_id, 'root', 'system' FROM config.roles WHERE builtin_key = 'admin' ''', (user_id,))
        cur.execute("INSERT INTO config.access_audit (actor, action, target) "
                    "SELECT 'system', 'assignment.add', username || ': Admin on the whole installation (first administrator)' "
                    "FROM config.users WHERE user_id = %s", (user_id,))
        _bump(cur)


def user_state(user_id: str) -> dict | None:
    """{username, enabled, awaiting_access} of a user id, or None when the user no longer exists (cached
    briefly: every API call asks)."""
    now = time.monotonic()
    ver = _version()
    with _lock:
        hit = _state_cache.get(user_id)
        if hit and now - hit[0] < _TTL and hit[2] == ver:
            return hit[1]
    conn = _connect()
    try:
        with conn.cursor() as cur:
            try:
                cur.execute('SELECT username, enabled, awaiting_access FROM config.users WHERE user_id = %s', (user_id,))
                row = cur.fetchone()
            except Exception as exc:  # noqa: BLE001
                if not _missing(exc):
                    raise
                conn.rollback()
                cur.execute('SELECT username, TRUE AS enabled, FALSE AS awaiting_access FROM config.users WHERE user_id = %s',
                            (user_id,))
                row = cur.fetchone()
    finally:
        conn.close()
    state = dict(row) if row else None
    with _lock:
        _state_cache[user_id] = (now, state, ver)
    return state


def access_for(user_id: str | None, superuser: bool = False) -> rbac.Access:
    if superuser:
        return rbac.Access([], rbac.Tree(), superuser=True)
    try:
        tree = load_tree()
        grants = load_grants(user_id) if user_id else []
    except Exception as exc:  # noqa: BLE001 — the database is down: nobody gets anything
        log.warning('access unavailable: %s', exc)
        return rbac.Access([], rbac.Tree(), user_id=user_id)
    return rbac.Access(grants, tree, user_id=user_id)


# ---------------------------------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------------------------------
def audit(cur, actor: str | None, action: str, target: str, node_id: str | None = None, detail: dict | None = None) -> None:
    cur.execute('INSERT INTO config.access_audit (actor, action, target, node_id, detail) VALUES (%s, %s, %s, %s, %s)',
                (actor, action, target, node_id, json.dumps(detail) if detail else None))


def audit_rows(node_ids: set | None, limit: int = 300, before_id: int | None = None) -> list[dict]:
    """The access audit log; node_ids None = everything (root), else rows at those nodes."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            where, params = [], []
            if node_ids is not None:
                where.append('node_id = ANY(%s::uuid[])')
                params.append([n for n in node_ids if n])
            if before_id:
                where.append('id < %s')
                params.append(before_id)
            sql = 'SELECT id, at, actor, action, target, node_id::text AS node_id, detail FROM config.access_audit'
            if where:
                sql += ' WHERE ' + ' AND '.join(where)
            cur.execute(sql + ' ORDER BY id DESC LIMIT %s', params + [limit])
            return [dict(r, at=r['at'].isoformat()) for r in cur.fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------------------------------------------
# tree
# ---------------------------------------------------------------------------------------------------
def nodes() -> list[dict]:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute('''SELECT folder_id::text AS id, parent_id::text AS parent_id, level, name, colour, share_id, created_at
                             FROM config.folders ORDER BY lower(name)''')
            return [dict(r, created_at=r['created_at'].isoformat() if r['created_at'] else None) for r in cur.fetchall()]
    finally:
        conn.close()


def create_node(name: str, level: str, parent_id: str | None, colour: str | None, actor: str) -> str:
    name = (name or '').strip()
    if not name or len(name) > 80:
        raise AccessError('a name is required (at most 80 characters)')
    if level not in rbac.NODE_LEVELS:
        raise AccessError('unknown level')
    tree = load_tree()
    want_parent = rbac.PARENT_LEVEL[level]
    if (parent_id is None) != (want_parent is None) or (parent_id and tree.level.get(parent_id) != want_parent):
        raise AccessError(f'a {level} belongs under {"the installation" if want_parent is None else "a " + want_parent}')
    conn = _connect()
    try:
        with conn.cursor() as cur:
            try:
                cur.execute('''INSERT INTO config.folders (name, level, parent_id, colour) VALUES (%s, %s, %s, %s)
                               RETURNING folder_id::text AS id''', (name, level, parent_id, colour or None))
            except Exception as exc:  # noqa: BLE001
                if 'ux_folders_parent_name' in str(exc) or 'duplicate key' in str(exc):
                    raise AccessError(f'there already is a {level} called {name!r} here', 409) from exc
                raise
            nid = cur.fetchone()['id']
            audit(cur, actor, f'{level}.create', name, node_id=nid, detail={'parent': parent_id})
        conn.commit()
    finally:
        conn.close()
    invalidate()
    return nid


def rename_node(node_id: str, name: str | None, colour, actor: str) -> None:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT name, level FROM config.folders WHERE folder_id = %s', (node_id,))
            row = cur.fetchone()
            if not row:
                raise AccessError('not found', 404)
            if name is not None:
                name = name.strip()
                if not name or len(name) > 80:
                    raise AccessError('a name is required (at most 80 characters)')
                try:
                    cur.execute('UPDATE config.folders SET name = %s, updated_at = NOW() WHERE folder_id = %s', (name, node_id))
                except Exception as exc:  # noqa: BLE001
                    if 'duplicate key' in str(exc):
                        raise AccessError(f'there already is one called {name!r} here', 409) from exc
                    raise
                audit(cur, actor, f"{row['level']}.rename", f"{row['name']} → {name}", node_id=node_id)
            if colour is not ...:
                cur.execute('UPDATE config.folders SET colour = %s, updated_at = NOW() WHERE folder_id = %s', (colour or None, node_id))
        conn.commit()
    finally:
        conn.close()
    invalidate()


def delete_node(node_id: str, actor: str) -> dict:
    """Delete a tree node (plans/PLAN-roles.md, Q4): it must have no child nodes; its clusters become
    unfiled (root only), its roles, groups and assignments go. Accounts belong to people, not to a
    node: they only lose the access they had here (an account left with none can no longer see anything)."""
    tree = load_tree()
    if node_id not in tree.parent:
        raise AccessError('not found', 404)
    if tree.children(node_id):
        raise AccessError('remove or move what is inside it first', 409)
    parent = tree.parent[node_id]
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT role_id FROM config.roles WHERE node_id = %s', (node_id,))
            role_ids = [r['role_id'] for r in cur.fetchall()]
            if role_ids:
                cur.execute('DELETE FROM config.assignments WHERE role_id = ANY(%s)', (role_ids,))
            cur.execute('UPDATE config.clusters SET folder_id = NULL WHERE folder_id = %s RETURNING cluster_name', (node_id,))
            unfiled = [r['cluster_name'] for r in cur.fetchall()]
            cur.execute('DELETE FROM config.folders WHERE folder_id = %s RETURNING name, level', (node_id,))
            row = cur.fetchone()
            audit(cur, actor, f"{row['level']}.delete", row['name'], node_id=parent,
                  detail={'unfiled_clusters': unfiled})
        conn.commit()
    finally:
        conn.close()
    invalidate()
    return {'unfiled_clusters': unfiled}


# ---------------------------------------------------------------------------------------------------
# roles
# ---------------------------------------------------------------------------------------------------
def roles() -> list[dict]:
    now = time.monotonic()
    ver = _version()
    with _lock:
        if _roles_cache['roles'] is not None and now - _roles_cache['at'] < _TTL and _roles_cache.get('v') == ver:
            return _roles_cache['roles']
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute('''SELECT r.role_id::text AS role_id, r.name, r.description, r.node_id::text AS node_id, r.builtin_key,
                                  r.permissions, r.copied_from, r.created_by, r.updated_at,
                                  (SELECT COUNT(*) FROM config.assignments a WHERE a.role_id = r.role_id) AS in_use
                             FROM config.roles r
                            ORDER BY r.builtin_key IS NULL, array_position(ARRAY['admin','engineer','app_manager','backup_manager','viewer'], r.builtin_key),
                                     lower(r.name)''')
            out = []
            for r in cur.fetchall():
                d = dict(r)
                d['permissions'] = sorted(rbac.role_perms(d['builtin_key'], d['permissions']))
                d['updated_at'] = d['updated_at'].isoformat() if d['updated_at'] else None
                out.append(d)
    finally:
        conn.close()
    with _lock:
        _roles_cache.update(at=now, roles=out, v=ver)
    return out


def role(role_id: str) -> dict | None:
    return next((r for r in roles() if r['role_id'] == role_id), None)


def builtin_role_id(key: str) -> str | None:
    return next((r['role_id'] for r in roles() if r['builtin_key'] == key), None)


def save_role(role_id: str | None, name: str, description: str, node_id: str | None, perms, actor: str,
              copied_from: str | None = None) -> str:
    name = (name or '').strip()
    if not name or len(name) > 60:
        raise AccessError('a name is required (at most 60 characters)')
    perms = sorted(rbac.expand(perms))
    if not perms:
        raise AccessError('a role needs at least one permission')
    conn = _connect()
    try:
        with conn.cursor() as cur:
            try:
                if role_id:
                    cur.execute('SELECT name, builtin_key, permissions FROM config.roles WHERE role_id = %s', (role_id,))
                    old = cur.fetchone()
                    if not old:
                        raise AccessError('not found', 404)
                    if old['builtin_key']:
                        raise AccessError('built-in roles are templates: copy one to change it', 409)
                    cur.execute('''UPDATE config.roles SET name = %s, description = %s, permissions = %s, updated_at = NOW()
                                   WHERE role_id = %s''', (name, description or None, perms, role_id))
                    added = sorted(set(perms) - set(old['permissions'] or []))
                    removed = sorted(set(old['permissions'] or []) - set(perms))
                    audit(cur, actor, 'role.update', name, node_id=node_id, detail={'added': added, 'removed': removed})
                else:
                    cur.execute('''INSERT INTO config.roles (name, description, node_id, permissions, copied_from, created_by)
                                   VALUES (%s, %s, %s, %s, %s, %s) RETURNING role_id::text AS id''',
                                (name, description or None, node_id, perms, copied_from, actor))
                    role_id = cur.fetchone()['id']
                    audit(cur, actor, 'role.create', name, node_id=node_id, detail={'permissions': perms, 'copied_from': copied_from})
            except AccessError:
                raise
            except Exception as exc:  # noqa: BLE001
                if 'duplicate key' in str(exc):
                    raise AccessError(f'there already is a role called {name!r} here', 409) from exc
                raise
        conn.commit()
    finally:
        conn.close()
    invalidate()
    return role_id


def delete_role(role_id: str, actor: str) -> None:
    r = role(role_id)
    if not r:
        raise AccessError('not found', 404)
    if r['builtin_key']:
        raise AccessError('built-in roles cannot be deleted', 409)
    if r['in_use']:
        raise AccessError(f"the role is still assigned {r['in_use']} time(s): remove those assignments first", 409)
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute('DELETE FROM config.roles WHERE role_id = %s', (role_id,))
            audit(cur, actor, 'role.delete', r['name'], node_id=r['node_id'])
        conn.commit()
    finally:
        conn.close()
    invalidate()


# ---------------------------------------------------------------------------------------------------
# groups
# ---------------------------------------------------------------------------------------------------
def groups() -> list[dict]:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute('''SELECT g.group_id::text AS group_id, g.name, g.description, g.node_id::text AS node_id,
                                  COALESCE(json_agg(json_build_object('user_id', u.user_id::text, 'username', u.username)
                                           ORDER BY u.username) FILTER (WHERE u.user_id IS NOT NULL), '[]') AS members
                             FROM config.groups g
                             LEFT JOIN config.group_members m ON m.group_id = g.group_id
                             LEFT JOIN config.users u ON u.user_id = m.user_id
                            GROUP BY g.group_id ORDER BY lower(g.name)''')
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def group(group_id: str) -> dict | None:
    return next((g for g in groups() if g['group_id'] == group_id), None)


def save_group(group_id: str | None, name: str, description: str, node_id: str | None, actor: str) -> str:
    name = (name or '').strip()
    if not name or len(name) > 60:
        raise AccessError('a name is required (at most 60 characters)')
    conn = _connect()
    try:
        with conn.cursor() as cur:
            try:
                if group_id:
                    cur.execute('UPDATE config.groups SET name = %s, description = %s WHERE group_id = %s RETURNING group_id',
                                (name, description or None, group_id))
                    if not cur.fetchone():
                        raise AccessError('not found', 404)
                    audit(cur, actor, 'group.update', name, node_id=node_id)
                else:
                    cur.execute('''INSERT INTO config.groups (name, description, node_id, created_by) VALUES (%s, %s, %s, %s)
                                   RETURNING group_id::text AS id''', (name, description or None, node_id, actor))
                    group_id = cur.fetchone()['id']
                    audit(cur, actor, 'group.create', name, node_id=node_id)
            except AccessError:
                raise
            except Exception as exc:  # noqa: BLE001
                if 'duplicate key' in str(exc):
                    raise AccessError(f'there already is a group called {name!r} here', 409) from exc
                raise
        conn.commit()
    finally:
        conn.close()
    invalidate()
    return group_id


def delete_group(group_id: str, actor: str) -> None:
    g = group(group_id)
    if not g:
        raise AccessError('not found', 404)
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute('DELETE FROM config.groups WHERE group_id = %s', (group_id,))
            audit(cur, actor, 'group.delete', g['name'], node_id=g['node_id'], detail={'members': [m['username'] for m in g['members']]})
        conn.commit()
    finally:
        conn.close()
    invalidate()


def set_group_member(group_id: str, user_id: str, member: bool, actor: str) -> None:
    g = group(group_id)
    if not g:
        raise AccessError('not found', 404)
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT username FROM config.users WHERE user_id = %s', (user_id,))
            u = cur.fetchone()
            if not u:
                raise AccessError('user not found', 404)
            if member:
                cur.execute('INSERT INTO config.group_members (group_id, user_id) VALUES (%s, %s) ON CONFLICT DO NOTHING',
                            (group_id, user_id))
            else:
                cur.execute('DELETE FROM config.group_members WHERE group_id = %s AND user_id = %s', (group_id, user_id))
            audit(cur, actor, 'group.member_add' if member else 'group.member_remove', f"{u['username']} in {g['name']}",
                  node_id=g['node_id'])
        conn.commit()
    finally:
        conn.close()
    invalidate()


# ---------------------------------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------------------------------
def users() -> list[dict]:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute('''SELECT u.user_id::text AS user_id, u.username, u.display_name, u.role, u.otp_enabled, u.enabled,
                                  u.awaiting_access, u.awaiting_email, u.email_verified_at, u.created_at,
                                  COALESCE((SELECT json_agg(m.folder_id::text) FROM config.folder_members m WHERE m.user_id = u.user_id), '[]') AS legacy_folders,
                                  COALESCE((SELECT json_agg(gm.group_id::text) FROM config.group_members gm WHERE gm.user_id = u.user_id), '[]') AS groups
                             FROM config.users u ORDER BY lower(u.username)''')
            return [dict(r, created_at=r['created_at'].isoformat() if r['created_at'] else None,
                         email_verified_at=r['email_verified_at'].isoformat() if r['email_verified_at'] else None)
                    for r in cur.fetchall()]
    finally:
        conn.close()


def user(user_id: str) -> dict | None:
    return next((u for u in users() if u['user_id'] == user_id), None)


_EMAIL = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


def normalise_email(v) -> str:
    """An account's login: the email address, trimmed and lowercased; AccessError when it is not one."""
    v = str(v or '').strip().lower()
    if len(v) > 254 or not _EMAIL.match(v):
        raise AccessError('enter a valid email address')
    return v


def create_user(email: str, password: str, display_name: str | None, actor: str, audit_node: str | None = None,
                reveal_taken: bool = False) -> str:
    """A new account, its login the email address. A taken address is refused without saying so to anyone
    but a top-level admin (reveal_taken): below them it would tell that the person is known elsewhere."""
    from werkzeug.security import generate_password_hash
    email = normalise_email(email)
    if len(password or '') < 8:
        raise AccessError('the password needs at least 8 characters')
    conn = _connect()
    try:
        with conn.cursor() as cur:
            try:
                cur.execute('''INSERT INTO config.users (username, password_hash, role, display_name, enabled)
                               VALUES (%s, %s, 'user', %s, TRUE) RETURNING user_id::text AS id''',
                            (email, generate_password_hash(password), (display_name or '').strip() or None))
            except Exception as exc:  # noqa: BLE001
                if 'duplicate key' in str(exc):
                    raise AccessError(f'{email} already has an account' if reveal_taken
                                      else 'this address cannot be used here', 409) from exc
                raise
            uid = cur.fetchone()['id']
            audit(cur, actor, 'user.create', email, node_id=audit_node)
        conn.commit()
    finally:
        conn.close()
    invalidate()
    return uid


def update_user(user_id: str, actor: str, *, enabled=None, display_name=None, email=None, password=None) -> None:
    """enabled, display_name: the account itself; email (+ password): a top-level admin giving an account its
    address (the callers check who may do what)."""
    from werkzeug.security import generate_password_hash
    u = user(user_id)
    if not u:
        raise AccessError('not found', 404)
    conn = _connect()
    try:
        with conn.cursor() as cur:
            if email is not None:
                email = normalise_email(email)
                if u['awaiting_email'] and len(password or '') < 8:
                    # the old password is not carried over to the new login (PLAN-roles, Accounts 7)
                    raise AccessError('set a password of at least 8 characters with the address')
                try:
                    cur.execute('''UPDATE config.users SET username = %s, awaiting_email = FALSE,
                                          enabled = CASE WHEN awaiting_email THEN NOT awaiting_access ELSE enabled END,
                                          updated_at = NOW() WHERE user_id = %s''', (email, user_id))
                except Exception as exc:  # noqa: BLE001
                    if 'duplicate key' in str(exc):
                        raise AccessError(f'{email} already has an account', 409) from exc
                    raise
                if password:
                    cur.execute('UPDATE config.users SET password_hash = %s WHERE user_id = %s',
                                (generate_password_hash(password), user_id))
                audit(cur, actor, 'user.email', email, detail={'from': u['username']})
            if enabled is not None:
                if enabled and u['awaiting_access'] and not _has_assignment(cur, user_id):
                    raise AccessError('give the account its access first', 409)
                if enabled and u['awaiting_email'] and email is None:
                    raise AccessError('give the account its email address first', 409)
                cur.execute('UPDATE config.users SET enabled = %s, updated_at = NOW() WHERE user_id = %s', (bool(enabled), user_id))
                audit(cur, actor, 'user.enable' if enabled else 'user.disable', u['username'])
            if display_name is not None:
                cur.execute('UPDATE config.users SET display_name = %s, updated_at = NOW() WHERE user_id = %s',
                            (display_name.strip()[:120] or None, user_id))
        conn.commit()
    finally:
        conn.close()
    invalidate()


def user_id_by_login(login: str | None) -> str | None:
    """The account a login (address in any capitals, or the main admin's username) names, or None."""
    login = (login or '').strip()
    if not login:
        return None
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT user_id::text AS id FROM config.users WHERE lower(username) = lower(%s)', (login,))
            rows = cur.fetchall()
            return rows[0]['id'] if len(rows) == 1 else None
    finally:
        conn.close()


def create_invited_user(email: str, password: str, display_name: str | None) -> str:
    """The account an invitation creates: the address verified by the link, the password the person chose.
    AccessError when the address has an account by now."""
    from werkzeug.security import generate_password_hash
    email = normalise_email(email)
    conn = _connect()
    try:
        with conn.cursor() as cur:
            try:
                cur.execute('''INSERT INTO config.users (username, password_hash, role, display_name, enabled, email_verified_at)
                               VALUES (%s, %s, 'user', %s, TRUE, NOW()) RETURNING user_id::text AS id''',
                            (email, generate_password_hash(password), (display_name or '').strip()[:120] or None))
            except Exception as exc:  # noqa: BLE001
                if 'duplicate key' in str(exc):
                    raise AccessError('this address has an account', 409) from exc
                raise
            uid = cur.fetchone()['id']
            audit(cur, email, 'user.create', email, detail={'via': 'invitation'})
        conn.commit()
    finally:
        conn.close()
    invalidate()
    return uid


def activate_user(user_id: str, password: str, display_name: str | None) -> None:
    """An account from before E1 confirmed its new address through the link: new password, address verified,
    enabled when it has its access."""
    from werkzeug.security import generate_password_hash
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute('''UPDATE config.users SET password_hash = %s, email_verified_at = NOW(), awaiting_email = FALSE,
                                  enabled = NOT awaiting_access, display_name = COALESCE(%s, display_name), updated_at = NOW()
                            WHERE user_id = %s RETURNING username''', (generate_password_hash(password), display_name, user_id))
            row = cur.fetchone()
            if row:
                audit(cur, row['username'], 'user.activate', row['username'])
        conn.commit()
    finally:
        conn.close()
    invalidate()


def require_email_addresses(main_admin: str | None) -> list[str]:
    """Accounts are email addresses (PLAN-roles, Accounts 1 and 7): every account without one, except the
    main admin from .env, is disabled until a top-level admin enters its address. Runs at start; does
    nothing without a known main admin (so an install can never lock out its last way in)."""
    main_admin = (main_admin or '').strip()
    if not main_admin:
        return []
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT 1 FROM config.users WHERE username = %s', (main_admin,))
            if not cur.fetchone():
                return []
            cur.execute('''UPDATE config.users SET enabled = FALSE, awaiting_email = TRUE
                            WHERE username !~ '^[^@[:space:]]+@[^@[:space:]]+[.][^@[:space:]]+$'
                              AND username <> %s AND NOT awaiting_email RETURNING username''', (main_admin,))
            names = [r['username'] for r in cur.fetchall()]
            for n in names:
                audit(cur, 'upgrade', 'user.awaiting_email', n, detail={'reason': 'accounts are email addresses'})
            if names:
                _bump(cur)
        conn.commit()
    finally:
        conn.close()
    return names


def delete_user(user_id: str, actor: str) -> None:
    u = user(user_id)
    if not u:
        raise AccessError('not found', 404)
    conn = _connect()
    try:
        with conn.cursor() as cur:
            _guard_last_root_admin(cur, removing_user=user_id)
            cur.execute('DELETE FROM config.users WHERE user_id = %s', (user_id,))
            audit(cur, actor, 'user.delete', u['username'])
        conn.commit()
    finally:
        conn.close()
    invalidate()


def _has_assignment(cur, user_id: str) -> bool:
    cur.execute('''SELECT 1 FROM config.assignments WHERE user_id = %s
                   UNION ALL SELECT 1 FROM config.assignments a JOIN config.group_members m ON m.group_id = a.group_id
                    WHERE m.user_id = %s LIMIT 1''', (user_id, user_id))
    return cur.fetchone() is not None


def _guard_last_root_admin(cur, removing_user: str | None = None, removing_assignment: str | None = None) -> None:
    """The top admin can never be locked out: at least one enabled user keeps Admin on the whole install."""
    cur.execute('''SELECT a.assignment_id::text AS aid, a.user_id::text AS uid FROM config.assignments a
                     JOIN config.roles r ON r.role_id = a.role_id
                     JOIN config.users u ON u.user_id = a.user_id
                    WHERE a.scope_kind = 'root' AND r.builtin_key = 'admin' AND u.enabled''')
    left = [r for r in cur.fetchall() if r['uid'] != removing_user and r['aid'] != removing_assignment]
    if not left:
        raise AccessError('this would leave nobody with Admin on the whole installation', 409)


# ---------------------------------------------------------------------------------------------------
# assignments
# ---------------------------------------------------------------------------------------------------
_ASSIGN_SQL = '''SELECT a.assignment_id::text AS assignment_id, a.user_id::text AS user_id, a.group_id::text AS group_id,
                        a.role_id::text AS role_id, r.name AS role_name, r.builtin_key, a.scope_kind, a.node_id::text AS node_id,
                        a.cluster_id::text AS cluster_id, c.cluster_name, a.tag, a.vm_uuid, a.vm_name, a.created_by, a.created_at,
                        u.username, g.name AS group_name
                   FROM config.assignments a
                   JOIN config.roles r ON r.role_id = a.role_id
                   LEFT JOIN config.clusters c ON c.cluster_id = a.cluster_id
                   LEFT JOIN config.users u ON u.user_id = a.user_id
                   LEFT JOIN config.groups g ON g.group_id = a.group_id'''


def assignments(user_id: str | None = None, group_id: str | None = None, cluster_id: str | None = None) -> list[dict]:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            where, params = [], []
            if user_id:
                where.append('a.user_id = %s'); params.append(user_id)
            if group_id:
                where.append('a.group_id = %s'); params.append(group_id)
            if cluster_id:
                where.append('a.cluster_id = %s'); params.append(cluster_id)
            sql = _ASSIGN_SQL + (' WHERE ' + ' AND '.join(where) if where else '') + ' ORDER BY a.created_at'
            cur.execute(sql, params)
            return [dict(r, created_at=r['created_at'].isoformat() if r['created_at'] else None) for r in cur.fetchall()]
    finally:
        conn.close()


def assignment(assignment_id: str) -> dict | None:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(_ASSIGN_SQL + ' WHERE a.assignment_id = %s', (assignment_id,))
            r = cur.fetchone()
            return dict(r, created_at=r['created_at'].isoformat() if r['created_at'] else None) if r else None
    finally:
        conn.close()


def node_label(node_id, tree: rbac.Tree, visible: set | None = None) -> str:
    """A node's path; a single-company install hides its default MSP and customer. With `visible`
    (the reader's rbac.Access.visible_nodes()), the layers above the reader are left out."""
    chain = list(reversed(tree.ancestors(node_id)))
    single = sum(1 for v in tree.level.values() if v == 'msp') <= 1 and sum(1 for v in tree.level.values() if v == 'customer') <= 1
    if single and len(chain) > 1:
        chain = chain[min(2, len(chain) - 1):]
    if visible is not None:
        chain = [n for n in chain if n in visible]
        if not chain:
            return 'a level above yours'
    return ' / '.join(tree.name.get(n, n) for n in chain) or str(node_id)


def assignment_in_reach(acc: rbac.Access, a: dict) -> bool:
    """May this reader manage people on the scope of assignment `a` (users.manage there)?"""
    k = a['scope_kind']
    if k == 'node':
        return acc.can('users.manage', node_id=a['node_id'])
    if k in ('cluster', 'tag', 'vm'):
        return acc.can('users.manage', a['cluster_id'])
    return acc.can('users.manage')                   # root: the whole installation


def manageable_user_ids(acc: rbac.Access) -> set | None:
    """The accounts this reader manages, or None for all (a top-level user manager). An account belongs to
    the person, not to a tenant (PLAN-roles, Accounts 3): a reader manages it when the person holds something
    in their reach, directly or through a group of their reach. Accounts with no access anywhere, and the
    access a person holds elsewhere, are a top-level matter."""
    if acc.superuser or acc.can('users.manage'):
        return None
    out = set()
    group_ok = {g['group_id'] for g in groups() if acc.can('users.manage', node_id=g['node_id'])}
    for a in assignments():
        if a['user_id'] and assignment_in_reach(acc, a):
            out.add(a['user_id'])
    members = {}
    for u in users():
        for gid in u.get('groups') or []:
            members.setdefault(gid, set()).add(u['user_id'])
    for gid in group_ok:
        out |= members.get(gid, set())
    return out


def nameable_usernames(acc: rbac.Access, me: str | None = None) -> set | None:
    """The usernames this reader may see named: themselves and the accounts they manage, or None when they
    see the whole installation. Accounts above them (an MSP's staff, the installation's administrators) are
    never named to them: a customer does not learn which MSP serves it."""
    if acc.superuser or any(g.kind == 'root' for g in acc.grants):
        return None
    ids = manageable_user_ids(acc) or set()
    out = {u['username'] for u in users() if u['user_id'] in ids}
    return out | ({me} if me else set())


def remove_access_in_reach(acc: rbac.Access, user_id: str, actor: str) -> int:
    """'Remove from my tenants': every assignment and group membership of this person within the reader's
    reach goes; what they hold elsewhere stays. Returns how many were removed."""
    n = 0
    for a in assignments(user_id=user_id):
        if assignment_in_reach(acc, a):
            remove_assignment(a['assignment_id'], actor, a['node_id'])
            n += 1
    u = user(user_id) or {}
    for gid in u.get('groups') or []:
        g = group(gid)
        if g and acc.can('users.manage', node_id=g['node_id']):
            set_group_member(gid, user_id, False, actor)
            n += 1
    return n


def hide_names(rows: list, fields, names: set | None) -> list:
    """Replace the names in `fields` that the reader may not see by None, listing them in row['hidden']."""
    if names is None:
        return rows
    for r in rows:
        for f in fields:
            v = r.get(f)
            # every name but the reader's own reach, except a guest's ('guest:…'), who is no account
            if v and v not in names and not str(v).startswith('guest:'):
                r[f] = None
                r.setdefault('hidden', []).append(f)
    return rows


def scope_label(a: dict, tree: rbac.Tree, visible: set | None = None) -> str:
    k = a['scope_kind']
    if k == 'root':
        return 'the whole installation' if visible is None else 'a level above yours'
    if k == 'node':
        return node_label(a['node_id'], tree, visible)
    if k == 'cluster':
        return a.get('cluster_name') or a['cluster_id']
    if k == 'tag':
        return f"tag {a['tag']} on {a.get('cluster_name') or a['cluster_id']}"
    return f"VM {a.get('vm_name') or a['vm_uuid']} on {a.get('cluster_name') or a['cluster_id']}"


def add_assignment(principal: dict, role_id: str, scope: dict, actor: str, audit_node: str | None) -> str:
    """principal: {'user_id'} or {'group_id'}; scope: {'kind', 'node_id'|'cluster_id'|'tag'|'vm_uuid', 'vm_name'}.
    The caller has checked rbac.may_grant; this only stores it (and enables an account waiting for access)."""
    kind = scope['kind']
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute('''INSERT INTO config.assignments (user_id, group_id, role_id, scope_kind, node_id, cluster_id, tag, vm_uuid,
                                                           vm_name, created_by)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING assignment_id::text AS id''',
                        (principal.get('user_id'), principal.get('group_id'), role_id, kind,
                         scope.get('node_id') if kind == 'node' else None,
                         scope.get('cluster_id') if kind in ('cluster', 'tag', 'vm') else None,
                         scope.get('tag') if kind == 'tag' else None,
                         scope.get('vm_uuid') if kind == 'vm' else None,
                         scope.get('vm_name') if kind == 'vm' else None, actor))
            aid = cur.fetchone()['id']
            a = None
            cur.execute(_ASSIGN_SQL + ' WHERE a.assignment_id = %s', (aid,))
            a = cur.fetchone()
            who = a['username'] or f"group {a['group_name']}"
            audit(cur, actor, 'assignment.add', f"{who}: {a['role_name']} on {scope_label(a, load_tree())}", node_id=audit_node,
                  detail={'role': a['role_name'], 'scope': kind})
            if principal.get('user_id'):
                if a['builtin_key'] == 'admin' and kind == 'root':
                    cur.execute("UPDATE config.users SET role = 'admin' WHERE user_id = %s", (principal['user_id'],))
                cur.execute('''UPDATE config.users SET enabled = NOT awaiting_email, awaiting_access = FALSE
                                WHERE user_id = %s AND awaiting_access RETURNING username''', (principal['user_id'],))
                if cur.fetchone():
                    audit(cur, actor, 'user.enable', who, node_id=audit_node, detail={'reason': 'access assigned after the upgrade'})
        conn.commit()
    finally:
        conn.close()
    invalidate()
    return aid


def remove_assignment(assignment_id: str, actor: str, audit_node: str | None) -> None:
    a = assignment(assignment_id)
    if not a:
        raise AccessError('not found', 404)
    conn = _connect()
    try:
        with conn.cursor() as cur:
            if a['scope_kind'] == 'root' and a['builtin_key'] == 'admin':
                _guard_last_root_admin(cur, removing_assignment=assignment_id)
                if a['user_id']:
                    cur.execute("UPDATE config.users SET role = 'user' WHERE user_id = %s", (a['user_id'],))
            cur.execute('DELETE FROM config.assignments WHERE assignment_id = %s', (assignment_id,))
            who = a['username'] or f"group {a['group_name']}"
            audit(cur, actor, 'assignment.remove', f"{who}: {a['role_name']} on {scope_label(a, load_tree())}", node_id=audit_node)
        conn.commit()
    finally:
        conn.close()
    invalidate()


def scoped_tags(cluster_id: str) -> dict[str, list[str]]:
    """Tags on this cluster that some assignment depends on: {tag: ['user: Role', 'group G: Role', ...]}."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            try:
                cur.execute('''SELECT a.tag, COALESCE(u.username, 'group ' || g.name) AS who, r.name AS role
                                 FROM config.assignments a JOIN config.roles r ON r.role_id = a.role_id
                                 LEFT JOIN config.users u ON u.user_id = a.user_id
                                 LEFT JOIN config.groups g ON g.group_id = a.group_id
                                WHERE a.scope_kind = 'tag' AND a.cluster_id = %s''', (cluster_id,))
            except Exception as exc:  # noqa: BLE001
                if _missing(exc):
                    return {}
                raise
            out: dict[str, list[str]] = {}
            for r in cur.fetchall():
                out.setdefault(r['tag'], []).append(f"{r['who']}: {r['role']}")
            return out
    finally:
        conn.close()


def grants_of_principal(user_id: str | None = None, group_id: str | None = None) -> list[rbac.Grant]:
    """The grants of one user (direct and through groups) or one group, for 'who has access' views."""
    rows = []
    conn = _connect()
    try:
        with conn.cursor() as cur:
            if user_id:
                cur.execute(_GRANT_SQL, {'u': user_id})
            else:
                cur.execute('''SELECT a.scope_kind, a.node_id::text AS node_id, a.cluster_id::text AS cluster_id, a.tag, a.vm_uuid,
                                      r.builtin_key, r.permissions
                                 FROM config.assignments a JOIN config.roles r ON r.role_id = a.role_id WHERE a.group_id = %s''',
                            (group_id,))
            rows = cur.fetchall()
    finally:
        conn.close()
    return [_grant_of(r) for r in rows]


# ---------------------------------------------------------------------------------------------------
# permissions introduced by later versions
# ---------------------------------------------------------------------------------------------------
def sync_known_permissions() -> list[str]:
    """Record the permissions this version knows. On the first run every one counts as known; after that
    a permission that appears (a newer version) is 'new' until a role manager acknowledges it."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            try:
                cur.execute('SELECT permission FROM config.permissions_known')
            except Exception as exc:  # noqa: BLE001
                if _missing(exc):
                    return []
                raise
            known = {r['permission'] for r in cur.fetchall()}
            first = not known
            new = sorted(set(rbac.PERMISSIONS) - known)
            for p in new:
                cur.execute('INSERT INTO config.permissions_known (permission, acknowledged) VALUES (%s, %s) ON CONFLICT DO NOTHING',
                            (p, first))
        conn.commit()
        return [] if first else new
    finally:
        conn.close()


def unacknowledged_permissions() -> list[str]:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            try:
                cur.execute('SELECT permission FROM config.permissions_known WHERE NOT acknowledged ORDER BY permission')
            except Exception as exc:  # noqa: BLE001
                if _missing(exc):
                    return []
                raise
            return [r['permission'] for r in cur.fetchall() if r['permission'] in rbac.PERMISSIONS]
    finally:
        conn.close()


def acknowledge_permissions(actor: str) -> None:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute('UPDATE config.permissions_known SET acknowledged = TRUE WHERE NOT acknowledged RETURNING permission')
            done = [r['permission'] for r in cur.fetchall()]
            if done:
                audit(cur, actor, 'permissions.acknowledge', ', '.join(done))
        conn.commit()
    finally:
        conn.close()
