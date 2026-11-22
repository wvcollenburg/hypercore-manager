"""
db.py — PostgreSQL persistence for the Backup module (schema `backup`).

Rows come back as plain dicts. Share passwords are stored Fernet-encrypted
(hcm_crypto) and decrypted only by the job that needs them.
"""
from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Optional

import psycopg2
import psycopg2.extras

import hcm_crypto

log = logging.getLogger('backup.db')


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


_CONNECT_RETRY_DELAYS = (2, 5, 10, 15)   # seconds; rides out a postgres stall while a proxy copy hammers the disk


def _open() -> psycopg2.extensions.connection:
    """Connect, retrying only the connect itself: no statement has run yet,
    so a retry can never apply a write twice."""
    for delay in _CONNECT_RETRY_DELAYS:
        try:
            return psycopg2.connect(**_dsn())
        except psycopg2.OperationalError as e:
            log.warning('postgres connect failed (%s), retrying in %ss', str(e).strip(), delay)
            time.sleep(delay)
    return psycopg2.connect(**_dsn())


@contextmanager
def connect():
    conn = _open()
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
                          c.enabled AS cluster_enabled, c.timezone AS cluster_timezone,
                          c.folder_id::text AS cluster_folder_id, pol.name AS policy_name
                     FROM backup.plans s JOIN config.clusters c ON c.cluster_id = s.cluster_id
                     LEFT JOIN backup.policies pol ON pol.id = s.policy_id'''


def add_schedule(cluster_id: str, vm_uuid: str, vm_name: str, path_uri_base: str, prune_path: str,
                 retention: int, run_time: str, compress: bool, smb_user: str = None, smb_password: str = None,
                 mode: str = 'export', full_every_n: int = 14, retention_days: Optional[int] = None) -> int:
    smb_user = (smb_user or '').strip() or None
    smb_pass_enc = encrypt_password(smb_password) if smb_password else None
    row = _one(
        '''INSERT INTO backup.plans (cluster_id, vm_uuid, vm_name, path_uri_base, smb_user, smb_pass_enc,
                                          prune_path, retention, run_time, compress, mode, full_every_n, retention_days)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
        (cluster_id, vm_uuid, vm_name, (path_uri_base or '').rstrip('/') or None, smb_user, smb_pass_enc,
         (prune_path or '').rstrip('/') or None, int(retention), run_time, bool(compress),
         mode, int(full_every_n), retention_days),
    )
    return row['id']


def update_schedule(schedule_id: int, **fields) -> None:
    allowed = {'path_uri_base', 'smb_user', 'prune_path', 'retention', 'run_time', 'compress', 'enabled', 'mode',
               'full_every_n', 'retention_days', 'policy_id', 'force_full'}
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
    _exec(f'UPDATE backup.plans SET {", ".join(sets)} WHERE id = %s', params)


def _cluster_in(column: str, cluster_id) -> tuple[str, Any]:
    """A WHERE clause for one cluster id or a list of them (a folder's clusters)."""
    if isinstance(cluster_id, (list, tuple, set)):
        return f'{column} = ANY(%s::uuid[])', list(cluster_id)
    return f'{column} = %s', cluster_id


def get_schedules(cluster_id=None) -> list[dict]:
    """Per-VM plans of one cluster, of a list of clusters, or (None) of all."""
    if cluster_id:
        clause, param = _cluster_in('s.cluster_id', cluster_id)
        return _all(_SCHED_SELECT + f' WHERE {clause} ORDER BY c.cluster_name, s.vm_name', (param,))
    return _all(_SCHED_SELECT + ' ORDER BY c.cluster_name, s.vm_name')


def get_schedule(schedule_id: int) -> Optional[dict]:
    return _one(_SCHED_SELECT + ' WHERE s.id = %s', (schedule_id,))


def toggle_schedule(schedule_id: int) -> None:
    _exec('UPDATE backup.plans SET enabled = NOT enabled, updated_at = NOW() WHERE id = %s', (schedule_id,))


def delete_schedule(schedule_id: int) -> None:
    """Removes the plan; its chains are retired first so the backups stay (see retire_chains_of_plan)."""
    retire_chains_of_plan(schedule_id)
    _exec('DELETE FROM backup.plans WHERE id = %s', (schedule_id,))


def get_known_smb_locations() -> list[dict]:
    """Distinct SMB destinations (base + user) saved on schedules, across all clusters, so
    an import can reuse them (cross-cluster restore) without re-entering credentials.
    ref_id is the newest schedule holding the stored password for that location."""
    return _all(
        '''SELECT path_uri_base, smb_user, MAX(id) AS ref_id
             FROM backup.plans
            WHERE path_uri_base ILIKE 'smb://%%' AND smb_user IS NOT NULL
            GROUP BY path_uri_base, smb_user ORDER BY path_uri_base''')


# ----------------------------------------------------------------------- runs
def start_run(schedule_id: int, policy_id: Optional[int] = None, occurrence=None) -> int:
    return _one('INSERT INTO backup.runs (schedule_id, policy_id, occurrence) VALUES (%s,%s,%s) RETURNING id',
                (schedule_id, policy_id, occurrence))['id']


def update_run(run_id: int, **fields) -> None:
    allowed = {'finished_at', 'status', 'task_tag', 'export_path', 'message', 'mode', 'point_kind', 'chain_id', 'point_id',
               'proxy_id', 'blocks_changed', 'bytes_written', 'work_started_at', 'lock_wait_seconds', 'copy_seconds',
               'overhead_seconds', 'notice', 'synth_seconds', 'planned_start', 'predicted_finish', 'progress'}
    items = [(k, _json(v) if k == 'progress' else v) for k, v in fields.items() if k in allowed]
    if not items:
        return
    _exec(f'UPDATE backup.runs SET {", ".join(f"{k} = %s" for k, _ in items)} WHERE id = %s',
          [v for _, v in items] + [run_id])


def get_runs(cluster_id: Optional[str] = None, schedule_id: Optional[int] = None, limit: int = 50) -> list[dict]:
    sql = '''SELECT r.*, s.vm_name, s.vm_uuid, s.cluster_id::text AS cluster_id, c.cluster_name
               FROM backup.runs r JOIN backup.plans s ON s.id = r.schedule_id
               JOIN config.clusters c ON c.cluster_id = s.cluster_id'''
    where, params = [], []
    if schedule_id:
        where.append('r.schedule_id = %s'); params.append(schedule_id)
    if cluster_id:
        clause, param = _cluster_in('s.cluster_id', cluster_id)
        where.append(clause); params.append(param)
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    sql += ' ORDER BY r.id DESC LIMIT %s'; params.append(limit)
    return _all(sql, params)


def get_run(run_id: int) -> Optional[dict]:
    return _one('SELECT * FROM backup.runs WHERE id = %s', (run_id,))


def last_run_for(schedule_id: int) -> Optional[dict]:
    return _one('SELECT * FROM backup.runs WHERE schedule_id = %s ORDER BY id DESC LIMIT 1', (schedule_id,))


def get_active_runs() -> list[dict]:
    return _all("SELECT * FROM backup.runs WHERE status = 'RUNNING' ORDER BY id")


def is_run_active(schedule_id: int) -> bool:
    row = _one("SELECT COUNT(*) AS n FROM backup.runs WHERE schedule_id = %s AND status = 'RUNNING'", (schedule_id,))
    return bool(row and row['n'])


def count_active_runs(cluster_id: str, mode: str, exclude_plans=()) -> int:
    row = _one('''SELECT COUNT(*) AS n FROM backup.runs r JOIN backup.plans s ON s.id = r.schedule_id
                  WHERE s.cluster_id = %s AND r.status = 'RUNNING' AND COALESCE(r.mode, s.mode) = %s
                    AND NOT (s.id = ANY(%s))''', (cluster_id, mode, [int(x) for x in exclude_plans]))
    return int(row['n']) if row else 0


# ------------------------------------------------------------------- policies
_POLICY_FIELDS = ('name', 'kind', 'enabled', 'timezone', 'rrule', 'starts_on', 'window_open', 'preferred_finish',
                  'buffer_minutes', 'blackouts', 'retention', 'retention_days', 'full_every_n', 'compress',
                  'full_rrule', 'full_kind')
# the plan settings a policy decides for its members
_POLICY_PLAN_FIELDS = ('retention', 'retention_days', 'full_every_n', 'compress')


