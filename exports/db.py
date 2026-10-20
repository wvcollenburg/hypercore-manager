"""
db.py — PostgreSQL persistence for the Exports module (schema `exports`).

Rows come back as plain dicts. Share passwords are stored Fernet-encrypted
(hcm_crypto) and decrypted only by the job that needs them.
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Optional

import psycopg2
import psycopg2.extras

import hcm_crypto

log = logging.getLogger('exports.db')


def _dsn() -> dict:
    return dict(
        host=os.getenv('PG_HOST') or os.getenv('SENTINEL_PG_HOST', 'postgres'),
        port=int(os.getenv('PG_PORT', '5432')),
        dbname=os.getenv('PG_DB', 'hypercore_balancer'),
        user=os.getenv('PG_USER', 'sentinel'),
        password=os.getenv('PG_PASSWORD') or os.getenv('SENTINEL_PG_PASSWORD', ''),
        connect_timeout=10,
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


@contextmanager
def connect():
    conn = psycopg2.connect(**_dsn())
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _one(sql: str, params=()) -> Optional[dict]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            r = cur.fetchone()
            return dict(r) if r else None


def _all(sql: str, params=()) -> list[dict]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]


def _exec(sql: str, params=()) -> int:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.rowcount


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def encrypt_password(plain: str) -> str:
    return hcm_crypto.encrypt(plain)


def decrypt_password(token: str) -> str:
    return hcm_crypto.decrypt(token)


# ------------------------------------------------------------------ schedules
_SCHED_SELECT = '''SELECT s.*, s.cluster_id::text AS cluster_id, c.cluster_name, c.hc_host AS cluster_host,
                          c.enabled AS cluster_enabled
                     FROM exports.schedules s JOIN config.clusters c ON c.cluster_id = s.cluster_id'''


def add_schedule(cluster_id: str, vm_uuid: str, vm_name: str, path_uri_base: str, prune_path: str,
                 retention: int, run_time: str, compress: bool, smb_user: str = None, smb_password: str = None) -> int:
    smb_user = (smb_user or '').strip() or None
    smb_pass_enc = encrypt_password(smb_password) if smb_password else None
    row = _one(
        '''INSERT INTO exports.schedules (cluster_id, vm_uuid, vm_name, path_uri_base, smb_user, smb_pass_enc,
                                          prune_path, retention, run_time, compress)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
        (cluster_id, vm_uuid, vm_name, path_uri_base.rstrip('/'), smb_user, smb_pass_enc,
         (prune_path or '').rstrip('/') or None, int(retention), run_time, bool(compress)),
    )
    return row['id']


def update_schedule(schedule_id: int, **fields) -> None:
    allowed = {'path_uri_base', 'smb_user', 'prune_path', 'retention', 'run_time', 'compress', 'enabled'}
    sets, params = [], []
    for k, v in fields.items():
        if k == 'smb_password':
            if v:
                sets.append('smb_pass_enc = %s'); params.append(encrypt_password(v))
            continue
        if k not in allowed:
            continue
        if k in ('path_uri_base', 'prune_path') and isinstance(v, str):
            v = v.rstrip('/') or None
        if k == 'smb_user' and isinstance(v, str):
            v = v.strip() or None
        sets.append(f'{k} = %s'); params.append(v)
    if not sets:
        return
    sets.append('updated_at = NOW()'); params.append(schedule_id)
    _exec(f'UPDATE exports.schedules SET {", ".join(sets)} WHERE id = %s', params)


def get_schedules(cluster_id: Optional[str] = None) -> list[dict]:
    if cluster_id:
        return _all(_SCHED_SELECT + ' WHERE s.cluster_id = %s ORDER BY s.vm_name', (cluster_id,))
    return _all(_SCHED_SELECT + ' ORDER BY c.cluster_name, s.vm_name')


def get_schedule(schedule_id: int) -> Optional[dict]:
    return _one(_SCHED_SELECT + ' WHERE s.id = %s', (schedule_id,))


def toggle_schedule(schedule_id: int) -> None:
    _exec('UPDATE exports.schedules SET enabled = NOT enabled, updated_at = NOW() WHERE id = %s', (schedule_id,))


def delete_schedule(schedule_id: int) -> None:
    _exec('DELETE FROM exports.schedules WHERE id = %s', (schedule_id,))


def get_known_smb_locations() -> list[dict]:
    """Distinct SMB destinations (base + user) saved on schedules, across all clusters, so
    an import can reuse them (cross-cluster restore) without re-entering credentials.
    ref_id is the newest schedule holding the stored password for that location."""
    return _all(
        '''SELECT path_uri_base, smb_user, MAX(id) AS ref_id
             FROM exports.schedules
            WHERE path_uri_base ILIKE 'smb://%%' AND smb_user IS NOT NULL
            GROUP BY path_uri_base, smb_user ORDER BY path_uri_base''')


# ----------------------------------------------------------------------- runs
def start_run(schedule_id: int) -> int:
    return _one('INSERT INTO exports.runs (schedule_id) VALUES (%s) RETURNING id', (schedule_id,))['id']


def update_run(run_id: int, **fields) -> None:
    allowed = {'finished_at', 'status', 'task_tag', 'export_path', 'message'}
    items = [(k, v) for k, v in fields.items() if k in allowed]
    if not items:
        return
    _exec(f'UPDATE exports.runs SET {", ".join(f"{k} = %s" for k, _ in items)} WHERE id = %s',
          [v for _, v in items] + [run_id])


def get_runs(cluster_id: Optional[str] = None, schedule_id: Optional[int] = None, limit: int = 50) -> list[dict]:
    sql = '''SELECT r.*, s.vm_name, s.cluster_id::text AS cluster_id, c.cluster_name
               FROM exports.runs r JOIN exports.schedules s ON s.id = r.schedule_id
               JOIN config.clusters c ON c.cluster_id = s.cluster_id'''
    where, params = [], []
    if schedule_id:
        where.append('r.schedule_id = %s'); params.append(schedule_id)
    if cluster_id:
        where.append('s.cluster_id = %s'); params.append(cluster_id)
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    sql += ' ORDER BY r.id DESC LIMIT %s'; params.append(limit)
    return _all(sql, params)


def get_run(run_id: int) -> Optional[dict]:
    return _one('SELECT * FROM exports.runs WHERE id = %s', (run_id,))


def last_run_for(schedule_id: int) -> Optional[dict]:
    return _one('SELECT * FROM exports.runs WHERE schedule_id = %s ORDER BY id DESC LIMIT 1', (schedule_id,))


def get_active_runs() -> list[dict]:
    return _all("SELECT * FROM exports.runs WHERE status = 'RUNNING' ORDER BY id")


def is_run_active(schedule_id: int) -> bool:
    row = _one("SELECT COUNT(*) AS n FROM exports.runs WHERE schedule_id = %s AND status = 'RUNNING'", (schedule_id,))
    return bool(row and row['n'])


# -------------------------------------------------------------------- imports
def add_import(cluster_id: str, source_uri: str, smb_user: str, smb_password: str, target_name: str) -> int:
    smb_user = (smb_user or '').strip() or None
    smb_pass_enc = encrypt_password(smb_password) if smb_password else None
    return _one(
        '''INSERT INTO exports.imports (cluster_id, source_uri, smb_user, smb_pass_enc, target_name)
           VALUES (%s,%s,%s,%s,%s) RETURNING id''',
        (cluster_id, source_uri.rstrip('/'), smb_user, smb_pass_enc, (target_name or '').strip() or None))['id']


def get_import(import_id: int) -> Optional[dict]:
    return _one('''SELECT i.*, i.cluster_id::text AS cluster_id, c.cluster_name FROM exports.imports i
                   JOIN config.clusters c ON c.cluster_id = i.cluster_id WHERE i.id = %s''', (import_id,))


def get_imports(cluster_id: Optional[str] = None, limit: int = 50) -> list[dict]:
    sql = '''SELECT i.*, i.cluster_id::text AS cluster_id, c.cluster_name FROM exports.imports i
             JOIN config.clusters c ON c.cluster_id = i.cluster_id'''
    params: list[Any] = []
    if cluster_id:
        sql += ' WHERE i.cluster_id = %s'; params.append(cluster_id)
    sql += ' ORDER BY i.id DESC LIMIT %s'; params.append(limit)
    return _all(sql, params)


def update_import(import_id: int, **fields) -> None:
    allowed = {'finished_at', 'status', 'task_tag', 'created_uuid', 'message'}
    items = [(k, v) for k, v in fields.items() if k in allowed]
    if not items:
        return
    _exec(f'UPDATE exports.imports SET {", ".join(f"{k} = %s" for k, _ in items)} WHERE id = %s',
          [v for _, v in items] + [import_id])


def get_active_imports() -> list[dict]:
    return _all("SELECT * FROM exports.imports WHERE status = 'RUNNING' ORDER BY id")


# --------------------------------------------------------------------- shares
def _share_row(r: Optional[dict]) -> Optional[dict]:
    if r is None:
        return None
    d = dict(r)
    d['has_password'] = bool(d.pop('password_enc', None))
    return d


def get_shares() -> list[dict]:
    rows = _all('''SELECT s.*, (SELECT COUNT(*) FROM config.folders f WHERE f.share_id = s.id) AS folder_count
                     FROM exports.shares s ORDER BY lower(s.name)''')
    return [_share_row(r) for r in rows]


def get_share(share_id: int, with_secret: bool = False) -> Optional[dict]:
    r = _one('SELECT * FROM exports.shares WHERE id = %s', (share_id,))
    if r is None:
        return None
    if with_secret:
        d = dict(r)
        d['password'] = decrypt_password(d['password_enc']) if d.get('password_enc') else ''
        return d
    return _share_row(r)


def add_share(name: str, uri_base: str, username: str = None, password: str = None, prune_path: str = None) -> int:
    return _one(
        '''INSERT INTO exports.shares (name, uri_base, username, password_enc, prune_path)
           VALUES (%s,%s,%s,%s,%s) RETURNING id''',
        (name.strip(), uri_base.strip().rstrip('/'), (username or '').strip() or None,
         encrypt_password(password) if password else None, (prune_path or '').strip().rstrip('/') or None))['id']


def update_share(share_id: int, **fields) -> None:
    sets, params = [], []
    for k, v in fields.items():
        if k == 'password':
            if v:
                sets.append('password_enc = %s'); params.append(encrypt_password(v))
            continue
        if k not in ('name', 'uri_base', 'username', 'prune_path'):
            continue
        if isinstance(v, str):
            v = v.strip().rstrip('/') if k in ('uri_base', 'prune_path') else v.strip()
            if k != 'name':
                v = v or None
        sets.append(f'{k} = %s'); params.append(v)
    if not sets:
        return
    sets.append('updated_at = NOW()'); params.append(share_id)
    _exec(f'UPDATE exports.shares SET {", ".join(sets)} WHERE id = %s', params)


def delete_share(share_id: int) -> None:
    _exec('DELETE FROM exports.shares WHERE id = %s', (share_id,))


def set_default_share(share_id: Optional[int]) -> None:
    """Make *share_id* the share for clusters without a folder (None clears it)."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute('UPDATE exports.shares SET default_for_unfiled = FALSE WHERE default_for_unfiled')
            if share_id:
                cur.execute('UPDATE exports.shares SET default_for_unfiled = TRUE, updated_at = NOW() WHERE id = %s', (share_id,))


def share_for_cluster(cluster_id: str) -> Optional[dict]:
    """The share for a cluster (with decrypted password): the one assigned to its folder, or
    for a cluster without a folder the share flagged default_for_unfiled. None otherwise."""
    r = _one('''SELECT s.* FROM config.clusters c
                  LEFT JOIN config.folders f ON f.folder_id = c.folder_id
                  JOIN exports.shares s
                    ON s.id = CASE WHEN c.folder_id IS NULL
                                   THEN (SELECT id FROM exports.shares WHERE default_for_unfiled LIMIT 1)
                                   ELSE f.share_id END
                 WHERE c.cluster_id = %s''', (cluster_id,))
    if r is None:
        return None
    d = dict(r)
    d['password'] = decrypt_password(d['password_enc']) if d.get('password_enc') else ''
    return d
