"""
access_db.py — loads the rows rbac.py reasons about: the tenant tree and a user's grants.

Kept apart from rbac.py so the rules stay pure (and unit-testable) and the SQL stays in one place.
Everything here is cached briefly per process (a page load fires a dozen API calls); `invalidate()`
drops the caches after any change to the tree, roles, groups, assignments or users.
"""
from __future__ import annotations

import logging
import threading
import time

import rbac

log = logging.getLogger(__name__)

_TTL = 10.0
_lock = threading.Lock()
_tree_cache: dict = {'at': 0.0, 'tree': None}
_grants_cache: dict[str, tuple[float, list]] = {}


def invalidate() -> None:
    with _lock:
        _tree_cache['at'] = 0.0
        _grants_cache.clear()
        _state_cache.clear()


def _connect():
    import user_db
    return user_db._connect()


def load_tree() -> rbac.Tree:
    now = time.monotonic()
    with _lock:
        if _tree_cache['tree'] is not None and now - _tree_cache['at'] < _TTL:
            return _tree_cache['tree']
    tree = rbac.Tree()
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT folder_id::text AS id, name FROM config.folders')
            for r in cur.fetchall():
                tree.parent[r['id']] = None
                tree.level[r['id']] = 'folder'
                tree.name[r['id']] = r['name']
            cur.execute("SELECT cluster_id::text AS id, folder_id::text AS folder FROM config.clusters "
                        "WHERE cluster_id <> '00000000-0000-0000-0000-000000000001'")
            for r in cur.fetchall():
                tree.cluster_folder[r['id']] = r['folder']
    finally:
        conn.close()
    with _lock:
        _tree_cache.update(at=now, tree=tree)
    return tree


def load_grants(user_id: str) -> list[rbac.Grant]:
    """A user's grants. Before RBAC's migration: the legacy model (admin = everything everywhere;
    a user = LEGACY_USER on the folders they are a member of)."""
    now = time.monotonic()
    with _lock:
        hit = _grants_cache.get(user_id)
        if hit and now - hit[0] < _TTL:
            return hit[1]
    grants: list[rbac.Grant] = []
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT role FROM config.users WHERE user_id = %s', (user_id,))
            row = cur.fetchone()
            if row is None:
                grants = []
            elif row['role'] == 'admin':
                grants = [rbac.Grant(rbac.ALL, 'root')]
            else:
                cur.execute('SELECT folder_id::text AS id FROM config.folder_members WHERE user_id = %s', (user_id,))
                grants = [rbac.Grant(rbac.LEGACY_USER, 'node', node_id=r['id']) for r in cur.fetchall()]
    finally:
        conn.close()
    with _lock:
        _grants_cache[user_id] = (now, grants)
    return grants


_state_cache: dict[str, tuple[float, dict | None]] = {}


def user_state(user_id: str) -> dict | None:
    """{username, enabled} of a user id, or None when the user no longer exists (cached briefly: every
    API call asks)."""
    now = time.monotonic()
    with _lock:
        hit = _state_cache.get(user_id)
        if hit and now - hit[0] < _TTL:
            return hit[1]
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT username FROM config.users WHERE user_id = %s', (user_id,))
            row = cur.fetchone()
    finally:
        conn.close()
    state = {'username': row['username'], 'enabled': True} if row else None
    with _lock:
        _state_cache[user_id] = (now, state)
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