def folder_of_cluster(cluster_id: str) -> tuple[bool, Optional[str]]:
    """(known, folder_id) of a registered cluster; folder_id None = no folder."""
    r = _one('SELECT folder_id::text AS folder_id FROM config.clusters WHERE cluster_id = %s', (cluster_id,))
    return (r is not None, r['folder_id'] if r else None)


def folder_name(folder_id: str) -> Optional[str]:
    r = _one('SELECT name FROM config.folders WHERE folder_id = %s', (folder_id,))
    return r['name'] if r else None


def folder_backup_dir(folder_id: str, new_dir: str) -> Optional[str]:
    """The folder's directory on its target (migration 055). A folder without one gets new_dir, stored
    once (COALESCE: a parallel first use keeps whichever was written first). None = no such folder."""
    r = _one('SELECT backup_dir FROM config.folders WHERE folder_id = %s', (folder_id,))
    if r is None:
        return None
    if r['backup_dir']:
        return r['backup_dir']
    r = _one('UPDATE config.folders SET backup_dir = COALESCE(backup_dir, %s) WHERE folder_id = %s RETURNING backup_dir',
             (new_dir, folder_id))
    return r['backup_dir'] if r else None


def _policy_row(r):
    if r is None:
        return None
    d = dict(r)
    d['folder_id'] = str(d['folder_id']) if d.get('folder_id') else None
    d['blackouts'] = d.get('blackouts') or []
    return d


def get_policies(folder_id: Optional[str] = None, all_folders: bool = False, enabled_only: bool = False) -> list[dict]:
    where, params = [], []
    if not all_folders:
        if folder_id:
            where.append('folder_id = %s'); params.append(folder_id)
        else:
            where.append('folder_id IS NULL')
    if enabled_only:
        where.append('enabled')
    sql = 'SELECT * FROM backup.policies' + (' WHERE ' + ' AND '.join(where) if where else '') + ' ORDER BY lower(name), id'
    return [_policy_row(r) for r in _all(sql, params)]


def get_policy(policy_id: int) -> Optional[dict]:
    return _policy_row(_one('SELECT * FROM backup.policies WHERE id = %s', (policy_id,)))


def add_policy(folder_id: Optional[str], **fields) -> int:
    cols = ['folder_id'] + [k for k in _POLICY_FIELDS if k in fields]
    vals = [folder_id] + [_json(fields[k]) if k == 'blackouts' else fields[k] for k in cols[1:]]
    return _one(f'''INSERT INTO backup.policies ({", ".join(cols)}) VALUES ({", ".join(["%s"] * len(cols))}) RETURNING id''',
                vals)['id']


def update_policy(policy_id: int, **fields) -> None:
    items = [(k, _json(v) if k == 'blackouts' else v) for k, v in fields.items() if k in _POLICY_FIELDS and k != 'kind']
    if not items:
        return
    _exec(f'''UPDATE backup.policies SET {", ".join(f"{k} = %s" for k, _ in items)}, updated_at = NOW() WHERE id = %s''',
          [v for _, v in items] + [policy_id])
    apply_policy(policy_id)


def apply_policy(policy_id: int) -> int:
    """Copy the policy's plan settings onto its members (their chains and retention read the plan)."""
    sets = ', '.join(f'{k} = p.{k}' for k in _POLICY_PLAN_FIELDS)          # SET takes bare column names
    return _exec(f'''UPDATE backup.plans s SET {sets}, mode = p.kind, updated_at = NOW()
                       FROM backup.policies p WHERE p.id = s.policy_id AND p.id = %s''', (policy_id,))


def delete_policy(policy_id: int) -> None:
    """Members stay as paused plans of their own (chains kept), like removing them one by one."""
    _exec('UPDATE backup.plans SET policy_id = NULL, enabled = FALSE, updated_at = NOW() WHERE policy_id = %s', (policy_id,))
    _exec('DELETE FROM backup.policies WHERE id = %s', (policy_id,))


def toggle_policy(policy_id: int) -> None:
    _exec('UPDATE backup.policies SET enabled = NOT enabled, updated_at = NOW() WHERE id = %s', (policy_id,))


def policy_members(policy_id: int) -> list[dict]:
    return _all(_SCHED_SELECT + ' WHERE s.policy_id = %s ORDER BY c.cluster_name, s.vm_name', (policy_id,))


def plan_for_vm(cluster_id: str, vm_uuid: str) -> Optional[dict]:
    return _one(_SCHED_SELECT + ' WHERE s.cluster_id = %s AND s.vm_uuid = %s', (cluster_id, vm_uuid))


def assign_plan(plan_id: int, policy_id: int) -> None:
    """The newest assignment wins: the plan joins the policy (leaving any other) and takes its settings."""
    _exec('UPDATE backup.plans SET policy_id = %s, enabled = TRUE, updated_at = NOW() WHERE id = %s', (policy_id, plan_id))
    apply_policy(policy_id)


def release_plan(plan_id: int) -> None:
    _exec('UPDATE backup.plans SET policy_id = NULL, enabled = FALSE, updated_at = NOW() WHERE id = %s', (plan_id,))


def release_foreign_members(policy: dict) -> list[dict]:
    """Members whose cluster is no longer in the policy's folder leave it (paused, chain kept)."""
    return _all('''UPDATE backup.plans s SET policy_id = NULL, enabled = FALSE, updated_at = NOW()
                      FROM config.clusters c
                     WHERE c.cluster_id = s.cluster_id AND s.policy_id = %s AND c.folder_id IS DISTINCT FROM %s::uuid
                 RETURNING s.id, s.vm_name''', (policy['id'], policy.get('folder_id')))


def plans_run_in_occurrence(policy_id: int, occurrence) -> set:
    return {r['schedule_id'] for r in _all('SELECT DISTINCT schedule_id FROM backup.runs WHERE policy_id = %s AND occurrence = %s',
                                           (policy_id, occurrence))}


def occurrence_summary(policy_id: int, occurrence) -> dict:
    rows = _all('''SELECT status, COUNT(*) AS n, COUNT(notice) AS notices, MAX(finished_at) AS last_finished
                    FROM backup.runs WHERE policy_id = %s AND occurrence = %s GROUP BY status''', (policy_id, occurrence))
    out = {'by_status': {r['status']: int(r['n']) for r in rows}, 'notices': sum(int(r['notices']) for r in rows),
           'last_finished': max((r['last_finished'] for r in rows if r['last_finished']), default=None)}
    return out


def last_occurrence(policy_id: int):
    r = _one('SELECT MAX(occurrence) AS occ FROM backup.runs WHERE policy_id = %s', (policy_id,))
    return r['occ'] if r else None


def last_active_seconds(plan_ids: list[int]) -> dict:
    """Per plan: the active time of its newest finished run (copy + overhead), for ordering."""
    if not plan_ids:
        return {}
    rows = _all('''SELECT DISTINCT ON (schedule_id) schedule_id, COALESCE(copy_seconds, 0) + COALESCE(overhead_seconds, 0) AS s
                    FROM backup.runs WHERE schedule_id = ANY(%s) AND status = 'COMPLETE' AND copy_seconds IS NOT NULL
                   ORDER BY schedule_id, id DESC''', (list(plan_ids),))
    return {r['schedule_id']: float(r['s']) for r in rows}


_HISTORY_COLS = '''r.id, r.schedule_id, r.status, r.mode, r.point_kind, r.started_at, r.work_started_at, r.blocks_changed,
                   r.bytes_written, r.copy_seconds, r.overhead_seconds, r.synth_seconds, r.proxy_id'''


def run_history(plan_ids: list[int], days: int = 90, per_plan: int = 30) -> dict:
    """Plan id -> its newest finished runs (newest first), what predictions learn from."""
    if not plan_ids:
        return {}
    rows = _all(f'''SELECT * FROM (SELECT {_HISTORY_COLS}, ROW_NUMBER() OVER (PARTITION BY r.schedule_id ORDER BY r.id DESC) AS n
                                     FROM backup.runs r WHERE r.schedule_id = ANY(%s) AND r.status = 'COMPLETE'
                                      AND r.started_at > NOW() - make_interval(days => %s)) x
                    WHERE n <= %s ORDER BY schedule_id, id DESC''', ([int(x) for x in plan_ids], int(days), int(per_plan)))
    out: dict = {}
    for r in rows:
        out.setdefault(r['schedule_id'], []).append(r)
    return out


def cluster_run_history(cluster_id: str, limit: int = 60) -> list[dict]:
    """The cluster's newest finished incremental runs (any plan), for its read rate."""
    return _all(f'''SELECT {_HISTORY_COLS} FROM backup.runs r JOIN backup.plans s ON s.id = r.schedule_id
                     WHERE s.cluster_id = %s AND r.status = 'COMPLETE' AND r.copy_seconds IS NOT NULL
                       AND COALESCE(r.mode, s.mode) = 'incremental'
                     ORDER BY r.id DESC LIMIT %s''', (cluster_id, int(limit)))


