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


def get_schedules(cluster_id: Optional[str] = None) -> list[dict]:
    if cluster_id:
        return _all(_SCHED_SELECT + ' WHERE s.cluster_id = %s ORDER BY s.vm_name', (cluster_id,))
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
               'overhead_seconds', 'notice', 'synth_seconds', 'planned_start', 'predicted_finish'}
    items = [(k, v) for k, v in fields.items() if k in allowed]
    if not items:
        return
    _exec(f'UPDATE backup.runs SET {", ".join(f"{k} = %s" for k, _ in items)} WHERE id = %s',
          [v for _, v in items] + [run_id])


def get_runs(cluster_id: Optional[str] = None, schedule_id: Optional[int] = None, limit: int = 50) -> list[dict]:
    sql = '''SELECT r.*, s.vm_name, s.cluster_id::text AS cluster_id, c.cluster_name
               FROM backup.runs r JOIN backup.plans s ON s.id = r.schedule_id
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
        sql += ' WHERE i.cluster_id = %s'; params.append(cluster_id)
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
    return _one('SELECT key, muted_until, muted_by, muted_at FROM backup.notice_mutes '
                'WHERE cluster_id = %s AND key = %s AND muted_until > now()', (cluster_id, key))


def set_notice_mute(cluster_id: str, key: str, days: int, muted_by: Optional[str]) -> None:
    _exec("""INSERT INTO backup.notice_mutes (cluster_id, key, muted_until, muted_by, muted_at)
             VALUES (%s, %s, now() + make_interval(days => %s), %s, now())
             ON CONFLICT (cluster_id, key) DO UPDATE
               SET muted_until = EXCLUDED.muted_until, muted_by = EXCLUDED.muted_by, muted_at = now()""",
          (cluster_id, key, int(days), muted_by))


def clear_notice_mute(cluster_id: str, key: str) -> None:
    _exec('DELETE FROM backup.notice_mutes WHERE cluster_id = %s AND key = %s', (cluster_id, key))


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
    sql = '''SELECT ch.*, t.name AS target_name, p.enabled AS plan_enabled,
                    (SELECT COUNT(*) FROM backup.points pt WHERE pt.chain_id = ch.id AND pt.status = 'ok') AS point_count,
                    (SELECT MAX(pt.created_at) FROM backup.points pt WHERE pt.chain_id = ch.id AND pt.status = 'ok') AS last_point_at
               FROM backup.chains ch LEFT JOIN backup.targets t ON t.id = ch.target_id
               LEFT JOIN backup.plans p ON p.id = ch.plan_id'''
    where, params = [], []
    if cluster_id:
        where.append('ch.cluster_id = %s'); params.append(cluster_id)
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
    allowed = {'status', 'verified_at', 'verify_error', 'files'}
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
def add_restore(point: dict, chain: dict, target_name: str, params: dict, requested_by: str | None = None) -> int:
    return _one('''INSERT INTO backup.restores (kind, point_id, chain_id, cluster_id, vm_name, target_name, params, requested_by)
                   VALUES ('vm',%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
                (point['id'], chain['id'], chain['cluster_id'], chain['vm_name'], target_name, _json(params), requested_by))['id']


def get_restore(restore_id: int) -> Optional[dict]:
    r = _one('SELECT * FROM backup.restores WHERE id = %s', (restore_id,))
    if r:
        r['cluster_id'] = str(r['cluster_id'])
    return r


def get_restores(cluster_id: Optional[str] = None, limit: int = 50) -> list[dict]:
    sql = 'SELECT r.*, r.cluster_id::text AS cluster_id FROM backup.restores r'
    params: list[Any] = []
    if cluster_id:
        sql += ' WHERE r.cluster_id = %s'; params.append(cluster_id)
    sql += ' ORDER BY r.id DESC LIMIT %s'; params.append(limit)
    return _all(sql, params)


def update_restore(restore_id: int, **fields) -> None:
    allowed = {'status', 'message', 'created_uuid', 'finished_at'}
    items = [(k, v) for k, v in fields.items() if k in allowed]
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
