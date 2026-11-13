"""
store.py — PostgreSQL access of the console service (schema `console`, migration 041).

Plain psycopg2, one short connection per call; the async code calls these through
asyncio.to_thread so a slow database never stalls a running console.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Optional

import psycopg2
import psycopg2.extras

DEFAULT_SETTINGS = {'enabled': False, 'interval_minutes': 30, 'wake': 'none', 'keep_images': False}


@contextmanager
def _conn():
    c = psycopg2.connect(host=os.getenv('PG_HOST', 'postgres'), port=int(os.getenv('PG_PORT', 5432)),
                         dbname=os.getenv('PG_DB', 'hypercore_balancer'), user=os.getenv('PG_USER', 'sentinel'),
                         password=os.getenv('PG_PASSWORD', ''), connect_timeout=10,
                         cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def _all(sql: str, params: tuple = ()) -> list[dict]:
    with _conn() as c, c.cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def _one(sql: str, params: tuple = ()) -> Optional[dict]:
    rows = _all(sql, params)
    return rows[0] if rows else None


def _exec(sql: str, params: tuple = ()) -> None:
    with _conn() as c, c.cursor() as cur:
        cur.execute(sql, params)


# ------------------------------------------------------------- audit log
def session_start(cluster_id: str, vm_uuid: str, vm_name: str, username: str, *, source: str = 'user',
                  kind: str = 'interactive', client_ip: Optional[str] = None) -> int:
    row = _one("""INSERT INTO console.sessions (cluster_id, vm_uuid, vm_name, username, source, kind, client_ip)
                  VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
               (cluster_id, vm_uuid, vm_name, username, source, kind, client_ip))
    return int(row['id'])


def session_end(session_id: int, reason: str, bytes_in: int = 0, bytes_out: int = 0) -> None:
    _exec("""UPDATE console.sessions SET ended_at = now(), end_reason = %s, bytes_in = %s, bytes_out = %s
             WHERE id = %s AND ended_at IS NULL""", (reason, int(bytes_in), int(bytes_out), session_id))


def close_dangling() -> int:
    """Sessions left open by a restart of this service: they ended when the process did."""
    rows = _all("""UPDATE console.sessions SET ended_at = now(), end_reason = 'service restarted'
                   WHERE ended_at IS NULL RETURNING id""")
    return len(rows)


def list_sessions(cluster_id: Optional[str], *, include_system: bool = False, vm_uuid: Optional[str] = None,
                  limit: int = 200) -> list[dict]:
    where, params = [], []
    if cluster_id:
        where.append('cluster_id = %s'); params.append(cluster_id)
    if vm_uuid:
        where.append('vm_uuid = %s'); params.append(vm_uuid)
    if not include_system:
        where.append("source <> 'system'")
    sql = ('SELECT id, cluster_id, vm_uuid, vm_name, username, source, kind, client_ip, started_at, ended_at, '
           'end_reason, bytes_in, bytes_out FROM console.sessions'
           + (' WHERE ' + ' AND '.join(where) if where else '') + ' ORDER BY started_at DESC LIMIT %s')
    params.append(max(1, min(int(limit), 2000)))
    return _all(sql, tuple(params))


def prune_system_sessions(days: int = 30) -> None:
    _exec("DELETE FROM console.sessions WHERE source = 'system' AND started_at < now() - make_interval(days => %s)",
          (int(days),))


# --------------------------------------------------------------- settings
def get_settings(cluster_id: str) -> dict[str, Any]:
    row = _one('SELECT enabled, interval_minutes, wake, keep_images, updated_at, updated_by '
               'FROM console.check_settings WHERE cluster_id = %s', (cluster_id,))
    return row or dict(DEFAULT_SETTINGS, updated_at=None, updated_by=None)


def enabled_clusters() -> list[dict]:
    return _all('SELECT cluster_id, interval_minutes, wake, keep_images FROM console.check_settings WHERE enabled')


def set_settings(cluster_id: str, enabled: bool, interval_minutes: int, wake: str, keep_images: bool,
                 username: Optional[str]) -> None:
    _exec("""INSERT INTO console.check_settings (cluster_id, enabled, interval_minutes, wake, keep_images, updated_at, updated_by)
             VALUES (%s, %s, %s, %s, %s, now(), %s)
             ON CONFLICT (cluster_id) DO UPDATE SET enabled = EXCLUDED.enabled,
               interval_minutes = EXCLUDED.interval_minutes, wake = EXCLUDED.wake,
               keep_images = EXCLUDED.keep_images, updated_at = now(), updated_by = EXCLUDED.updated_by""",
          (cluster_id, bool(enabled), int(interval_minutes), wake, bool(keep_images), username))
    if not keep_images:
        _exec('UPDATE console.check_vm SET image = NULL, image_at = NULL WHERE cluster_id = %s', (cluster_id,))


# ------------------------------------------------------------- VM verdicts
def check_vms(cluster_id: str) -> list[dict]:
    return _all("""SELECT v.vm_uuid, v.vm_name, v.state, v.detail, v.first_open_at, v.last_check_at, v.excluded,
                          v.excluded_by, v.image IS NOT NULL AS has_image, v.image_at,
                          (SELECT count(*) FROM console.lock_refs r
                            WHERE r.cluster_id = v.cluster_id AND r.vm_uuid = v.vm_uuid) AS refs
                   FROM console.check_vm v WHERE v.cluster_id = %s ORDER BY lower(v.vm_name)""", (cluster_id,))