def save_night(policy_id: int, occurrence, items: list[dict]) -> None:
    with connect() as conn:
        with conn.cursor() as cur:
            for i in items:
                cur.execute('''INSERT INTO backup.policy_nights (policy_id, occurrence, plan_id, cluster_id, slot, planned_start,
                                                                  predicted_finish, predicted_seconds, learned)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (policy_id, occurrence, plan_id) DO NOTHING''',
                            (policy_id, occurrence, i['plan_id'], i['cluster_id'], i['slot'], i['planned_start'], i['predicted_finish'],
                             i['seconds'], i['learned']))


def get_night(policy_id: int, occurrence) -> dict:
    """Plan id -> the stored plan row of that night."""
    return {r['plan_id']: r for r in _all('SELECT *, cluster_id::text AS cluster_id FROM backup.policy_nights WHERE policy_id = %s AND occurrence = %s',
                                         (policy_id, occurrence))}


def prune_nights(days: int = 90) -> int:
    return _exec('DELETE FROM backup.policy_nights WHERE occurrence < NOW() - make_interval(days => %s)', (int(days),))


def set_manual(policy_id: int, at) -> None:
    """Open (at = now) or close (None) the manual occurrence of a schedule ("run now")."""
    _exec('UPDATE backup.policies SET manual_at = %s WHERE id = %s', (at, policy_id))


def runs_in_occurrence(policy_id: int, occurrence) -> dict:
    """Plan id -> its newest run in that occurrence."""
    rows = _all('''SELECT DISTINCT ON (schedule_id) * FROM backup.runs WHERE policy_id = %s AND occurrence = %s
                    ORDER BY schedule_id, id DESC''', (policy_id, occurrence))
    return {r['schedule_id']: r for r in rows}


# -------------------------------------------------------------------- imports
def add_import(cluster_id: str, source_uri: str, smb_user: str, smb_password: str, target_name: str) -> int:
    smb_user = (smb_user or '').strip() or None
    smb_pass_enc = encrypt_password(smb_password) if smb_password else None
    return _one(
        '''INSERT INTO backup.imports (cluster_id, source_uri, smb_user, smb_pass_enc, target_name)
           VALUES (%s,%s,%s,%s,%s) RETURNING id''',
        (cluster_id, source_uri.rstrip('/'), smb_user, smb_pass_enc, (target_name or '').strip() or None))['id']


def get_import(import_id: int) -> Optional[dict]:
    return _one('''SELECT i.*, i.cluster_id::text AS cluster_id, c.cluster_name FROM backup.imports i
                   JOIN config.clusters c ON c.cluster_id = i.cluster_id WHERE i.id = %s''', (import_id,))


def get_imports(cluster_id: Optional[str] = None, limit: int = 50) -> list[dict]:
    sql = '''SELECT i.*, i.cluster_id::text AS cluster_id, c.cluster_name FROM backup.imports i
             JOIN config.clusters c ON c.cluster_id = i.cluster_id'''
    params: list[Any] = []
    if cluster_id:
        clause, param = _cluster_in('i.cluster_id', cluster_id)
        sql += f' WHERE {clause}'; params.append(param)
    sql += ' ORDER BY i.id DESC LIMIT %s'; params.append(limit)
    return _all(sql, params)


def update_import(import_id: int, **fields) -> None:
    allowed = {'finished_at', 'status', 'task_tag', 'created_uuid', 'message'}
    items = [(k, v) for k, v in fields.items() if k in allowed]
    if not items:
        return
    _exec(f'UPDATE backup.imports SET {", ".join(f"{k} = %s" for k, _ in items)} WHERE id = %s',
          [v for _, v in items] + [import_id])


def get_active_imports() -> list[dict]:
    return _all("SELECT * FROM backup.imports WHERE status = 'RUNNING' ORDER BY id")


# --------------------------------------------------------------------- shares
def _share_row(r: Optional[dict]) -> Optional[dict]:
    if r is None:
        return None
    d = dict(r)
    d['has_password'] = bool(d.pop('password_enc', None))
    return d


def get_shares() -> list[dict]:
    rows = _all('''SELECT s.*, (SELECT COUNT(*) FROM config.folders f WHERE f.share_id = s.id) AS folder_count
                     FROM backup.targets s ORDER BY lower(s.name)''')
    return [_share_row(r) for r in rows]


def get_share(share_id: int, with_secret: bool = False) -> Optional[dict]:
    r = _one('SELECT * FROM backup.targets WHERE id = %s', (share_id,))
    if r is None:
        return None
    if with_secret:
        d = dict(r)
        d['password'] = decrypt_password(d['password_enc']) if d.get('password_enc') else ''
        return d
    return _share_row(r)


def add_share(name: str, uri_base: str, username: str = None, password: str = None, prune_path: str = None) -> int:
    return _one(
        '''INSERT INTO backup.targets (name, uri_base, username, password_enc, prune_path)
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
    _exec(f'UPDATE backup.targets SET {", ".join(sets)} WHERE id = %s', params)


def delete_share(share_id: int) -> None:
    _exec('DELETE FROM backup.targets WHERE id = %s', (share_id,))


def set_default_share(share_id: Optional[int]) -> None:
    """Make *share_id* the share for clusters without a folder (None clears it)."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute('UPDATE backup.targets SET default_for_unfiled = FALSE WHERE default_for_unfiled')
            if share_id:
                cur.execute('UPDATE backup.targets SET default_for_unfiled = TRUE, updated_at = NOW() WHERE id = %s', (share_id,))


def share_for_cluster(cluster_id: str) -> Optional[dict]:
    """The share for a cluster (with decrypted password): the one assigned to its folder, or
    for a cluster without a folder the share flagged default_for_unfiled. None otherwise."""
    r = _one('''SELECT s.* FROM config.clusters c
                  LEFT JOIN config.folders f ON f.folder_id = c.folder_id
                  JOIN backup.targets s
                    ON s.id = CASE WHEN c.folder_id IS NULL
                                   THEN (SELECT id FROM backup.targets WHERE default_for_unfiled LIMIT 1)
                                   ELSE f.share_id END
                 WHERE c.cluster_id = %s''', (cluster_id,))
    if r is None:
        return None
    d = dict(r)
    d['password'] = decrypt_password(d['password_enc']) if d.get('password_enc') else ''
    return d


# -------------------------------------------------------------------- proxies
def _proxy_row(r: Optional[dict]) -> Optional[dict]:
    if r is None:
        return None
    d = dict(r)
    d['cluster_id'] = str(d['cluster_id'])
    d['has_token'] = bool(d.pop('token_enc', None))
    return d


# ------------------------------------------------------------- notice mutes
def get_notice_mute(cluster_id: str, key: str) -> Optional[dict]:
    """The mute of one notice while it lasts, else None."""
    return _one('SELECT key, muted_until, muted_by, muted_at FROM config.notice_mutes '
                'WHERE cluster_id = %s AND key = %s AND muted_until > now()', (cluster_id, key))


def set_notice_mute(cluster_id: str, key: str, days: int, muted_by: Optional[str]) -> None:
    _exec("""INSERT INTO config.notice_mutes (cluster_id, key, muted_until, muted_by, muted_at)
             VALUES (%s, %s, now() + make_interval(days => %s), %s, now())
             ON CONFLICT (cluster_id, key) DO UPDATE
               SET muted_until = EXCLUDED.muted_until, muted_by = EXCLUDED.muted_by, muted_at = now()""",
          (cluster_id, key, int(days), muted_by))


def clear_notice_mute(cluster_id: str, key: str) -> None:
    _exec('DELETE FROM config.notice_mutes WHERE cluster_id = %s AND key = %s', (cluster_id, key))


def get_proxies(cluster_id: Optional[str] = None, enabled_only: bool = False) -> list[dict]:
    sql = '''SELECT p.*, c.cluster_name FROM backup.proxies p JOIN config.clusters c ON c.cluster_id = p.cluster_id'''
    where, params = [], []
    if cluster_id:
        where.append('p.cluster_id = %s'); params.append(cluster_id)
    if enabled_only:
        where.append('p.enabled')
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    return [_proxy_row(r) for r in _all(sql + ' ORDER BY c.cluster_name, p.name', params)]


def get_proxy(proxy_id: int, with_secret: bool = False) -> Optional[dict]:
    r = _one('SELECT * FROM backup.proxies WHERE id = %s', (proxy_id,))
    if r is None:
        return None
    if with_secret:
        d = dict(r)
        d['cluster_id'] = str(d['cluster_id'])
        d['token'] = decrypt_password(d['token_enc']) if d.get('token_enc') else ''
        return d
    return _proxy_row(r)


def add_proxy(cluster_id: str, name: str, url: str, token: Optional[str], kind: str = 'remote',
              vm_uuid: Optional[str] = None, tls_fingerprint: Optional[str] = None, tls_verify: bool = False) -> int:
    return _one('''INSERT INTO backup.proxies (cluster_id, name, url, token_enc, kind, vm_uuid, tls_fingerprint, tls_verify)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
                (cluster_id, name.strip(), url.strip().rstrip('/'), encrypt_password(token) if token else None,
                 kind, vm_uuid, tls_fingerprint, bool(tls_verify)))['id']


