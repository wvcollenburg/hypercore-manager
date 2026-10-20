"""
cluster_registry.py — the registry of HyperCore clusters managed by this stack.

Clusters are registered in the dashboard (Settings → Clusters) and stored in
config.clusters with Fernet-encrypted credentials (see hcm_crypto.py). Every
service reads this table instead of SC_HOST / SC_USERNAME / SC_PASSWORD.

Row …0001 is the "Defaults" template: never polled, never listed as a cluster;
its config.tunables rows are the defaults copied to each newly created cluster.

Connection: PG_HOST / PG_PORT / PG_DB / PG_USER / PG_PASSWORD (the dashboard,
collector and balancer convention) with SENTINEL_PG_* accepted as fallback.

Canonical copy: shared/cluster_registry.py — run tools/sync_shared.py after editing.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import psycopg2
import psycopg2.extras

import hcm_crypto

log = logging.getLogger(__name__)

TEMPLATE_CLUSTER_ID = '00000000-0000-0000-0000-000000000001'

_UUID_RE = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')


@dataclass
class Cluster:
    cluster_id: str
    name: str
    host: str
    username: str
    verify_tls: bool
    enabled: bool
    is_template: bool
    hc_cluster_uuid: Optional[str]
    colour: Optional[str]
    last_seen_at: Optional[datetime]
    last_error: Optional[str]
    registered_at: Optional[datetime]
    updated_at: Optional[datetime]
    folder_id: Optional[str] = None

    @property
    def base_url(self) -> str:
        return base_url(self.host)

    def to_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        for k in ('last_seen_at', 'registered_at', 'updated_at'):
            if d[k] is not None:
                d[k] = d[k].isoformat()
        d['base_url'] = self.base_url
        return d


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def is_uuid(value: str) -> bool:
    return bool(value) and bool(_UUID_RE.match(str(value)))


def normalize_host(raw: str) -> str:
    """Accept '172.16.0.241', 'cluster.example.com:443', 'https://x/rest/v1' and
    return just host[:port]."""
    s = (raw or '').strip()
    s = re.sub(r'^[a-zA-Z]+://', '', s)
    s = s.split('/', 1)[0]
    return s.strip().lower()


def base_url(host: str) -> str:
    """REST base for a stored host value."""
    return f'https://{normalize_host(host)}/rest/v1'


def _env(*names: str, default: str = '') -> str:
    for n in names:
        v = os.getenv(n)
        if v:
            return v
    return default


def _connect():
    return psycopg2.connect(
        host=_env('PG_HOST', 'SENTINEL_PG_HOST', default='postgres'),
        port=int(_env('PG_PORT', 'SENTINEL_PG_PORT', default='5432')),
        dbname=_env('PG_DB', 'SENTINEL_PG_DB', default='hypercore_balancer'),
        user=_env('PG_USER', 'SENTINEL_PG_USER', default='sentinel'),
        password=_env('PG_PASSWORD', 'SENTINEL_PG_PASSWORD'),
        connect_timeout=5,
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


_COLUMNS = '''cluster_id::text AS cluster_id, cluster_name, hc_host, hc_username, verify_tls,
              enabled, is_template, hc_cluster_uuid, colour, last_seen_at, last_error,
              registered_at, updated_at, folder_id::text AS folder_id'''


def _row_to_cluster(r: dict) -> Cluster:
    return Cluster(
        cluster_id=r['cluster_id'], name=r['cluster_name'], host=r['hc_host'] or '',
        username=r['hc_username'] or '', verify_tls=bool(r['verify_tls']),
        enabled=bool(r['enabled']), is_template=bool(r['is_template']),
        hc_cluster_uuid=r['hc_cluster_uuid'], colour=r['colour'],
        last_seen_at=r['last_seen_at'], last_error=r['last_error'],
        registered_at=r['registered_at'], updated_at=r['updated_at'],
        folder_id=r.get('folder_id'),
    )


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------

def list_all(include_template: bool = False) -> list[Cluster]:
    sql = f'SELECT {_COLUMNS} FROM config.clusters'
    if not include_template:
        sql += ' WHERE is_template = FALSE'
    sql += ' ORDER BY is_template DESC, lower(cluster_name)'
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            return [_row_to_cluster(r) for r in cur.fetchall()]
    finally:
        conn.close()


def list_enabled() -> list[Cluster]:
    return [c for c in list_all() if c.enabled]


def get(cluster_id: str) -> Optional[Cluster]:
    if not is_uuid(cluster_id):
        return None
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f'SELECT {_COLUMNS} FROM config.clusters WHERE cluster_id = %s', (cluster_id,))
            r = cur.fetchone()
            return _row_to_cluster(r) if r else None
    finally:
        conn.close()


def credentials(cluster_id: str) -> tuple[str, str]:
    """Return (username, password) for a cluster, decrypting the stored password."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT hc_username, hc_password_enc FROM config.clusters WHERE cluster_id = %s',
                        (cluster_id,))
            r = cur.fetchone()
    finally:
        conn.close()
    if not r or not r['hc_password_enc']:
        return (r['hc_username'] if r else '') or '', ''
    return r['hc_username'] or '', hcm_crypto.decrypt(r['hc_password_enc'])


