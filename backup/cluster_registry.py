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

import json
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
    folder_name: Optional[str] = None
    api_version: str = 'auto'                  # auto | v1 | v2 (the switch)
    api_v2_available: Optional[bool] = None    # what the last probe found; None = never probed
    hc_version: Optional[str] = None
    active_host: Optional[str] = None          # node the services currently reach the cluster on
    timezone: Optional[str] = None             # IANA zone from HyperCore's /TimeZone (or typed by an admin)
    timezone_override: bool = False            # True = keep the typed zone, ignore what the cluster reports

    @property
    def base_url(self) -> str:
        return base_url(self.host)

    @property
    def effective_timezone(self) -> str:
        """The zone plan run times are read in: the cluster's, else the stack's TZ, else UTC."""
        return self.timezone or os.environ.get('TZ') or 'UTC'

    @property
    def uses_v2(self) -> bool:
        """REST v2 calls are made for this cluster: forced on, or auto and found available."""
        return self.api_version == 'v2' or (self.api_version == 'auto' and bool(self.api_v2_available))

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


API_VERSIONS = ('auto', 'v1', 'v2')


def probe_v2(session, v1_base: str, timeout: float = 10) -> bool:
    """True when the cluster answers on /rest/v2/TaskTag (HyperCore 9.8 and later; experimental)."""
    try:
        r = session.get(f"{v1_base.rstrip('/').rsplit('/rest/', 1)[0]}/rest/v2/TaskTag", params={'isActive': 'true', 'limitSize': 1}, timeout=timeout)
        return r.status_code == 200 and isinstance(r.json(), list)
    except Exception:       # noqa: BLE001
        return False


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


_COLUMNS = '''c.cluster_id::text AS cluster_id, c.cluster_name, c.hc_host, c.hc_username, c.verify_tls,
              c.enabled, c.is_template, c.hc_cluster_uuid, c.colour, c.last_seen_at, c.last_error,
              c.registered_at, c.updated_at, c.folder_id::text AS folder_id, f.name AS folder_name,
              c.api_version, c.api_v2_available, c.hc_version, c.active_host, c.timezone, c.timezone_override'''
_FROM = 'FROM config.clusters c LEFT JOIN config.folders f ON f.folder_id = c.folder_id'


def _row_to_cluster(r: dict) -> Cluster:
    return Cluster(
        cluster_id=r['cluster_id'], name=r['cluster_name'], host=r['hc_host'] or '',
        username=r['hc_username'] or '', verify_tls=bool(r['verify_tls']),
        enabled=bool(r['enabled']), is_template=bool(r['is_template']),
        hc_cluster_uuid=r['hc_cluster_uuid'], colour=r['colour'],
        last_seen_at=r['last_seen_at'], last_error=r['last_error'],
        registered_at=r['registered_at'], updated_at=r['updated_at'],
        folder_id=r.get('folder_id'), folder_name=r.get('folder_name'),
        api_version=r.get('api_version') or 'auto', api_v2_available=r.get('api_v2_available'), hc_version=r.get('hc_version'),
        active_host=r.get('active_host'), timezone=r.get('timezone'), timezone_override=bool(r.get('timezone_override')),
    )


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------