def update_proxy(proxy_id: int, **fields) -> None:
    allowed = {'name', 'url', 'kind', 'vm_uuid', 'tls_fingerprint', 'tls_verify', 'enabled', 'status', 'status_detail',
               'last_seen_at', 'version', 'capabilities'}
    sets, params = [], []
    for k, v in fields.items():
        if k == 'token':
            if v:
                sets.append('token_enc = %s'); params.append(encrypt_password(v))
            continue
        if k not in allowed:
            continue
        if k == 'capabilities' and v is not None:
            v = psycopg2.extras.Json(v)
        sets.append(f'{k} = %s'); params.append(v)
    if not sets:
        return
    sets.append('updated_at = NOW()'); params.append(proxy_id)
    _exec(f'UPDATE backup.proxies SET {", ".join(sets)} WHERE id = %s', params)


def delete_proxy(proxy_id: int) -> None:
    _exec('DELETE FROM backup.proxies WHERE id = %s', (proxy_id,))


# --------------------------------------------------------------------- chains
def _json(v):
    return psycopg2.extras.Json(v) if v is not None else None


def get_active_chain(plan_id: int) -> Optional[dict]:
    r = _one("SELECT * FROM backup.chains WHERE plan_id = %s AND status = 'active' ORDER BY id DESC LIMIT 1", (plan_id,))
    return _chain_row(r)


def _chain_row(r):
    if r is None:
        return None
    d = dict(r); d['cluster_id'] = str(d['cluster_id']); return d


def get_chain(chain_id: int) -> Optional[dict]:
    return _chain_row(_one('SELECT * FROM backup.chains WHERE id = %s', (chain_id,)))


def get_chains(cluster_id: Optional[str] = None, vm_uuid: Optional[str] = None, plan_id: Optional[int] = None,
               include_deleted: bool = False) -> list[dict]:
    sql = '''SELECT ch.*, t.name AS target_name, p.enabled AS plan_enabled, c.cluster_name,
                    (SELECT COUNT(*) FROM backup.points pt WHERE pt.chain_id = ch.id AND pt.status = 'ok') AS point_count,
                    (SELECT MAX(pt.created_at) FROM backup.points pt WHERE pt.chain_id = ch.id AND pt.status = 'ok') AS last_point_at,
                    (SELECT COUNT(*) FROM backup.points pt WHERE pt.chain_id = ch.id AND pt.status = 'ok'
                                                            AND pt.verify_error IS NOT NULL) AS verify_failed,
                    (SELECT COUNT(*) FROM backup.points pt WHERE pt.chain_id = ch.id AND pt.status = 'ok'
                                                            AND pt.verify_checked_at IS NULL) AS verify_never,
                    (SELECT MIN(pt.verify_checked_at) FROM backup.points pt WHERE pt.chain_id = ch.id AND pt.status = 'ok') AS verify_oldest,
                    (SELECT MIN(pt.created_at) FROM backup.points pt WHERE pt.chain_id = ch.id AND pt.status = 'ok'
                                                            AND pt.verify_checked_at IS NULL) AS verify_never_since,
                    c.folder_id::text AS folder_id,
                    (SELECT row_to_json(x) FROM (SELECT tr.id, tr.status, tr.verdict, tr.finished_at FROM backup.test_runs tr
                       WHERE tr.cluster_id = ch.cluster_id AND tr.vm_uuid = ch.vm_uuid AND tr.status IN ('passed', 'failed')
                       ORDER BY tr.id DESC LIMIT 1) x) AS last_test
               FROM backup.chains ch LEFT JOIN backup.targets t ON t.id = ch.target_id
               LEFT JOIN backup.plans p ON p.id = ch.plan_id
               LEFT JOIN config.clusters c ON c.cluster_id = ch.cluster_id'''
    where, params = [], []
    if cluster_id:
        clause, param = _cluster_in('ch.cluster_id', cluster_id)
        where.append(clause); params.append(param)
    if vm_uuid:
        where.append('ch.vm_uuid = %s'); params.append(vm_uuid)
    if plan_id:
        where.append('ch.plan_id = %s'); params.append(plan_id)
    if not include_deleted:
        where.append("ch.status <> 'deleted'")
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    return [_chain_row(r) for r in _all(sql + ' ORDER BY ch.vm_name, ch.id DESC', params)]


def add_chain(plan: dict, target_id: int, folder_slug: str, chain_dir: str) -> int:
    return _one('''INSERT INTO backup.chains (plan_id, cluster_id, vm_uuid, vm_name, target_id, folder_slug, chain_dir,
                                              retention_points, retention_days)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
                (plan['id'], plan['cluster_id'], plan['vm_uuid'], plan['vm_name'], target_id, folder_slug, chain_dir,
                 plan.get('retention'), plan.get('retention_days')))['id']


def update_chain(chain_id: int, **fields) -> None:
    allowed = {'status', 'anchor_snapshot_uuid', 'last_serials', 'next_seq', 'runs_since_full', 'active_clones',
               'retention_points', 'retention_days', 'plan_id', 'vm_name'}
    sets, params = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k in ('last_serials', 'active_clones'):
            v = _json(v)
        sets.append(f'{k} = %s'); params.append(v)
    if not sets:
        return
    sets.append('updated_at = NOW()'); params.append(chain_id)
    _exec(f'UPDATE backup.chains SET {", ".join(sets)} WHERE id = %s', params)


def retire_chains_of_plan(plan_id: int) -> int:
    """Deleting a plan keeps its data: chains become retired, keep their retention, lose the plan link."""
    return _exec("UPDATE backup.chains SET status = 'retired', plan_id = NULL, updated_at = NOW() WHERE plan_id = %s AND status = 'active'", (plan_id,))


def chains_with_clones() -> list[dict]:
    return [_chain_row(r) for r in _all("SELECT * FROM backup.chains WHERE active_clones IS NOT NULL AND active_clones <> 'null'::jsonb AND jsonb_array_length(active_clones) > 0")]


# --------------------------------------------------------------------- points
def add_point(chain_id: int, seq: int, kind: str, dir_: str, serials: dict, blocks: int, bytes_written: int,
              duration_s: float, files: list, definition: dict, run_id: Optional[int], quiesced: Optional[bool] = None,
              synthetic: bool = False) -> int:
    """quiesced: what the anchor snapshot reported (True = the guest flushed, False = crash-consistent,
    None = unknown); the restore UI shows it per point. synthetic: a full built on the target from a chain."""
    return _one('''INSERT INTO backup.points (chain_id, seq, kind, dir, serials, blocks, bytes_written, duration_s, files, definition, run_id,
                                              quiesced, synthetic)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
                (chain_id, seq, kind, dir_, _json(serials), blocks, bytes_written, duration_s, _json(files), _json(definition), run_id,
                 quiesced, bool(synthetic)))['id']


def get_points(chain_id: int, include_pruned: bool = False) -> list[dict]:
    sql = 'SELECT * FROM backup.points WHERE chain_id = %s'
    if not include_pruned:
        sql += " AND status = 'ok'"
    return _all(sql + ' ORDER BY seq', (chain_id,))


def get_point(point_id: int) -> Optional[dict]:
    return _one('SELECT * FROM backup.points WHERE id = %s', (point_id,))


def last_point(chain_id: int) -> Optional[dict]:
    return _one("SELECT * FROM backup.points WHERE chain_id = %s AND status = 'ok' ORDER BY seq DESC LIMIT 1", (chain_id,))