# ---------------------------------------------------------------------------
# writes (dashboard)
# ---------------------------------------------------------------------------

def create(name: str, host: str, username: str, password: str,
           verify_tls: bool = False, colour: Optional[str] = None,
           hc_cluster_uuid: Optional[str] = None) -> str:
    """Register a cluster. Copies the template's tunables and creates its sharing
    preferences row. Returns the new cluster_id."""
    name = (name or '').strip()
    host = normalize_host(host)
    if not name or not host or not username:
        raise ValueError('name, host and username are required')
    enc = hcm_crypto.encrypt(password or '')
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                '''INSERT INTO config.clusters
                       (cluster_name, hc_host, hc_username, hc_password_enc, verify_tls, colour,
                        hc_cluster_uuid, enabled, is_template, is_active)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE, FALSE, TRUE)
                   RETURNING cluster_id::text AS cluster_id''',
                (name, host, username, enc, bool(verify_tls), colour, hc_cluster_uuid),
            )
            cid = cur.fetchone()['cluster_id']
            # per-cluster tunables start as a copy of the defaults template
            cur.execute(
                '''INSERT INTO config.tunables (cluster_id, key, value, type)
                   SELECT %s, key, value, type FROM config.tunables WHERE cluster_id = %s
                   ON CONFLICT (cluster_id, key) DO NOTHING''',
                (cid, TEMPLATE_CLUSTER_ID),
            )
            cur.execute(
                'INSERT INTO config.sharing_prefs (cluster_id) VALUES (%s) ON CONFLICT DO NOTHING',
                (cid,),
            )
        conn.commit()
        return cid
    finally:
        conn.close()


def update(cluster_id: str, *, name: Optional[str] = None, host: Optional[str] = None,
           username: Optional[str] = None, password: Optional[str] = None,
           verify_tls: Optional[bool] = None, colour: Optional[str] = None,
           enabled: Optional[bool] = None, folder_id: Optional[str] = ...) -> None:
    """Update the given fields; password is re-encrypted only when provided (non-empty).
    folder_id: a folder uuid to move the cluster, None to unfile it, omitted to leave it."""
    sets, params = [], []
    if folder_id is not ...:
        sets.append('folder_id = %s'); params.append(folder_id if folder_id and is_uuid(folder_id) else None)
    if name is not None:
        sets.append('cluster_name = %s'); params.append(name.strip())
    if host is not None:
        sets.append('hc_host = %s'); params.append(normalize_host(host))
    if username is not None:
        sets.append('hc_username = %s'); params.append(username.strip())
    if password:
        sets.append('hc_password_enc = %s'); params.append(hcm_crypto.encrypt(password))
    if verify_tls is not None:
        sets.append('verify_tls = %s'); params.append(bool(verify_tls))
    if colour is not None:
        sets.append('colour = %s'); params.append(colour or None)
    if enabled is not None:
        sets.append('enabled = %s'); params.append(bool(enabled))
    if not sets:
        return
    sets.append('updated_at = NOW()')
    # a credential or host change invalidates the last error state
    if host is not None or username is not None or password:
        sets.append('last_error = NULL')
    params.append(cluster_id)
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f'UPDATE config.clusters SET {", ".join(sets)} '
                        f'WHERE cluster_id = %s AND is_template = FALSE', params)
        conn.commit()
    finally:
        conn.close()