def list_all(include_template: bool = False) -> list[Cluster]:
    sql = f'SELECT {_COLUMNS} {_FROM}'
    if not include_template:
        sql += ' WHERE c.is_template = FALSE'
    sql += ' ORDER BY c.is_template DESC, lower(c.cluster_name)'
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
            cur.execute(f'SELECT {_COLUMNS} {_FROM} WHERE c.cluster_id = %s', (cluster_id,))
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
           hc_cluster_uuid: Optional[str] = None, api_version: str = 'auto',
           api_v2_available: Optional[bool] = None, hc_version: Optional[str] = None) -> str:
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
                        hc_cluster_uuid, enabled, is_template, is_active, api_version, api_v2_available, hc_version)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE, FALSE, TRUE, %s, %s, %s)
                   RETURNING cluster_id::text AS cluster_id''',
                (name, host, username, enc, bool(verify_tls), colour, hc_cluster_uuid,
                 api_version if api_version in API_VERSIONS else 'auto', api_v2_available, hc_version),
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
           enabled: Optional[bool] = None, folder_id: Optional[str] = ..., api_version: Optional[str] = None,
           timezone: Optional[str] = None) -> None:
    """Update the given fields; password is re-encrypted only when provided (non-empty).
    folder_id: a folder uuid to move the cluster, None to unfile it, omitted to leave it.
    timezone: an IANA name to override what the cluster reports, '' to follow the cluster again."""
    sets, params = [], []
    if timezone is not None:
        tz = timezone.strip()
        if tz:
            if not zone_is_valid(tz):
                raise ValueError(f'unknown time zone {tz!r}; use an IANA name such as Europe/Amsterdam or US/Eastern')
            sets.append('timezone = %s'); params.append(tz)
            sets.append('timezone_override = TRUE')
        else:
            sets.append('timezone_override = FALSE')
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
    if api_version is not None:
        if api_version not in API_VERSIONS:
            raise ValueError("api_version must be auto, v1 or v2")
        sets.append('api_version = %s'); params.append(api_version)
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


def zone_is_valid(name: str) -> bool:
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(name)
        return True
    except Exception:  # noqa: BLE001 — unknown key or no tzdata
        return False


def timezone_from_api(payload) -> Optional[str]:
    """The IANA name out of HyperCore's GET /TimeZone answer (a one-element list)."""
    items = payload if isinstance(payload, list) else [payload]
    for it in items:
        tz = (it or {}).get('timeZone') if isinstance(it, dict) else None
        if tz and zone_is_valid(tz):
            return tz
    return None


def mark_seen(cluster_id: str, hc_cluster_uuid: Optional[str] = None, *, api_v2_available: Optional[bool] = None,
              hc_version: Optional[str] = None, active_host: Optional[str] = None, timezone: Optional[str] = None) -> None:
    """Record a successful contact (and, when given, what the probe found about REST v2 and the
    HyperCore version, and the node address the contact went through). When the HyperCore uuid
    turns out to be shared with another enabled cluster, the newer registration is flagged
    instead of cleared."""
    sets, params = ['last_seen_at = NOW()', 'last_error = NULL'], []
    if hc_cluster_uuid:
        sets.append('hc_cluster_uuid = %s'); params.append(hc_cluster_uuid)
    if active_host:
        sets.append('active_host = %s'); params.append(normalize_host(active_host))
    if api_v2_available is not None:
        sets.append('api_v2_available = %s'); params.append(bool(api_v2_available))
    if hc_version:
        sets.append('hc_version = %s'); params.append(str(hc_version)[:40])
    if timezone and zone_is_valid(timezone):
        # what the cluster reports, unless an admin typed a zone (timezone_override)
        sets.append('timezone = CASE WHEN timezone_override THEN timezone ELSE %s END'); params.append(timezone)
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f'UPDATE config.clusters SET {", ".join(sets)} WHERE cluster_id = %s', params + [cluster_id])
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


# ---------------------------------------------------------------------------
# nodes — every node of a cluster as seen through GET /Node. HyperCore has no primary node:
# each one serves the REST API, so any present ONLINE/IN node is a valid address.
# ---------------------------------------------------------------------------

def is_ip(host: str) -> bool:
    """True when the stored host (without port) is an IPv4 or IPv6 literal."""
    import ipaddress
    h = normalize_host(host or '')
    if h.startswith('['):
        h = h[1:h.find(']')] if ']' in h else h[1:]
    elif h.count(':') == 1:
        h = h.split(':', 1)[0]
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        return False


def node_from_api(node: dict) -> dict[str, Any]:
    """The subset of a GET /Node record this module stores."""
    def _b(v):
        return None if v is None else bool(v)
    return {
        'node_uuid': node.get('uuid') or '',
        'peer_id': node.get('peerID'),
        'lan_ip': node.get('lanIP') or '',
        'backplane_ip': node.get('backplaneIP') or '',
        'network_status': node.get('networkStatus') or 'UNKNOWN',
        'disposition': node.get('currentDisposition') or 'UNKNOWN',
        'allow_running_vms': _b(node.get('allowRunningVMs')),
        'virtualization_online': _b(node.get('virtualizationOnline')),
        'supports_virtualization': _b(node.get('supportsVirtualization')),
    }


def resolve_node_address(seed_host: str, lan_ip: str, lookup=None) -> str:
    """The address the services use for a node. Registered by IP: the node's LAN IP as it is.
    Registered by DNS name: the reverse-resolved name of the LAN IP, so TLS verification
    matches the node's own certificate; when the lookup fails the IP is kept (and will fail a
    TLS check, which is the honest outcome)."""
    if not lan_ip or is_ip(seed_host):
        return lan_ip
    try:
        import socket
        name = (lookup or socket.gethostbyaddr)(lan_ip)
        name = name[0] if isinstance(name, tuple) else name
        return name or lan_ip
    except Exception:
        log.warning('cluster_registry: no reverse DNS for node %s (cluster registered as %s); keeping the IP',
                    lan_ip, seed_host)
        return lan_ip


def node_usable(node: dict) -> bool:
    """A present node the API can be reached on. Witness nodes count (no storage, no VMs,
    full API); the virtualization flags say nothing about the API."""
    return bool(node.get('present', True)) and node.get('network_status') == 'ONLINE' \
        and node.get('disposition') == 'IN' and bool(node.get('address') or node.get('lan_ip'))


def order_candidates(seed_host: str, active_host: Optional[str], known_nodes: list[dict],
                     current: Optional[str] = None) -> list[str]:
    """Addresses a service tries, in order: the one it uses now (else the cluster's recorded
    active host), then the other usable nodes rotated after it, then the registered address as
    a last resort. No node is preferred; the seed only matters before discovery."""
    usable = []
    for n in known_nodes or []:
        if node_usable(n):
            a = normalize_host(n.get('address') or n.get('lan_ip') or '')
            if a and a not in usable:
                usable.append(a)
    start = normalize_host(current or active_host or '')
    out = [start] if start else []
    if start in usable:
        i = usable.index(start)
        out += usable[i + 1:] + usable[:i]
    else:
        out += usable
    seed = normalize_host(seed_host or '')
    if seed and seed not in out:
        out.append(seed)
    return out


def candidate_hosts(cluster_id: str, current: Optional[str] = None) -> list[str]:
    """order_candidates() fed from the registry. Falls back to the registered address alone
    when the registry cannot be read, so a Postgres hiccup never blocks a login."""
    c = get(cluster_id)
    if c is None:
        return []
    try:
        known = nodes(cluster_id)
    except Exception as exc:
        log.warning('cluster_registry.candidate_hosts(%s): %s', cluster_id, exc)
        known = []
    return order_candidates(c.host, c.active_host, known, current=current)


def set_active_host(cluster_id: str, host: str) -> None:
    """Record the node the cluster is currently reached on (written on every switch)."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute('UPDATE config.clusters SET active_host = %s WHERE cluster_id = %s',
                        (normalize_host(host), cluster_id))
        conn.commit()
    except Exception as exc:
        log.warning('cluster_registry.set_active_host(%s): %s', cluster_id, exc)
    finally:
        conn.close()


def upsert_nodes(cluster_id: str, api_nodes: list[dict], seed_host: Optional[str] = None) -> None:
    """Store the node list of one GET /Node (raw records or node_from_api() rows). Nodes missing from the list are kept with
    present = FALSE. The reverse lookup (name-registered clusters only) runs once per new node
    or per changed LAN IP, never per poll. Never raises."""
    rows = [dict(n) if 'node_uuid' in n else node_from_api(n) for n in api_nodes or []]   # raw API or slim
    rows = [r for r in rows if r.get('node_uuid')]
    conn = _connect()
    try:
        with conn.cursor() as cur:
            if seed_host is None:
                cur.execute('SELECT hc_host FROM config.clusters WHERE cluster_id = %s', (cluster_id,))
                got = cur.fetchone()
                seed_host = (got or {}).get('hc_host') or ''
            cur.execute('SELECT node_uuid, lan_ip, address FROM config.cluster_nodes WHERE cluster_id = %s', (cluster_id,))
            known = {r['node_uuid']: r for r in cur.fetchall()}
            for r in rows:
                old = known.get(r['node_uuid'])
                if old and old['lan_ip'] == r['lan_ip'] and old['address']:
                    r['address'] = old['address']
                else:
                    r['address'] = resolve_node_address(seed_host, r['lan_ip'])
                cur.execute(
                    '''INSERT INTO config.cluster_nodes
                           (cluster_id, node_uuid, peer_id, lan_ip, backplane_ip, address, network_status, disposition,
                            allow_running_vms, virtualization_online, supports_virtualization, present, last_seen_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE, NOW())
                       ON CONFLICT (cluster_id, node_uuid) DO UPDATE SET
                           peer_id = EXCLUDED.peer_id, lan_ip = EXCLUDED.lan_ip, backplane_ip = EXCLUDED.backplane_ip,
                           address = EXCLUDED.address, network_status = EXCLUDED.network_status,
                           disposition = EXCLUDED.disposition, allow_running_vms = EXCLUDED.allow_running_vms,
                           virtualization_online = EXCLUDED.virtualization_online,
                           supports_virtualization = EXCLUDED.supports_virtualization,
                           present = TRUE, last_seen_at = NOW()''',
                    (cluster_id, r['node_uuid'], r['peer_id'], r['lan_ip'], r['backplane_ip'], r['address'],
                     r['network_status'], r['disposition'], r['allow_running_vms'], r['virtualization_online'],
                     r['supports_virtualization']),
                )
            seen = [r['node_uuid'] for r in rows]
            if seen:
                cur.execute('UPDATE config.cluster_nodes SET present = FALSE WHERE cluster_id = %s AND present AND NOT (node_uuid = ANY(%s))',
                            (cluster_id, seen))
        conn.commit()
    except Exception as exc:  # bookkeeping must never break a poll loop
        log.warning('cluster_registry.upsert_nodes(%s): %s', cluster_id, exc)
    finally:
        conn.close()


_NODE_COLUMNS = '''cluster_id::text AS cluster_id, node_uuid, peer_id, lan_ip, backplane_ip, address, network_status,
                   disposition, allow_running_vms, virtualization_online, supports_virtualization, present,
                   first_seen_at, last_seen_at'''


def _node_row(r: dict) -> dict[str, Any]:
    d = dict(r)
    for k in ('first_seen_at', 'last_seen_at'):
        if d.get(k) is not None:
            d[k] = d[k].isoformat()
    d['usable'] = node_usable(d)
    d['witness'] = d.get('supports_virtualization') is False
    return d


def nodes_for(cluster_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Known nodes per cluster, present ones first, then by peer id."""
    out: dict[str, list[dict[str, Any]]] = {cid: [] for cid in cluster_ids}
    if not cluster_ids:
        return out
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f'SELECT {_NODE_COLUMNS} FROM config.cluster_nodes WHERE cluster_id = ANY(%s::uuid[]) '
                        'ORDER BY present DESC, peer_id NULLS LAST, lan_ip', (cluster_ids,))
            for r in cur.fetchall():
                out.setdefault(r['cluster_id'], []).append(_node_row(r))
        return out
    finally:
        conn.close()


def nodes(cluster_id: str) -> list[dict[str, Any]]:
    return nodes_for([cluster_id]).get(cluster_id, [])


def node_owner(node_uuids: list[str]) -> Optional[dict[str, Any]]:
    """The registered cluster that already lists one of these node uuids, if any:
    {'cluster_id', 'cluster_name', 'peer_id', 'lan_ip'} of the first match."""
    ids = [u for u in (node_uuids or []) if u]
    if not ids:
        return None
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute('''SELECT n.cluster_id::text AS cluster_id, c.cluster_name, n.peer_id, n.lan_ip
                           FROM config.cluster_nodes n JOIN config.clusters c ON c.cluster_id = n.cluster_id
                           WHERE n.node_uuid = ANY(%s) ORDER BY c.registered_at LIMIT 1''', (ids,))
            r = cur.fetchone()
            return dict(r) if r else None
    finally:
        conn.close()


def forget_node(cluster_id: str, node_uuid: str) -> bool:
    """Remove a node row that is no longer present. A present node is never removed."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute('DELETE FROM config.cluster_nodes WHERE cluster_id = %s AND node_uuid = %s AND NOT present',
                        (cluster_id, node_uuid))
            n = cur.rowcount
        conn.commit()
        return n > 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# task feed (HyperCore REST v2), written by the collector, read by the dashboard
# ---------------------------------------------------------------------------
def record_tasks(cluster_id: str, rows: list[dict]) -> None:
    """Upsert normalised task rows (see hypercore_tasks.normalise) for one cluster."""
    if not rows:
        return
    conn = _connect()
    try:
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, '''
                INSERT INTO config.cluster_tasks (cluster_id, task_tag, kind, description, vm_name, object_uuid, state, progress,
                                                  node_uuids, params, created_at, modified_at, completed_at, seen_at)
                VALUES (%(cluster_id)s, %(task_tag)s, %(kind)s, %(description)s, %(vm_name)s, %(object_uuid)s, %(state)s, %(progress)s,
                        %(node_uuids)s, %(params)s, %(created_at)s, %(modified_at)s, %(completed_at)s, NOW())
                ON CONFLICT (cluster_id, task_tag) DO UPDATE SET
                    state = EXCLUDED.state, progress = EXCLUDED.progress, description = EXCLUDED.description,
                    modified_at = EXCLUDED.modified_at, completed_at = EXCLUDED.completed_at, node_uuids = EXCLUDED.node_uuids,
                    seen_at = NOW()''',
                [dict(r, cluster_id=cluster_id, node_uuids=json.dumps(r.get('node_uuids') or []), params=json.dumps(r.get('params') or []))
                 for r in rows if r.get('task_tag')])
        conn.commit()
    except Exception as exc:  # the feed is informational; never break a poll loop
        log.warning('cluster_registry.record_tasks(%s): %s', cluster_id, exc)
    finally:
        conn.close()


def settle_stale_tasks(cluster_id: str, active_tags: set[str], older_than_seconds: int = 180) -> None:
    """A task that was active but is no longer listed as active and has not been seen for a while
    is marked COMPLETE (the v2 feed drops finished tasks from the active list)."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute('''UPDATE config.cluster_tasks SET state = 'COMPLETE', progress = 100, completed_at = COALESCE(completed_at, NOW())
                           WHERE cluster_id = %s AND state IN ('QUEUED', 'RUNNING') AND NOT (task_tag = ANY(%s))
                             AND seen_at < NOW() - make_interval(secs => %s)''',
                        (cluster_id, list(active_tags), older_than_seconds))
            cur.execute("DELETE FROM config.cluster_tasks WHERE cluster_id = %s AND seen_at < NOW() - INTERVAL '7 days'", (cluster_id,))
        conn.commit()
    except Exception as exc:
        log.warning('cluster_registry.settle_stale_tasks(%s): %s', cluster_id, exc)
    finally:
        conn.close()


def list_tasks(cluster_id: str, limit: int = 40) -> list[dict[str, Any]]:
    """Active tasks first (oldest first), then the most recently finished."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute('''SELECT task_tag, kind, description, vm_name, object_uuid, state, progress, node_uuids, params,
                                  created_at, modified_at, completed_at, seen_at
                           FROM config.cluster_tasks WHERE cluster_id = %s
                           ORDER BY (state IN ('QUEUED', 'RUNNING')) DESC,
                                    CASE WHEN state IN ('QUEUED', 'RUNNING') THEN created_at END ASC,
                                    COALESCE(completed_at, modified_at, created_at) DESC
                           LIMIT %s''', (cluster_id, limit))
            rows = [dict(r) for r in cur.fetchall()]
        for r in rows:
            for k in ('created_at', 'modified_at', 'completed_at', 'seen_at'):
                if r.get(k) is not None:
                    r[k] = r[k].isoformat()
        return rows
    finally:
        conn.close()


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
        try:
            tz = timezone_from_api(s.get(f'{url}/TimeZone', timeout=timeout).json())
        except Exception:  # noqa: BLE001 — an old release without the endpoint
            tz = None
        return {
            'ok': True,
            'cluster_name': info.get('clusterName') or info.get('name') or '',
            'hc_cluster_uuid': info.get('uuid') or '',
            'version': info.get('icosVersion') or info.get('version') or '',
            'node_count': len(node_list),
            'nodes': [node_from_api(n) for n in node_list if isinstance(n, dict)],
            'api_v2': probe_v2(s, url, timeout),
            'timezone': tz,
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
            cur.execute('''SELECT f.folder_id::text AS folder_id, f.name, f.colour, f.created_at, f.share_id,
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


def update_folder(folder_id: str, name: Optional[str] = None, colour: Optional[str] = None,
                  share_id=...) -> None:
    """share_id: an exports.shares id to assign, None to clear, omitted to leave untouched."""
    sets, params = [], []
    if name is not None:
        sets.append('name = %s'); params.append(name.strip())
    if colour is not None:
        sets.append('colour = %s'); params.append(colour or None)
    if share_id is not ...:
        sets.append('share_id = %s'); params.append(int(share_id) if share_id not in (None, '', 0) else None)
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