def update_point(point_id: int, **fields) -> None:
    allowed = {'status', 'verified_at', 'verify_error', 'files', 'verify_checked_at'}
    items = [(k, _json(v) if k == 'files' else v) for k, v in fields.items() if k in allowed]
    if not items:
        return
    _exec(f'UPDATE backup.points SET {", ".join(f"{k} = %s" for k, _ in items)} WHERE id = %s', [v for _, v in items] + [point_id])


def points_for_plan_retention(plan_id: int) -> list[dict]:
    """All ok points of a plan's chains (active and retired-but-linked), oldest first."""
    return _all('''SELECT pt.*, ch.chain_dir, ch.target_id, ch.status AS chain_status FROM backup.points pt
                   JOIN backup.chains ch ON ch.id = pt.chain_id
                   WHERE ch.plan_id = %s AND pt.status = 'ok' ORDER BY pt.created_at, pt.seq''', (plan_id,))


# --------------------------------------------------------- learned templates
def get_os_template(machine_type: str, os_profile: str) -> Optional[str]:
    r = _one('SELECT template FROM backup.os_templates WHERE machine_type = %s AND os_profile = %s', (machine_type, os_profile))
    return r['template'] if r else None


def save_os_template(machine_type: str, os_profile: str, template: str, source: str = 'learned') -> None:
    _exec('''INSERT INTO backup.os_templates (machine_type, os_profile, template, source) VALUES (%s,%s,%s,%s)
             ON CONFLICT (machine_type, os_profile) DO UPDATE SET template = EXCLUDED.template, source = EXCLUDED.source, created_at = NOW()''',
          (machine_type, os_profile, template, source))