def delete(cluster_id: str, purge: bool = False) -> None:
    """Remove a cluster. With purge=True also drop its configuration and Foresight
    conclusions; otherwise only the registry row and its tunables go (recommendation
    history stays, keyed by the old cluster_id)."""
    if cluster_id == TEMPLATE_CLUSTER_ID:
        raise ValueError('the defaults template cannot be deleted')
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute('DELETE FROM config.tunables WHERE cluster_id = %s', (cluster_id,))
            cur.execute('DELETE FROM config.sharing_prefs WHERE cluster_id = %s', (cluster_id,))
            cur.execute('DELETE FROM config.threshold_overrides WHERE cluster_id = %s', (cluster_id,))
            cur.execute('DELETE FROM config.module_toggles WHERE cluster_id = %s', (cluster_id,))
            cur.execute('DELETE FROM config.branding WHERE cluster_id = %s', (cluster_id,))
            if purge:
                for table in ('recommendations.resize_recommendations', 'recommendations.right_sizing',
                              'recommendations.forecast_snapshots', 'recommendations.resource_exhaustion',
                              'recommendations.cluster_health_scores', 'audit.recommendation_events',
                              'audit.action_log', 'audit.webhook_deliveries'):
                    cur.execute(f'DELETE FROM {table} WHERE cluster_id = %s', (cluster_id,))
                cur.execute('DELETE FROM config.clusters WHERE cluster_id = %s AND is_template = FALSE',
                            (cluster_id,))
            else:
                # keep the row so FK-bound history stays valid, but take it out of service
                cur.execute('''UPDATE config.clusters
                                  SET enabled = FALSE, is_active = FALSE, hc_password_enc = NULL,
                                      cluster_name = cluster_name || ' (removed ' || to_char(NOW(), 'YYYY-MM-DD HH24:MI') || ')',
                                      updated_at = NOW()
                                WHERE cluster_id = %s AND is_template = FALSE''', (cluster_id,))
        conn.commit()
    finally:
        conn.close()


def duplicate_groups(clusters: Optional[list[Cluster]] = None) -> list[list[Cluster]]:
    """Enabled clusters that share a HyperCore cluster uuid: the same physical cluster
    registered more than once (typically two node IPs). Ordered by registration time, so the
    first entry of each group is the one that keeps balancing."""
    clusters = clusters if clusters is not None else list_enabled()
    by_uuid: dict[str, list[Cluster]] = {}
    for c in clusters:
        if c.hc_cluster_uuid:
            by_uuid.setdefault(c.hc_cluster_uuid, []).append(c)
    groups = [sorted(g, key=lambda c: (c.registered_at or datetime.min.replace(tzinfo=None)).isoformat())
              for g in by_uuid.values() if len(g) > 1]
    return groups


def duplicate_of(cluster: Cluster, clusters: Optional[list[Cluster]] = None) -> Optional[Cluster]:
    """The earlier-registered cluster this one duplicates, or None."""
    for group in duplicate_groups(clusters):
        if any(c.cluster_id == cluster.cluster_id for c in group) and group[0].cluster_id != cluster.cluster_id:
            return group[0]
    return None