def check_vm(cluster_id: str, vm_uuid: str) -> Optional[dict]:
    return _one('SELECT vm_uuid, vm_name, state, detail, first_open_at, last_check_at, excluded '
                'FROM console.check_vm WHERE cluster_id = %s AND vm_uuid = %s', (cluster_id, vm_uuid))


def open_flags(cluster_id: str) -> list[dict]:
    return _all("""SELECT vm_uuid, vm_name, detail, first_open_at, last_check_at FROM console.check_vm
                   WHERE cluster_id = %s AND state = 'open' AND NOT excluded ORDER BY first_open_at""", (cluster_id,))


def sync_vms(cluster_id: str, running: dict[str, str], stopped: dict[str, str]) -> None:
    """Make sure every VM has a row (name kept current); VMs that stopped lose their open flag."""
    with _conn() as c, c.cursor() as cur:
        for uuid, name in running.items():
            cur.execute("""INSERT INTO console.check_vm (cluster_id, vm_uuid, vm_name) VALUES (%s, %s, %s)
                           ON CONFLICT (cluster_id, vm_uuid) DO UPDATE SET vm_name = EXCLUDED.vm_name,
                             state = CASE WHEN console.check_vm.state = 'stopped' THEN 'pending'
                                          ELSE console.check_vm.state END""", (cluster_id, uuid, name))
        for uuid, name in stopped.items():
            cur.execute("""INSERT INTO console.check_vm (cluster_id, vm_uuid, vm_name, state) VALUES (%s, %s, %s, 'stopped')
                           ON CONFLICT (cluster_id, vm_uuid) DO UPDATE SET vm_name = EXCLUDED.vm_name,
                             state = 'stopped', detail = NULL, first_open_at = NULL""", (cluster_id, uuid, name))
        known = list(running) + list(stopped)
        cur.execute('DELETE FROM console.check_vm WHERE cluster_id = %s AND NOT (vm_uuid = ANY(%s))',
                    (cluster_id, known))


def record_verdict(cluster_id: str, vm_uuid: str, vm_name: Optional[str], state: str, detail: str,
                   image: Optional[bytes] = None) -> None:
    """Store a check result. An open session keeps its first-seen time across checks."""
    _exec("""INSERT INTO console.check_vm (cluster_id, vm_uuid, vm_name, state, detail, first_open_at, last_check_at, image, image_at)
             VALUES (%(c)s, %(v)s, %(n)s, %(s)s, %(d)s, CASE WHEN %(s)s = 'open' THEN now() END, now(),
                     %(img)s, CASE WHEN %(img)s IS NOT NULL THEN now() END)
             ON CONFLICT (cluster_id, vm_uuid) DO UPDATE SET vm_name = COALESCE(EXCLUDED.vm_name, console.check_vm.vm_name),
               state = EXCLUDED.state, detail = EXCLUDED.detail, last_check_at = now(),
               first_open_at = CASE WHEN EXCLUDED.state = 'open'
                                    THEN COALESCE(console.check_vm.first_open_at, now()) END,
               image = EXCLUDED.image, image_at = EXCLUDED.image_at""",
          {'c': cluster_id, 'v': vm_uuid, 'n': vm_name, 's': state, 'd': detail,
           'img': psycopg2.Binary(image) if image else None})


def set_excluded(cluster_id: str, vm_uuid: str, excluded: bool, username: Optional[str]) -> None:
    _exec("""INSERT INTO console.check_vm (cluster_id, vm_uuid, excluded, excluded_by) VALUES (%s, %s, %s, %s)
             ON CONFLICT (cluster_id, vm_uuid) DO UPDATE SET excluded = EXCLUDED.excluded,
               excluded_by = EXCLUDED.excluded_by,
               first_open_at = CASE WHEN EXCLUDED.excluded THEN NULL ELSE console.check_vm.first_open_at END,
               state = CASE WHEN EXCLUDED.excluded AND console.check_vm.state = 'open' THEN 'pending'
                            ELSE console.check_vm.state END""",
          (cluster_id, vm_uuid, bool(excluded), username if excluded else None))


def get_image(cluster_id: str, vm_uuid: str) -> Optional[tuple[bytes, datetime]]:
    row = _one('SELECT image, image_at FROM console.check_vm WHERE cluster_id = %s AND vm_uuid = %s AND image IS NOT NULL',
               (cluster_id, vm_uuid))
    return (bytes(row['image']), row['image_at']) if row else None


# ------------------------------------------------------------- references
def ref_hashes(cluster_id: str, vm_uuid: str) -> list[str]:
    return [r['hash'] for r in _all('SELECT hash FROM console.lock_refs WHERE cluster_id = %s AND vm_uuid = %s',
                                    (cluster_id, vm_uuid))]


def list_refs(cluster_id: str, vm_uuid: str) -> list[dict]:
    return _all('SELECT id, hash, thumb, created_at, created_by FROM console.lock_refs '
                'WHERE cluster_id = %s AND vm_uuid = %s ORDER BY created_at', (cluster_id, vm_uuid))


def add_ref(cluster_id: str, vm_uuid: str, hash_: str, thumb: bytes, username: Optional[str]) -> int:
    row = _one("""INSERT INTO console.lock_refs (cluster_id, vm_uuid, hash, thumb, created_by)
                  VALUES (%s, %s, %s, %s, %s) RETURNING id""",
               (cluster_id, vm_uuid, hash_, psycopg2.Binary(thumb), username))
    return int(row['id'])


def delete_ref(cluster_id: str, ref_id: int) -> bool:
    rows = _all('DELETE FROM console.lock_refs WHERE cluster_id = %s AND id = %s RETURNING id', (cluster_id, int(ref_id)))
    return bool(rows)