# ------------------------------------------------------------------- restores
def add_restore(point: dict, chain: dict, target_name: str, params: dict, requested_by: str | None = None,
                cluster_id: str | None = None) -> int:
    """A VM restore; cluster_id is where the VM is created (default: the chain's own cluster)."""
    return _one('''INSERT INTO backup.restores (kind, point_id, chain_id, cluster_id, vm_name, target_name, params, requested_by)
                   VALUES ('vm',%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
                (point['id'], chain['id'], cluster_id or chain['cluster_id'], chain['vm_name'], target_name, _json(params),
                 requested_by))['id']


def get_restore(restore_id: int) -> Optional[dict]:
    r = _one('SELECT * FROM backup.restores WHERE id = %s', (restore_id,))
    if r:
        r['cluster_id'] = str(r['cluster_id'])
    return r


def get_restores(cluster_id: Optional[str] = None, limit: int = 50) -> list[dict]:
    sql = '''SELECT r.*, r.cluster_id::text AS cluster_id, ch.vm_uuid, c.cluster_name
               FROM backup.restores r LEFT JOIN backup.chains ch ON ch.id = r.chain_id
               LEFT JOIN config.clusters c ON c.cluster_id = r.cluster_id'''
    params: list[Any] = []
    if cluster_id:
        clause, param = _cluster_in('r.cluster_id', cluster_id)
        sql += f' WHERE {clause}'; params.append(param)
    sql += ' ORDER BY r.id DESC LIMIT %s'; params.append(limit)
    return _all(sql, params)


def update_restore(restore_id: int, **fields) -> None:
    allowed = {'status', 'message', 'created_uuid', 'finished_at', 'progress'}
    items = [(k, _json(v) if k == 'progress' else v) for k, v in fields.items() if k in allowed]
    if not items:
        return
    _exec(f'UPDATE backup.restores SET {", ".join(f"{k} = %s" for k, _ in items)} WHERE id = %s', [v for _, v in items] + [restore_id])


def get_active_restores() -> list[dict]:
    return _all("SELECT * FROM backup.restores WHERE status = 'RUNNING' ORDER BY id")


# ---------------------------------------------------------------- deployments
def add_deployment(cluster_id: str, name: str, params: dict, requested_by: str | None = None) -> int:
    return _one('INSERT INTO backup.deployments (cluster_id, name, params, requested_by) VALUES (%s,%s,%s,%s) RETURNING id',
                (cluster_id, name, _json(params), requested_by))['id']


def get_deployment(dep_id: int) -> Optional[dict]:
    r = _one('SELECT * FROM backup.deployments WHERE id = %s', (dep_id,))
    if r:
        r['cluster_id'] = str(r['cluster_id'])
    return r


def get_deployments(cluster_id: Optional[str] = None, limit: int = 20) -> list[dict]:
    sql = 'SELECT d.*, d.cluster_id::text AS cluster_id, c.cluster_name FROM backup.deployments d JOIN config.clusters c ON c.cluster_id = d.cluster_id'
    params: list[Any] = []
    if cluster_id:
        sql += ' WHERE d.cluster_id = %s'; params.append(cluster_id)
    sql += ' ORDER BY d.id DESC LIMIT %s'; params.append(limit)
    return _all(sql, params)


def delete_finished_deployments(cluster_id: Optional[str] = None) -> int:
    """Drop every deployment row that is no longer running (the panel's Clear)."""
    sql = "DELETE FROM backup.deployments WHERE status <> 'RUNNING'"
    params: list[Any] = []
    if cluster_id:
        sql += ' AND cluster_id = %s'; params.append(cluster_id)
    r = _one(f'WITH d AS ({sql} RETURNING 1) SELECT count(*) AS n FROM d', params)
    return int(r['n']) if r else 0


def prune_deployments(days: int = 30) -> int:
    """Daily housekeeping: finished deployment rows older than `days` go, except the latest
    successful row of a VM that still backs a registered proxy (when it was built stays visible)."""
    r = _one('''WITH keep AS (
                    SELECT DISTINCT ON (d.vm_uuid) d.id
                    FROM backup.deployments d JOIN backup.proxies p ON p.vm_uuid = d.vm_uuid
                    WHERE d.status = 'COMPLETE' ORDER BY d.vm_uuid, d.id DESC),
                  gone AS (
                    DELETE FROM backup.deployments
                    WHERE status <> 'RUNNING' AND COALESCE(finished_at, started_at) < NOW() - make_interval(days => %s)
                      AND id NOT IN (SELECT id FROM keep)
                    RETURNING 1)
               SELECT count(*) AS n FROM gone''', (int(days),))
    return int(r['n']) if r else 0


def update_deployment(dep_id: int, **fields) -> None:
    allowed = {'status', 'message', 'vm_uuid', 'proxy_id', 'finished_at'}
    items = [(k, v) for k, v in fields.items() if k in allowed]
    if not items:
        return
    _exec(f'UPDATE backup.deployments SET {", ".join(f"{k} = %s" for k, _ in items)} WHERE id = %s', [v for _, v in items] + [dep_id])


# ------------------------------------------------------- planned verification (052)
VERIFY_DEFAULTS = {'interval_days': 7, 'hours_from': '01:00', 'hours_until': '06:00', 'dr_during_backups': False}
_NO_FOLDER = '00000000-0000-0000-0000-000000000000'


def verify_settings(folder_id: Optional[str]) -> dict:
    """A folder's verification settings (None = the unfiled clusters); the defaults when none were saved."""
    r = _one('''SELECT interval_days, hours_from, hours_until, dr_during_backups, updated_at, updated_by
                  FROM backup.verify_settings WHERE COALESCE(folder_id, %s::uuid) = COALESCE(%s::uuid, %s::uuid)''',
             (_NO_FOLDER, folder_id, _NO_FOLDER))
    out = dict(VERIFY_DEFAULTS, updated_at=None, updated_by=None)
    if r:
        out.update(r)
    return out


def save_verify_settings(folder_id: Optional[str], actor: Optional[str], **fields) -> None:
    f = {k: fields[k] for k in VERIFY_DEFAULTS if k in fields}
    cur = verify_settings(folder_id)
    vals = {k: f.get(k, cur[k]) for k in VERIFY_DEFAULTS}
    with connect() as conn:
        with conn.cursor() as c:
            c.execute('''UPDATE backup.verify_settings SET interval_days = %(interval_days)s, hours_from = %(hours_from)s,
                                hours_until = %(hours_until)s, dr_during_backups = %(dr_during_backups)s,
                                updated_at = NOW(), updated_by = %(actor)s
                         WHERE COALESCE(folder_id, %(none)s::uuid) = COALESCE(%(folder)s::uuid, %(none)s::uuid)''',
                      dict(vals, actor=actor, folder=folder_id, none=_NO_FOLDER))
            if c.rowcount == 0:
                c.execute('''INSERT INTO backup.verify_settings (folder_id, interval_days, hours_from, hours_until, dr_during_backups, updated_by)
                             VALUES (%(folder)s, %(interval_days)s, %(hours_from)s, %(hours_until)s, %(dr_during_backups)s, %(actor)s)''',
                          dict(vals, actor=actor, folder=folder_id))


def verify_folders() -> list[Optional[str]]:
    """Every folder (None = unfiled) that has an enabled cluster."""
    return [r['folder_id'] for r in _all('''SELECT DISTINCT folder_id::text AS folder_id FROM config.clusters
                                              WHERE enabled AND cluster_id <> %s::uuid''', ('00000000-0000-0000-0000-000000000001',))]


def folder_clusters(folder_id: Optional[str]) -> list[dict]:
    """Enabled clusters of a folder with their role and time zone."""
    return [dict(r, cluster_id=str(r['cluster_id'])) for r in _all(
        '''SELECT cluster_id, cluster_name, COALESCE(role, 'production') AS role, timezone
             FROM config.clusters WHERE enabled AND COALESCE(folder_id, %s::uuid) = COALESCE(%s::uuid, %s::uuid)
               AND cluster_id <> %s::uuid ORDER BY cluster_name''',
        (_NO_FOLDER, folder_id, _NO_FOLDER, '00000000-0000-0000-0000-000000000001'))]


def cluster_role(cluster_id: str) -> str:
    r = _one("SELECT COALESCE(role, 'production') AS role FROM config.clusters WHERE cluster_id = %s", (cluster_id,))
    return r['role'] if r else 'production'


def verify_candidates(folder_id: Optional[str], older_than: datetime, limit: int = 60, per_chain: int = 2) -> list[dict]:
    """Restore points of a folder's chains (active and retired: both can be restored) not checked since
    `older_than`, never-checked first, then the longest unchecked; points with an open failure come last
    (they are retried, but never block the rest). At most `per_chain` per chain, so one chain with many
    unchecked points cannot fill the list while only one point per chain is verified at a time."""
    return _all('''SELECT * FROM (
                    SELECT pt.id AS point_id, pt.seq, pt.dir, pt.kind, pt.created_at, pt.verify_checked_at, pt.verify_error,
                           COALESCE(pt.bytes_written, 0) AS bytes_written, ch.id AS chain_id, ch.plan_id, ch.vm_name,
                           ch.cluster_id::text AS cluster_id, ch.target_id, ch.chain_dir,
                           ROW_NUMBER() OVER (PARTITION BY ch.id ORDER BY pt.verify_error IS NOT NULL,
                                              pt.verify_checked_at NULLS FIRST, pt.created_at) AS nth
                      FROM backup.points pt JOIN backup.chains ch ON ch.id = pt.chain_id
                      JOIN config.clusters c ON c.cluster_id = ch.cluster_id
                     WHERE pt.status = 'ok' AND ch.status IN ('active', 'retired') AND ch.target_id IS NOT NULL
                       AND COALESCE(c.folder_id, %s::uuid) = COALESCE(%s::uuid, %s::uuid)
                       AND (pt.verify_checked_at IS NULL OR pt.verify_checked_at < %s)) x
                   WHERE nth <= %s
                   ORDER BY verify_error IS NOT NULL, verify_checked_at NULLS FIRST, created_at
                   LIMIT %s''', (_NO_FOLDER, folder_id, _NO_FOLDER, older_than, int(per_chain), int(limit)))


def plan_run_active(plan_id: Optional[int]) -> bool:
    if not plan_id:
        return False
    r = _one("SELECT 1 AS x FROM backup.runs WHERE schedule_id = %s AND status = 'RUNNING' LIMIT 1", (plan_id,))
    return r is not None


def active_runs_on(cluster_ids: list[str]) -> int:
    if not cluster_ids:
        return 0
    r = _one('''SELECT COUNT(*) AS n FROM backup.runs r JOIN backup.plans s ON s.id = r.schedule_id
                  WHERE r.status = 'RUNNING' AND s.cluster_id = ANY(%s::uuid[])''', (list(cluster_ids),))
    return int(r['n']) if r else 0


def standalone_plans_on(cluster_ids: list[str]) -> list[dict]:
    """Enabled plans with their own daily time (not in a schedule) on these clusters."""
    if not cluster_ids:
        return []
    return _all(_SCHED_SELECT + ''' WHERE s.cluster_id = ANY(%s::uuid[]) AND s.enabled AND s.policy_id IS NULL''',
                (list(cluster_ids),))


def add_verify_run(**f) -> int:
    cols = [k for k in ('folder_id', 'cluster_id', 'proxy_id', 'proxy_name', 'proxy_role', 'chain_id', 'point_id', 'vm_name', 'seq') if k in f]
    return _one(f'''INSERT INTO backup.verify_runs ({", ".join(cols)}) VALUES ({", ".join(["%s"] * len(cols))}) RETURNING id''',
                [f[k] for k in cols])['id']


def finish_verify_run(run_id: int, status: str, bytes_: Optional[int] = None, duration_s: Optional[float] = None,
                      message: Optional[str] = None) -> None:
    _exec('''UPDATE backup.verify_runs SET status = %s, finished_at = NOW(), bytes = %s, duration_s = %s, message = %s
              WHERE id = %s''', (status, bytes_, duration_s, (message or None) and message[:1000], run_id))


def interrupted_verify_runs() -> int:
    return _exec("""UPDATE backup.verify_runs SET status = 'stopped', finished_at = NOW(),
                       message = 'Interrupted by a restart; the point is verified again later.' WHERE status = 'running'""")


def verify_runs(cluster_ids: list[str], limit: int = 100) -> list[dict]:
    """Recent point verifications of chains on these clusters, newest first."""
    return _all('''SELECT v.*, v.cluster_id::text AS cluster_id, v.folder_id::text AS folder_id, c.cluster_name, ch.vm_uuid
                     FROM backup.verify_runs v LEFT JOIN config.clusters c ON c.cluster_id = v.cluster_id
                     LEFT JOIN backup.chains ch ON ch.id = v.chain_id
                    WHERE v.cluster_id = ANY(%s::uuid[]) ORDER BY v.id DESC LIMIT %s''', (list(cluster_ids), int(limit)))


def proxy_read_rate(proxy_id: int) -> Optional[float]:
    """Median bytes/second of this proxy's recent verifications of at least 256 MiB, or None."""
    r = _one('''SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY bytes / NULLIF(duration_s, 0)) AS rate FROM (
                    SELECT bytes, duration_s FROM backup.verify_runs
                     WHERE proxy_id = %s AND status IN ('ok', 'failed') AND bytes >= 268435456 AND duration_s > 0
                     ORDER BY id DESC LIMIT 20) x''', (proxy_id,))
    return float(r['rate']) if r and r['rate'] else None


def raise_verify_alert(kind: str, chain: dict, point: Optional[dict], detail: str) -> bool:
    """Open an alert unless one is open already for that point (failed) or chain (overdue). True when new."""
    known, folder = folder_of_cluster(chain['cluster_id'])
    r = _one('''INSERT INTO backup.verify_alerts (kind, folder_id, cluster_id, chain_id, point_id, vm_name, seq, detail)
                  VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING id''',
             (kind, folder, chain['cluster_id'], chain['id'], point['id'] if point else None, chain.get('vm_name'),
              point['seq'] if point else None, (detail or '')[:1000]))
    return r is not None


def resolve_verify_alerts(point_id: Optional[int] = None, chain_id: Optional[int] = None, kind: Optional[str] = None) -> int:
    where, params = ['resolved_at IS NULL'], []
    if point_id is not None:
        where.append('point_id = %s'); params.append(point_id)
    if chain_id is not None:
        where.append('chain_id = %s'); params.append(chain_id)
    if kind:
        where.append('kind = %s'); params.append(kind)
    return _exec(f'UPDATE backup.verify_alerts SET resolved_at = NOW() WHERE {" AND ".join(where)}', params)


def resolve_alerts_of_gone_points() -> int:
    """Alerts of points that were pruned or whose chain was deleted are no longer actionable."""
    return _exec('''UPDATE backup.verify_alerts a SET resolved_at = NOW()
                     WHERE a.resolved_at IS NULL AND a.kind IN ('failed', 'overdue') AND (
                        (a.point_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM backup.points p WHERE p.id = a.point_id AND p.status = 'ok'))
                        OR NOT EXISTS (SELECT 1 FROM backup.chains c WHERE c.id = a.chain_id AND c.status IN ('active', 'retired')))''')


def open_verify_alerts(cluster_id: str) -> list[dict]:
    return _all('''SELECT id, kind, chain_id, point_id, vm_uuid, vm_name, seq, detail, created_at FROM backup.verify_alerts
                    WHERE cluster_id = %s AND resolved_at IS NULL ORDER BY created_at''', (cluster_id,))


def overdue_chains(folder_id: Optional[str], older_than: datetime) -> list[dict]:
    """Chains of a folder with a point not checked since `older_than` (never-checked points count from
    their creation), with how many such points and the oldest moment."""
    return _all('''SELECT ch.id, ch.vm_name, ch.cluster_id::text AS cluster_id, COUNT(*) AS points,
                         MIN(COALESCE(pt.verify_checked_at, pt.created_at)) AS since
                    FROM backup.points pt JOIN backup.chains ch ON ch.id = pt.chain_id
                    JOIN config.clusters c ON c.cluster_id = ch.cluster_id
                   WHERE pt.status = 'ok' AND ch.status IN ('active', 'retired') AND ch.target_id IS NOT NULL
                     AND COALESCE(c.folder_id, %s::uuid) = COALESCE(%s::uuid, %s::uuid)
                     AND COALESCE(pt.verify_checked_at, pt.created_at) < %s
                   GROUP BY ch.id, ch.vm_name, ch.cluster_id''', (_NO_FOLDER, folder_id, _NO_FOLDER, older_than))


def open_overdue_chain_ids(folder_id: Optional[str]) -> set:
    return {r['chain_id'] for r in _all('''SELECT chain_id FROM backup.verify_alerts
                                              WHERE kind = 'overdue' AND resolved_at IS NULL
                                                AND COALESCE(folder_id, %s::uuid) = COALESCE(%s::uuid, %s::uuid)''',
                                           (_NO_FOLDER, folder_id, _NO_FOLDER))}


def verify_summary(folder_id: Optional[str], interval_days: int) -> dict:
    """Counts over a folder's restorable points for the Schedules tab."""
    r = _one('''SELECT COUNT(*) AS points,
                         COUNT(*) FILTER (WHERE pt.verify_error IS NOT NULL) AS failed,
                         COUNT(*) FILTER (WHERE pt.verify_checked_at IS NULL) AS never,
                         COUNT(*) FILTER (WHERE pt.verify_error IS NULL AND pt.verify_checked_at >= NOW() - make_interval(days => %s)) AS current,
                         COUNT(*) FILTER (WHERE COALESCE(pt.verify_checked_at, pt.created_at) < NOW() - make_interval(days => %s)) AS overdue,
                         MIN(pt.verify_checked_at) AS oldest
                    FROM backup.points pt JOIN backup.chains ch ON ch.id = pt.chain_id
                    JOIN config.clusters c ON c.cluster_id = ch.cluster_id
                   WHERE pt.status = 'ok' AND ch.status IN ('active', 'retired') AND ch.target_id IS NOT NULL
                     AND COALESCE(c.folder_id, %s::uuid) = COALESCE(%s::uuid, %s::uuid)''',
             (max(1, interval_days), max(1, interval_days) * 2, _NO_FOLDER, folder_id, _NO_FOLDER))
    return dict(r) if r else {}


def prune_verify_history(days: int = 90) -> int:
    n = _exec('DELETE FROM backup.verify_runs WHERE status <> %s AND started_at < NOW() - make_interval(days => %s)', ('running', int(days)))
    n += _exec('DELETE FROM backup.verify_alerts WHERE resolved_at < NOW() - make_interval(days => %s)', (int(days),))
    return n


# --------------------------------------------------------------- test restores (053)
_TEST_PLAN_FIELDS = ('name', 'enabled', 'interval_days', 'hours_from', 'hours_until', 'random_count', 'rest_weeks', 'level',
                     'test_vlan', 'probe_address', 'default_ports', 'timeout_minutes', 'keep_failed_hours', 'byte_budget_gb')


def _test_plan_row(r):
    if r is None:
        return None
    d = dict(r)
    d['folder_id'] = str(d['folder_id']) if d.get('folder_id') else None
    d['default_ports'] = list(d.get('default_ports') or [])
    return d


def get_test_plans(folder_id: Optional[str] = None, all_folders: bool = False) -> list[dict]:
    if all_folders:
        return [_test_plan_row(r) for r in _all('SELECT * FROM backup.test_plans ORDER BY id')]
    return [_test_plan_row(r) for r in _all('''SELECT * FROM backup.test_plans
                                                WHERE COALESCE(folder_id, %s::uuid) = COALESCE(%s::uuid, %s::uuid)
                                                ORDER BY lower(name), id''', (_NO_FOLDER, folder_id, _NO_FOLDER))]


def get_test_plan(plan_id: int) -> Optional[dict]:
    return _test_plan_row(_one('SELECT * FROM backup.test_plans WHERE id = %s', (plan_id,)))


def add_test_plan(folder_id: Optional[str], actor: Optional[str], **f) -> int:
    cols = [k for k in _TEST_PLAN_FIELDS if k in f]
    return _one(f'''INSERT INTO backup.test_plans (folder_id, updated_by, {", ".join(cols)})
                    VALUES (%s, %s, {", ".join(["%s"] * len(cols))}) RETURNING id''', [folder_id, actor] + [f[k] for k in cols])['id']


def update_test_plan(plan_id: int, actor: Optional[str] = None, **f) -> None:
    items = [(k, v) for k, v in f.items() if k in _TEST_PLAN_FIELDS or k == 'last_run_at']
    if not items:
        return
    _exec(f'''UPDATE backup.test_plans SET {", ".join(f"{k} = %s" for k, _ in items)}, updated_at = NOW(),
                     updated_by = COALESCE(%s, updated_by) WHERE id = %s''', [v for _, v in items] + [actor, plan_id])


def delete_test_plan(plan_id: int) -> None:
    _exec('DELETE FROM backup.test_plans WHERE id = %s', (plan_id,))


def test_plan_vms(plan_id: int) -> list[dict]:
    return [dict(r, cluster_id=str(r['cluster_id']), ports=list(r['ports']) if r.get('ports') is not None else None)
            for r in _all('SELECT * FROM backup.test_plan_vms WHERE plan_id = %s ORDER BY vm_name', (plan_id,))]


def set_test_plan_vm(plan_id: int, cluster_id: str, vm_uuid: str, vm_name: Optional[str], mode: str, ports) -> None:
    _exec('''INSERT INTO backup.test_plan_vms (plan_id, cluster_id, vm_uuid, vm_name, mode, ports) VALUES (%s,%s,%s,%s,%s,%s)
             ON CONFLICT (plan_id, cluster_id, vm_uuid) DO UPDATE SET vm_name = EXCLUDED.vm_name, mode = EXCLUDED.mode,
                                                                      ports = EXCLUDED.ports''',
          (plan_id, cluster_id, vm_uuid, vm_name, mode, ports))


def remove_test_plan_vm(plan_id: int, cluster_id: str, vm_uuid: str) -> int:
    return _exec('DELETE FROM backup.test_plan_vms WHERE plan_id = %s AND cluster_id = %s AND vm_uuid = %s', (plan_id, cluster_id, vm_uuid))


def backed_up_vms(folder_id: Optional[str]) -> list[dict]:
    """VMs of a folder with at least one restorable point in an active chain, with their last test."""
    return _all('''SELECT DISTINCT ON (ch.cluster_id, ch.vm_uuid) ch.cluster_id::text AS cluster_id, ch.vm_uuid, ch.vm_name,
                          (SELECT MAX(t.finished_at) FROM backup.test_runs t WHERE t.cluster_id = ch.cluster_id
                              AND t.vm_uuid = ch.vm_uuid AND t.status IN ('passed', 'failed')) AS last_tested
                     FROM backup.chains ch JOIN config.clusters c ON c.cluster_id = ch.cluster_id
                    WHERE ch.status = 'active' AND ch.target_id IS NOT NULL AND c.enabled
                      AND COALESCE(c.folder_id, %s::uuid) = COALESCE(%s::uuid, %s::uuid)
                      AND EXISTS (SELECT 1 FROM backup.points p WHERE p.chain_id = ch.id AND p.status = 'ok')
                    ORDER BY ch.cluster_id, ch.vm_uuid, ch.id DESC''', (_NO_FOLDER, folder_id, _NO_FOLDER))


def newest_point_for_vm(cluster_id: str, vm_uuid: str) -> Optional[dict]:
    """The newest restorable point of the VM's active chain, with its chain and the bytes a restore writes
    (the sum of what the chain's points up to it wrote: an upper bound)."""
    return _one('''SELECT p.*, ch.id AS chain_id, ch.vm_name, ch.target_id, ch.chain_dir, ch.folder_slug,
                          (SELECT COALESCE(SUM(q.bytes_written), 0) FROM backup.points q
                            WHERE q.chain_id = ch.id AND q.status = 'ok' AND q.seq <= p.seq) AS restore_bytes
                     FROM backup.points p JOIN backup.chains ch ON ch.id = p.chain_id
                    WHERE ch.cluster_id = %s AND ch.vm_uuid = %s AND ch.status = 'active' AND p.status = 'ok'
                    ORDER BY p.created_at DESC, p.seq DESC LIMIT 1''', (cluster_id, vm_uuid))


def queue_test_runs(plan: dict, round_at: datetime, vms: list[dict]) -> int:
    n = 0
    with connect() as conn:
        with conn.cursor() as c:
            # what the previous round did not reach is not carried over: the new round picks again
            c.execute('''UPDATE backup.test_runs SET status = 'skipped', finished_at = NOW(),
                                detail = 'not reached before the next round of this plan'
                          WHERE plan_id = %s AND status = 'queued' ''', (plan['id'],))
            for v in vms:
                c.execute('''INSERT INTO backup.test_runs (plan_id, folder_id, round_at, cluster_id, vm_uuid, vm_name, picked, level)
                             VALUES (%s,%s,%s,%s,%s,%s,%s,%s)''',
                          (plan['id'], plan['folder_id'], round_at, v['cluster_id'], v['vm_uuid'], v.get('vm_name'), v['picked'], plan['level']))
                n += 1
            c.execute('UPDATE backup.test_plans SET last_run_at = %s WHERE id = %s', (round_at, plan['id']))
    return n


_TEST_RUN_COLS = ('phase', 'progress', 'status', 'verdict', 'agent', 'point_id', 'point_created_at', 'target_cluster_id', 'proxy_id', 'restore_id',
                  'restored_vm_uuid', 'bytes', 'checks', 'screenshot', 'detail', 'started_at', 'restored_at', 'booted_at',
                  'finished_at', 'keep_until', 'cleaned_at')


def update_test_run(run_id: int, **f) -> None:
    items = [(k, _json(v) if k in ('checks', 'progress') else (psycopg2.Binary(v) if k == 'screenshot' and v is not None else v))
             for k, v in f.items() if k in _TEST_RUN_COLS]
    if items:
        _exec(f'UPDATE backup.test_runs SET {", ".join(f"{k} = %s" for k, _ in items)} WHERE id = %s', [v for _, v in items] + [run_id])


_TEST_RUN_SELECT = '''SELECT t.id, t.plan_id, t.folder_id::text AS folder_id, t.round_at, t.cluster_id::text AS cluster_id, t.vm_uuid,
                             t.vm_name, t.picked, t.level, t.status, t.verdict, t.agent, t.point_id, t.point_created_at,
                             t.target_cluster_id::text AS target_cluster_id, t.proxy_id, t.restore_id, t.restored_vm_uuid, t.bytes,
                             t.checks, t.detail, t.queued_at, t.started_at, t.restored_at, t.booted_at, t.finished_at,
                             t.keep_until, t.cleaned_at, (t.screenshot IS NOT NULL) AS has_screenshot, t.phase, t.progress,
                             rs.progress AS restore_progress, rs.message AS restore_message,
                             c.cluster_name, tc.cluster_name AS target_cluster_name, pl.name AS plan_name
                        FROM backup.test_runs t LEFT JOIN config.clusters c ON c.cluster_id = t.cluster_id
                        LEFT JOIN backup.restores rs ON rs.id = t.restore_id
                        LEFT JOIN config.clusters tc ON tc.cluster_id = t.target_cluster_id
                        LEFT JOIN backup.test_plans pl ON pl.id = t.plan_id'''


def get_test_run(run_id: int) -> Optional[dict]:
    return _one(_TEST_RUN_SELECT + ' WHERE t.id = %s', (run_id,))


def test_runs(cluster_ids, limit: int = 100) -> list[dict]:
    if cluster_ids is None:
        return _all(_TEST_RUN_SELECT + ' ORDER BY t.id DESC LIMIT %s', (int(limit),))
    ids = cluster_ids if isinstance(cluster_ids, list) else [cluster_ids]
    return _all(_TEST_RUN_SELECT + ' WHERE t.cluster_id = ANY(%s::uuid[]) ORDER BY t.id DESC LIMIT %s', (ids, int(limit)))


def queued_test_runs() -> list[dict]:
    return _all(_TEST_RUN_SELECT + " WHERE t.status = 'queued' ORDER BY t.queued_at, t.id")


def test_run_screenshot(run_id: int) -> Optional[bytes]:
    r = _one('SELECT screenshot FROM backup.test_runs WHERE id = %s', (run_id,))
    return bytes(r['screenshot']) if r and r.get('screenshot') is not None else None


def round_bytes(plan_id: int, round_at) -> int:
    r = _one('''SELECT COALESCE(SUM(bytes), 0) AS n FROM backup.test_runs
                 WHERE plan_id = %s AND round_at = %s AND status NOT IN ('queued', 'skipped')''', (plan_id, round_at))
    return int(r['n']) if r else 0


def interrupted_test_runs() -> int:
    return _exec("""UPDATE backup.test_runs SET status = 'error', finished_at = NOW(),
                       detail = 'Interrupted by a restart; the test VM is removed by the sweep.' WHERE status = 'running'""")


def test_vms_to_clean(now: datetime) -> list[dict]:
    """Test VMs that are done with: finished (or interrupted), not kept, not deleted yet."""
    return _all('''SELECT id, target_cluster_id::text AS target_cluster_id, restored_vm_uuid FROM backup.test_runs
                    WHERE restored_vm_uuid IS NOT NULL AND cleaned_at IS NULL AND status NOT IN ('running', 'queued')
                      AND (keep_until IS NULL OR keep_until < %s)''', (now,))


def test_vms_in_use(now: datetime) -> set:
    """Restored test VMs that must stay: running tests, and failed ones kept for inspection."""
    return {r['restored_vm_uuid'] for r in _all('''SELECT restored_vm_uuid FROM backup.test_runs
                                                    WHERE restored_vm_uuid IS NOT NULL AND cleaned_at IS NULL
                                                      AND (status = 'running' OR keep_until >= %s)''', (now,))}


def last_test_passes(plan_id: int) -> dict:
    """(cluster_id, vm_uuid) -> last passed finished_at, for a plan's fixed VMs."""
    rows = _all('''SELECT v.cluster_id::text AS cluster_id, v.vm_uuid, v.vm_name,
                          (SELECT MAX(t.finished_at) FROM backup.test_runs t WHERE t.cluster_id = v.cluster_id AND t.vm_uuid = v.vm_uuid
                              AND t.status = 'passed') AS passed
                     FROM backup.test_plan_vms v WHERE v.plan_id = %s AND v.mode = 'fixed' ''', (plan_id,))
    return {(r['cluster_id'], r['vm_uuid']): r for r in rows}


def raise_test_alert(kind: str, folder_id: Optional[str], cluster_id: str, vm_uuid: str, vm_name: Optional[str], detail: str) -> bool:
    r = _one('''INSERT INTO backup.verify_alerts (kind, folder_id, cluster_id, vm_uuid, vm_name, detail)
                VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING id''',
             (kind, folder_id, cluster_id, vm_uuid, vm_name, (detail or '')[:1000]))
    return r is not None


def resolve_test_alerts(cluster_id: str, vm_uuid: str, kind: Optional[str] = None) -> int:
    sql = "UPDATE backup.verify_alerts SET resolved_at = NOW() WHERE resolved_at IS NULL AND cluster_id = %s AND vm_uuid = %s"
    params: list = [cluster_id, vm_uuid]
    if kind:
        sql += ' AND kind = %s'; params.append(kind)
    else:
        sql += " AND kind IN ('test_failed', 'test_overdue')"
    return _exec(sql, params)


def ram_limit_percent(cluster_id: str) -> float:
    """The balancer's hard RAM ceiling for the cluster (RAM_LIMIT_PERCENT; the balancer's default is 85)."""
    try:
        r = _one("SELECT value FROM config.tunables WHERE cluster_id = %s AND key = 'RAM_LIMIT_PERCENT'", (cluster_id,))
        return float(r["value"]) if r else 85.0
    except Exception:                                   # noqa: BLE001
        return 85.0


def running_test_ids() -> list[int]:
    return [r['id'] for r in _all("SELECT id FROM backup.test_runs WHERE status = 'running'")]