def mark_seen(cluster_id: str, hc_cluster_uuid: Optional[str] = None) -> None:
    """Record a successful contact. When the HyperCore uuid turns out to be shared with
    another enabled cluster, the newer registration is flagged instead of cleared."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            if hc_cluster_uuid:
                cur.execute('''UPDATE config.clusters SET last_seen_at = NOW(), last_error = NULL,
                               hc_cluster_uuid = %s WHERE cluster_id = %s''', (hc_cluster_uuid, cluster_id))
            else:
                cur.execute('''UPDATE config.clusters SET last_seen_at = NOW(), last_error = NULL
                               WHERE cluster_id = %s''', (cluster_id,))
        conn.commit()
    except Exception as exc:  # never let bookkeeping break a poll loop
        log.warning('cluster_registry.mark_seen(%s): %s', cluster_id, exc)
    finally:
        conn.close()
    if hc_cluster_uuid:
        try:
            me = get(cluster_id)
            dup = duplicate_of(me) if me else None
            if dup:
                mark_error(cluster_id, f"same HyperCore cluster as '{dup.name}' ({hc_cluster_uuid[:8]}…): "
                                       f"registered twice? Balancing is suspended for this entry.")
        except Exception as exc:
            log.warning('cluster_registry duplicate check(%s): %s', cluster_id, exc)


def mark_error(cluster_id: str, message: str) -> None:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute('UPDATE config.clusters SET last_error = %s WHERE cluster_id = %s',
                        ((message or '')[:500], cluster_id))
        conn.commit()
    except Exception as exc:
        log.warning('cluster_registry.mark_error(%s): %s', cluster_id, exc)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# connection test (dashboard "Test connection" and first contact)
# ---------------------------------------------------------------------------

def test_connection(host: str, username: str, password: str, verify_tls: bool = False,
                    timeout: int = 10) -> dict[str, Any]:
    """Log in, read /Cluster and /Node, log out. Never raises; returns a dict with
    ok, and on success cluster_name / hc_cluster_uuid / node_count / version."""
    import requests
    import urllib3
    if not verify_tls:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    url = base_url(host)
    s = requests.Session()
    s.verify = bool(verify_tls)
    try:
        r = s.post(f'{url}/login', json={'username': username, 'password': password}, timeout=timeout)
        if r.status_code in (401, 403):
            return {'ok': False, 'error': 'Authentication failed: check username and password'}
        r.raise_for_status()
        cluster = s.get(f'{url}/Cluster', timeout=timeout)
        cluster.raise_for_status()
        info = (cluster.json() or [{}])[0] if isinstance(cluster.json(), list) else (cluster.json() or {})
        nodes = s.get(f'{url}/Node', timeout=timeout)
        nodes.raise_for_status()
        node_list = nodes.json() or []
        return {
            'ok': True,
            'cluster_name': info.get('clusterName') or info.get('name') or '',
            'hc_cluster_uuid': info.get('uuid') or '',
            'version': info.get('icosVersion') or info.get('version') or '',
            'node_count': len(node_list),
        }
    except requests.exceptions.SSLError as exc:
        return {'ok': False, 'error': f'TLS error: {exc}'}
    except requests.exceptions.ConnectionError:
        return {'ok': False, 'error': f'Cannot reach {url}'}
    except requests.exceptions.Timeout:
        return {'ok': False, 'error': f'Timeout talking to {url}'}
    except Exception as exc:  # noqa: BLE001 — turn anything into a UI message
        return {'ok': False, 'error': str(exc)[:300]}
    finally:
        try:
            s.post(f'{url}/logout', timeout=3)
        except Exception:
            pass
        s.close()


# ---------------------------------------------------------------------------
# folders — grouping in the rail and access rights for non-admin users
# ---------------------------------------------------------------------------

def list_folders() -> list[dict[str, Any]]:
    """All folders with their member user ids and usernames."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute('''SELECT f.folder_id::text AS folder_id, f.name, f.colour, f.created_at,
                                  COALESCE(json_agg(json_build_object('user_id', u.user_id::text, 'username', u.username)
                                           ORDER BY u.username) FILTER (WHERE u.user_id IS NOT NULL), '[]') AS members
                             FROM config.folders f
                             LEFT JOIN config.folder_members m ON m.folder_id = f.folder_id
                             LEFT JOIN config.users u ON u.user_id = m.user_id
                            GROUP BY f.folder_id ORDER BY lower(f.name)''')
            rows = cur.fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d['created_at'] = d['created_at'].isoformat() if d['created_at'] else None
        out.append(d)
    return out


def create_folder(name: str, colour: Optional[str] = None) -> str:
    name = (name or '').strip()
    if not name:
        raise ValueError('folder name is required')
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute('INSERT INTO config.folders (name, colour) VALUES (%s, %s) RETURNING folder_id::text AS folder_id',
                        (name, colour or None))
            fid = cur.fetchone()['folder_id']
        conn.commit()
        return fid
    finally:
        conn.close()


def update_folder(folder_id: str, name: Optional[str] = None, colour: Optional[str] = None) -> None:
    sets, params = [], []
    if name is not None:
        sets.append('name = %s'); params.append(name.strip())
    if colour is not None:
        sets.append('colour = %s'); params.append(colour or None)
    if not sets:
        return
    sets.append('updated_at = NOW()'); params.append(folder_id)
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f'UPDATE config.folders SET {", ".join(sets)} WHERE folder_id = %s', params)
        conn.commit()
    finally:
        conn.close()


def delete_folder(folder_id: str) -> None:
    """Remove a folder; its clusters become unfiled (admin-only visibility), memberships go."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute('DELETE FROM config.folders WHERE folder_id = %s', (folder_id,))
        conn.commit()
    finally:
        conn.close()


def set_folder_member(folder_id: str, user_id: str, member: bool) -> None:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            if member:
                cur.execute('INSERT INTO config.folder_members (folder_id, user_id) VALUES (%s, %s) ON CONFLICT DO NOTHING',
                            (folder_id, user_id))
            else:
                cur.execute('DELETE FROM config.folder_members WHERE folder_id = %s AND user_id = %s', (folder_id, user_id))
        conn.commit()
    finally:
        conn.close()


def folder_ids_for_user(user_id: str) -> set[str]:
    """Folders a (non-admin) user may see."""
    if not is_uuid(user_id):
        return set()
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT folder_id::text AS folder_id FROM config.folder_members WHERE user_id = %s', (user_id,))
            return {r['folder_id'] for r in cur.fetchall()}
    finally:
        conn.close()
