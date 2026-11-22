"""
HyperCore Dashboard API
========================
A lightweight Flask API that queries InfluxDB and serves data to the frontend.

KEY DESIGN: Module Discovery
-----------------------------
Any new container in the stack just needs to write data to InfluxDB with a
distinct measurement name. The /api/modules endpoint auto-discovers all
measurements in the bucket, so the frontend can dynamically list and
display data from new modules without code changes.

Endpoints:
    GET /api/nodes          - Current node CPU/RAM metrics (last 1h)
    GET /api/drives         - Physical drive health & usage, grouped by drive (last 1h)
    GET /api/vms            - Current VM CPU metrics (last 1h)
    GET /api/vsds           - Virtual storage device I/O, grouped by VSD (last 1h)
    GET /api/migrations     - Migration event log (last 7d, configurable)
    GET /api/predictions    - Predictive engine recommendation log (last 7d, configurable)
    GET /api/forecast_runs  - Predictive engine per-run summary log (last 7d, configurable)
    GET /api/modules        - Auto-discovered measurement names in the bucket
    GET /api/query/<measurement> - Generic query for any measurement
    GET /                   - Serves the dashboard HTML

Foresight (Sentinel) endpoints — E1-E7:
    GET /api/sentinel/summary        - Overview: health score, counts (E3/E6)
    GET /api/sentinel/forecasts      - Capacity forecast snapshots (E1)
    GET /api/sentinel/rightsizing    - Right-sizing recommendations (E2)
    GET /api/sentinel/resize         - Concrete resize advice (E2/C3-C6)
    GET /api/sentinel/exhaustion     - Resource exhaustion alerts (E4)
    GET /api/sentinel/cluster-health - Cluster health history (E3)
    GET /api/sentinel/report.pdf     - Download PDF capacity report (E5)
    GET /api/sentinel/branding       - Fetch report branding config (E7)
    PUT /api/sentinel/branding       - Save report branding config (E7)
"""

import hashlib
import hmac
import io
import json
import os
import re
import secrets
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from functools import wraps

import psycopg2
import psycopg2.extras
from flask import Flask, g, jsonify, redirect, render_template, request, send_from_directory, Response, session, url_for
from flask.sessions import SecureCookieSessionInterface
from flask_sock import Sock
import simple_websocket
from influxdb_client import InfluxDBClient
import config_db
import user_db
import backup_scope
import vm_rows
import rbac
import access_db
import cluster_registry
import hypercore_tasks
import hcm_crypto
import storage_summary
from versioning import latest_semver_tag, is_newer, origin_to_web_url, resolve_apply, move_to_tag
from forwarding import body_cluster
from alerts import ALERT_LEVELS, ALERT_SEVERITY, active_items, condition_episodes, ms_iso

app = Flask(__name__, static_folder='static', template_folder='templates')
# No CORS: every page that calls this API is served by it (the guest pages too), so no other origin needs to
# read its answers. The session cookie is not sent on cross-site requests (SameSite=Lax), and is marked Secure
# when the request came over HTTPS (directly or through a TLS proxy), so plain-HTTP installs keep working.
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = os.getenv('HCM_SESSION_COOKIE_SECURE', '').strip().lower() in ('1', 'true', 'yes')


def _request_is_https() -> bool:
    return request.is_secure or request.headers.get('X-Forwarded-Proto', '').split(',')[0].strip().lower() == 'https'


class _SessionInterface(SecureCookieSessionInterface):
    def get_cookie_secure(self, app):              # called while the response of a request is made
        if app.config.get('SESSION_COOKIE_SECURE'):
            return True
        try:
            return _request_is_https()
        except RuntimeError:                       # outside a request
            return False


app.session_interface = _SessionInterface()
# websockets (VM console): pings keep reverse proxies with a read timeout from closing an idle console
app.config['SOCK_SERVER_OPTIONS'] = {'ping_interval': 25, 'max_message_size': None}
sock = Sock(app)


@app.template_global()
def theme_inline():
    """theme.css and theme.js written into the page, for pages a guest opens: only /guest/ is published to
    outsiders, so those pages cannot load /static/. Read once per process."""
    cached = getattr(theme_inline, '_cached', None)
    if cached is None:
        from markupsafe import Markup
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
        with open(os.path.join(base, 'theme.css'), encoding='utf-8') as f:
            css = f.read()
        with open(os.path.join(base, 'theme.js'), encoding='utf-8') as f:
            js = f.read()
        cached = theme_inline._cached = Markup(f'<style>{css}</style>\n<script>{js}</script>')
    return cached

# seed tunables DB from env vars on first run (no-op if already seeded)
config_db.seed_from_env()

# InfluxDB configuration — configurables, stay in .env, require restart to change
INFLUX_URL = os.getenv('INFLUX_URL', 'http://influxdb:8086')
INFLUX_TOKEN = os.getenv('INFLUX_TOKEN', 'super-secret-auth-token')
INFLUX_ORG = os.getenv('INFLUX_ORG', 'hypercore')
INFLUX_BUCKET = os.getenv('INFLUX_BUCKET', 'metrics')

# fallback stale threshold — live value is read from config_db per request
STALE_SECONDS = int(os.getenv('SC_DASHBOARD_STALE_SECONDS', 120))

# session signing key — generated randomly at startup if not set (sessions won't
# survive restarts in that case, but everything still works for dev)
_secret_key = os.getenv('SC_DASHBOARD_SECRET_KEY', '') or secrets.token_hex(32)
app.secret_key = _secret_key

# bootstrap the users table and seed the admin account from env vars on first run
user_db.ensure_table()
user_db.seed_admin_from_env()
try:
    # accounts are email addresses: the ones without, except the main admin, wait for one (PLAN-roles, Accounts)
    _no_email = access_db.require_email_addresses(os.getenv('SC_DASHBOARD_USER', ''))
    if _no_email:
        app.logger.warning('accounts waiting for an email address: %s', ', '.join(_no_email))
except Exception as _exc:  # noqa: BLE001 — before migration 046, or the database is not up yet
    app.logger.info('email address check skipped: %s', _exc)

# auth is enabled iff at least one user row exists. Cached once the database has answered — restart
# is required if the table goes from empty to populated outside of the UI. Until it has answered
# (Postgres still starting, or down) authentication stays ON: an unreachable database is not "no users".
_AUTH_ENABLED = True
_auth_known = False
_auth_checked_at = 0.0
_auth_lock = threading.Lock()


def _refresh_auth_enabled(min_interval: float = 5.0) -> None:
    """Ask the database whether accounts exist, until it has answered once. While the answer is unknown
    every request is treated as needing a login; the question is repeated at most every few seconds."""
    global _AUTH_ENABLED, _auth_known, _auth_checked_at
    if _auth_known:
        return
    with _auth_lock:
        if _auth_known or time.monotonic() - _auth_checked_at < min_interval:
            return
        first = _auth_checked_at == 0.0
        _auth_checked_at = time.monotonic()
        if not first:
            # the start-up bootstrap ran against a database that was not there: do it now, before
            # concluding that there are no accounts
            user_db.ensure_table()
            user_db.seed_admin_from_env()
        state = user_db.has_any_users()
        if state is None:
            app.logger.warning('user store unreachable: logins stay required until it answers')
            return
        _AUTH_ENABLED, _auth_known = bool(state), True


_refresh_auth_enabled()


class AccessDenied(Exception):
    """Raised inside a route when the user may not do this here: 403 (or 404 when `hide`)."""
    def __init__(self, message: str = 'Access denied', status: int = 403):
        super().__init__(message)
        self.status = status


def _access() -> 'rbac.Access':
    """The current user's resolved rights, once per request."""
    acc = g.get('hcm_access')
    if acc is None:
        acc = access_db.access_for(session.get('user_id'), superuser=not _AUTH_ENABLED)
        g.hcm_access = acc
    return acc


def _login_problem():
    """A response when the request has no usable login, else None."""
    if not _AUTH_ENABLED:
        return None
    if not session.get('user_id'):
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Authentication required'}), 401
        if request.method == 'GET' and _after_login(request.path) != url_for('index'):
            session['next'] = request.path        # a link from a mail (a guest request): continue there after login
        return redirect(url_for('login_page'))
    try:
        state = access_db.user_state(session.get('user_id'))
    except Exception as exc:  # noqa: BLE001 — database down: fail closed, but do not log the user out
        app.logger.warning('user state unavailable: %s', exc)
        return jsonify({'error': 'user store unavailable'}), 503
    if state is None or state.get('username') != session.get('username'):
        # a cookie signed before the users table was recreated (a reinstall that kept the secret key),
        # or a deleted user: the session passes the signature but its user is gone
        session.clear()
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Authentication required'}), 401
        return redirect(url_for('login_page'))
    if not state.get('enabled', True):
        session.clear()
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Your account is waiting for an administrator to assign its access'}), 401
        return redirect(url_for('login_page'))
    if int(state.get('session_gen') or 0) != int(session.get('gen') or 0):
        # the account's sessions were ended (password changed or reset, two-factor off or reset, new address):
        # a cookie from before, also a stolen one, no longer signs in
        session.clear()
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Authentication required'}), 401
        return redirect(url_for('login_page'))
    return None


def _start_session(u: dict) -> None:
    """Sign `u` in on this session (after the password, and the code when two-factor is on)."""
    session['user_id']  = str(u['user_id'])
    session['username'] = u['username']
    session['role']     = u['role']
    session['gen']      = int(u.get('session_gen') or 0)


_ROUTE_PERMS: dict = {}         # view function name -> (permission or None, scope); read by /api/access/routes


def requires(perm: str | None, scope: str = 'cluster'):
    """Every route declares exactly one permission (plans/PLAN-roles.md, deny by default).

    perm None: any logged-in user. scope:
      'cluster' — the selected cluster (?cluster=): the user must hold `perm` there, on the whole
                  cluster or (VM-level permissions) on some of its VMs; the handler narrows to VMs.
                  A cluster named in the request that fails is refused, never swapped for another.
      'any'     — held anywhere; the handler checks the exact target (a body cluster, a node, a VM).
      'root'    — on the whole installation.
    """
    if perm is not None and perm not in rbac.PERMISSIONS:
        raise ValueError(f'unknown permission {perm!r}')

    def deco(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            problem = _login_problem()
            if problem is not None:
                return problem
            g.hcm_perm = perm
            if perm is not None:
                acc = _access()
                if scope == 'root':
                    ok = acc.can(perm)
                elif scope == 'any':
                    ok = acc.held_anywhere(perm)
                else:
                    problem = _write_cluster_problem(perm, need=True)
                    if problem is not None:
                        return problem
                    # reads fall back to the first allowed cluster (the UI recovers after a cluster was taken
                    # away); a write was checked above: it names a cluster the user may use for this route
                    ok = bool(_visible_clusters(perm))
                if not ok:
                    if request.path.startswith('/api/'):
                        return jsonify({'error': 'Access denied', 'permission': perm}), 403
                    return Response('Access denied', status=403, mimetype='text/plain')
            return f(*args, **kwargs)
        wrapped._hcm_perm = (perm, scope)
        _ROUTE_PERMS[f.__name__] = (perm, scope)
        return wrapped
    return deco


def public(reason: str):
    """A route open without the permission check (login page, internal token routes, the guest
    console): named on purpose so the startup check can tell it from a forgotten declaration."""
    def deco(f):
        f._hcm_perm = ('public', reason)
        _ROUTE_PERMS[f.__name__] = ('public', reason)
        return f
    return deco


_UNSAFE_METHODS = frozenset({'POST', 'PUT', 'PATCH', 'DELETE'})


def _host_of(url_or_host: str) -> str:
    """'https://Hcm.Example:443/x' -> 'hcm.example' (scheme and default ports left out: a TLS proxy changes
    the scheme, not who the site is)."""
    v = (url_or_host or '').strip().lower()
    if '://' in v:
        v = urllib.parse.urlsplit(v).netloc
    v = v.rsplit('@', 1)[-1]
    for port in (':80', ':443'):
        if v.endswith(port):
            v = v[:-len(port)]
    return v


def _own_hosts() -> set:
    """The names this request may legitimately come from: the Host it was sent to and the one a reverse proxy
    names in X-Forwarded-Host (a cross-site page can set neither: X-Forwarded-Host is not a header a browser
    lets another origin add without a CORS preflight, which this app never answers)."""
    hosts = {_host_of(request.host)}
    fwd = (request.headers.get('X-Forwarded-Host') or '').split(',')[0].strip()
    if fwd:
        hosts.add(_host_of(fwd))
    return hosts


def _configured_hosts() -> set:
    """The addresses in Settings -> Mail (links and guest links): a proxy that rewrites Host still works."""
    import mailer
    out = set()
    try:
        s = mailer.settings()
        for k in ('public_url', 'guest_url'):
            if s.get(k):
                out.add(_host_of(s[k]))
    except Exception:  # noqa: BLE001 — no database: only the request's own host counts
        pass
    env = (os.getenv('HCM_GUEST_BASE_URL') or '').strip()
    if env:
        out.add(_host_of(env))
    return out


@app.before_request
def _same_origin_only():
    """Cross-site request forgery: a request that changes something must come from a page of this site.
    Browsers send Origin on such requests (Referer when they leave Origin out); a value naming another site is
    refused. A request with neither is a script or a non-browser client, which holds no victim's cookie."""
    if request.method not in _UNSAFE_METHODS or request.path.startswith('/internal/'):
        return None
    source = request.headers.get('Origin')
    if source is None:
        source = request.headers.get('Referer')
        if not source:
            return None
    source_host = _host_of(source) if source != 'null' else 'null'
    if source_host and (source_host in _own_hosts() or source_host in _configured_hosts()):
        return None
    app.logger.warning('refused a cross-site %s %s from %r', request.method, request.path, source[:200])
    if request.path.startswith('/api/'):
        return jsonify({'error': 'cross-site request refused'}), 403
    return Response('Cross-site request refused', status=403, mimetype='text/plain')


@app.before_request
def _access_fresh():
    _refresh_auth_enabled()
    access_db.begin_request()


@app.errorhandler(AccessDenied)
def _access_denied(e):
    return jsonify({'error': str(e)}), e.status


def get_influx_client():
    return InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)


# ---------------------------------------------------------------------------
# PostgreSQL helper — sentinel data lives in the recommendations schema
# ---------------------------------------------------------------------------

_PG_HOST = os.getenv('PG_HOST', 'postgres')
_PG_PORT = int(os.getenv('PG_PORT', 5432))
_PG_DB   = os.getenv('PG_DB',   'hypercore_balancer')
_PG_USER = os.getenv('PG_USER', 'sentinel')
_PG_PASS = os.getenv('PG_PASSWORD', '')


@contextmanager
def _pg():
    conn = psycopg2.connect(
        host=_PG_HOST, port=_PG_PORT, dbname=_PG_DB,
        user=_PG_USER, password=_PG_PASS,
        connect_timeout=10,
        cursor_factory=psycopg2.extras.RealDictCursor,
    )
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# cluster selection
# ---------------------------------------------------------------------------
# the frontend sends ?cluster=<uuid> on every data call. The registry is cached
# briefly so a page load with a dozen API calls costs one lookup, not twelve
_registry_cache = {'at': 0.0, 'clusters': []}
_REGISTRY_TTL = 10.0


def _enabled_clusters():
    now = time.time()
    if now - _registry_cache['at'] > _REGISTRY_TTL:
        try:
            _registry_cache['clusters'] = cluster_registry.list_enabled()
        except Exception as exc:
            app.logger.warning('cluster registry unavailable: %s', exc)
            _registry_cache['clusters'] = []
        _registry_cache['at'] = now
    return _registry_cache['clusters']


def _invalidate_registry_cache():
    _registry_cache['at'] = 0.0
    access_db.invalidate()


class BadRequest(Exception):
    pass


def _days_arg(default: int = 7) -> int:
    """?days= as a whole number (it is written into a Flux query: never pass the raw text)."""
    raw = request.args.get('days', str(default))
    try:
        return max(1, min(int(raw), 3650))
    except (TypeError, ValueError):
        raise BadRequest(f'days must be a whole number of days, not {raw!r}')


def _hours_arg(default: int = 1) -> int:
    """?hours= as a whole number of hours, clamped to a year; anything else is the caller's mistake (400), not a 500."""
    raw = request.args.get('hours', str(default))
    try:
        return max(1, min(int(raw), 8760))
    except (TypeError, ValueError):
        raise BadRequest(f'hours must be a whole number of hours, not {raw!r}')


@app.errorhandler(BadRequest)
def _bad_request(e):
    return jsonify({'error': str(e)}), 400


def _is_admin_request() -> bool:
    """Root admin (or auth disabled): sees and may do everything."""
    return _access().is_root_admin()


def _visible_clusters(perm: str | None = None):
    """Enabled clusters for this request: where the user holds `perm` (default: the route's own
    permission), on the whole cluster or on some of its VMs. Without one: where they hold anything."""
    enabled = _enabled_clusters()
    acc = _access()
    perm = perm or g.get('hcm_perm')
    ids = [c.cluster_id for c in enabled]
    allowed = set(acc.scope_for(perm, ids)) if perm else set(acc.visible_clusters(ids))
    return [c for c in enabled if c.cluster_id in allowed]


def _cluster_id() -> str:
    """Cluster the current request is about: ?cluster=<uuid> when the user may use it for this
    route, else the first such cluster, else the defaults template (no data). The route decorator
    has already refused a named cluster for anything but a read."""
    visible = _visible_clusters()
    wanted = (request.args.get('cluster') or '').strip() if request else ''
    if wanted and cluster_registry.is_uuid(wanted):
        if any(c.cluster_id == wanted for c in visible):
            return wanted
    return visible[0].cluster_id if visible else cluster_registry.TEMPLATE_CLUSTER_ID


def _write_cluster_problem(perm: str, need: bool):
    """For a request that changes something on a cluster: the cluster in ?cluster= must be one the user may use
    for `perm` (403), and with `need` it must be there at all (400). Never the first visible cluster instead:
    a write that names no cluster, or one taken away, would otherwise act on some other cluster. Reads are not
    checked here (they fall back, so the UI recovers). Returns an error response or None."""
    if request.method in ('GET', 'HEAD', 'OPTIONS'):
        return None
    wanted = (request.args.get('cluster') or '').strip()
    if not wanted:
        return (jsonify({'error': 'cluster is required'}), 400) if need else None
    if not cluster_registry.is_uuid(wanted) or wanted not in {c.cluster_id for c in _visible_clusters(perm)}:
        return jsonify({'error': 'Access denied', 'permission': perm}), 403
    return None


def _named_cluster(perm: str) -> str:
    """The cluster in ?cluster= when the user holds `perm` on the whole of it; refused otherwise."""
    wanted = (request.args.get('cluster') or '').strip() or _cluster_id()
    if not cluster_registry.is_uuid(wanted) or not _access().can(perm, wanted):
        raise AccessDenied('Access denied')
    return wanted


def _vm_scope(perm: str | None = None, cid: str | None = None):
    """For the selected cluster: 'all', or {'vms': [...], 'tags': [...]} for a user who holds `perm`
    only on some of its VMs."""
    cid = cid or _cluster_id()
    perm = perm or g.get('hcm_perm')
    return _access().scope_for(perm, [cid]).get(cid)


def _vm_tags(cid: str, vm_uuid: str) -> list:
    """A VM's tags (for tag-scoped access), from the console service's VM index (cached a minute there)."""
    for v in _vm_index_rows(cid):
        if v.get('uuid') == vm_uuid:
            return v.get('tags') or []
    return []


def _vm_index_rows(cid: str, fresh: bool = False) -> list:
    q = {'cluster': cid}
    if fresh:
        q['fresh'] = '1'
    status, raw, _ = _console_call('GET', 'vm-index', query=urllib.parse.urlencode(q), timeout=30)
    if status != 200:
        return []
    try:
        return json.loads(raw.decode() or '{}').get('vms') or []
    except ValueError:
        return []


def _can_vm(perm: str, cid: str, vm_uuid: str) -> bool:
    acc = _access()
    if perm in acc.cluster_perms(cid) or acc.superuser:
        return True
    if not acc.vm_grants(cid):
        return False
    return acc.can(perm, cid, vm_uuid, _vm_tags(cid, vm_uuid))


def _require_vm(perm: str, cid: str, vm_uuid: str) -> None:
    if not _can_vm(perm, cid, vm_uuid):
        raise AccessDenied('Access denied')


def _filter_vms(rows, scope, key: str = 'uuid', tags_key: str | None = None, cid: str | None = None):
    """Rows of one cluster's VMs narrowed to a VM scope ('all' keeps everything)."""
    if scope == 'all':
        return rows
    if not scope:
        return []
    tag_of = {}
    if scope.get('tags') and not tags_key:
        tag_of = {v.get('uuid'): v.get('tags') or [] for v in _vm_index_rows(cid or _cluster_id())}
    out = []
    for r in rows:
        u = r.get(key)
        tags = r.get(tags_key) if tags_key else tag_of.get(u, [])
        if rbac.vm_in_scope(scope, u, tags or []):
            out.append(r)
    return out


def _flux_cluster() -> str:
    """Flux filter line restricting a query to the selected cluster."""
    return f'|> filter(fn: (r) => r["cluster_id"] == "{_cluster_id()}")'


def _agg_window(hours: int) -> str:
    """Return an InfluxDB aggregateWindow duration that keeps point count ~150-200."""
    if hours <= 2:   return '1m'
    if hours <= 6:   return '2m'
    if hours <= 12:  return '5m'
    if hours <= 24:  return '10m'
    if hours <= 168: return '1h'
    if hours <= 720: return '4h'
    return '1d'


def _stale_seconds() -> float:
    """The STALE_SECONDS tunable, read once per request: a lookup opens a PostgreSQL connection,
    and calling it per node, VM or disk inside a list comprehension cost ~0.7 s per request."""
    return config_db.get('STALE_SECONDS') or STALE_SECONDS


def is_stale(history_list, stale_secs=None):
    """Returns True if the most recent data point is older than STALE_SECONDS."""
    if not history_list:
        return True
    if stale_secs is None:
        stale_secs = _stale_seconds()
    latest_time = max(h['time'] for h in history_list)
    latest_dt = datetime.fromisoformat(latest_time.replace('Z', '+00:00'))
    return (datetime.now(timezone.utc) - latest_dt).total_seconds() > stale_secs


# =============================================================================
# API: node metrics (last 1 hour, 1-minute windows)
# =============================================================================
@app.route('/api/nodes')
@requires('cluster.view')
def get_nodes():
    range_hours = _hours_arg()
    client = get_influx_client()
    try:
        query = f'''
        from(bucket: "{INFLUX_BUCKET}")
          |> range(start: -{range_hours}h)
          {_flux_cluster()}
          |> filter(fn: (r) => r["_measurement"] == "node_metrics")
          |> filter(fn: (r) => r["_field"] != "network_online" and r["_field"] != "virtualization_online" and r["_field"] != "allow_running_vms")
          |> aggregateWindow(every: {_agg_window(range_hours)}, fn: mean, createEmpty: false)
          |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
        '''
        result = client.query_api().query(org=INFLUX_ORG, query=query)
        
        nodes = {}
        for table in result:
            for record in table.records:
                node_uuid = record.values.get('node_uuid', 'unknown')
                peer_id = record.values.get('node_peer_id', 'unknown')
                
                if node_uuid not in nodes:
                    nodes[node_uuid] = {
                        'uuid': node_uuid,
                        'peer_id': peer_id,
                        'history': []
                    }
                
                nodes[node_uuid]['history'].append({
                    'time': record.get_time().isoformat(),
                    'cpu_usage': record.values.get('cpu_usage'),
                    'mem_usage_bytes': record.values.get('total_mem_usage_bytes'),
                    'mem_size_bytes': record.values.get('mem_size'),
                    'capacity_bytes': record.values.get('capacity_bytes'),
                })
                # num_threads is static — capture it once from any record
                if 'num_threads' not in nodes[node_uuid]:
                    nodes[node_uuid]['num_threads'] = int(record.values.get('num_threads') or 0)

        # fetch latest allocated vCPUs per node from vm_inventory. Node and state are tags, so a VM that
        # migrated or stopped inside the hour has several series: only its newest one counts, or it would be
        # added to every node it ran on
        # (and only the last polls are read, as in _vm_sizes: a deleted VM stops counting when it stops reporting)
        fresh = max(60, int(_stale_seconds()))
        vcpu_query = f'''
        from(bucket: "{INFLUX_BUCKET}")
          |> range(start: -{fresh}s)
          {_flux_cluster()}
          |> filter(fn: (r) => r["_measurement"] == "vm_inventory")
          |> filter(fn: (r) => r["_field"] == "num_vcpu")
          |> last()
          |> group(columns: ["vm_uuid"])
          |> sort(columns: ["_time"])
          |> last()
          |> filter(fn: (r) => r["vm_state"] == "RUNNING")
          |> group(columns: ["node_uuid"])
          |> sum()
        '''
        vcpu_result = client.query_api().query(org=INFLUX_ORG, query=vcpu_query)
        allocated_vcpus = {}
        for table in vcpu_result:
            for record in table.records:
                node_uuid = record.values.get('node_uuid', 'unknown')
                allocated_vcpus[node_uuid] = int(record.get_value() or 0)

        for node in nodes.values():
            threads = node.get('num_threads', 0)
            vcpus = allocated_vcpus.get(node['uuid'], 0)
            node['vcpu_ratio'] = round(vcpus / threads, 2) if threads > 0 else 0.0
            node['allocated_vcpus'] = vcpus

        # fetch true current CPU per node (last measurement, independent of time range)
        current_query = f'''
        from(bucket: "{INFLUX_BUCKET}")
          |> range(start: -5m)
          {_flux_cluster()}
          |> filter(fn: (r) => r["_measurement"] == "node_metrics")
          |> filter(fn: (r) => r["_field"] == "cpu_usage")
          |> last()
        '''
        current_result = client.query_api().query(org=INFLUX_ORG, query=current_query)
        for table in current_result:
            for record in table.records:
                nid = record.values.get('node_uuid', 'unknown')
                if nid in nodes:
                    nodes[nid]['current_cpu'] = record.get_value()

        # filter out nodes that haven't reported recently
        stale_secs = _stale_seconds()
        active = [n for n in nodes.values() if not is_stale(n['history'], stale_secs)]
        return jsonify(active)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        client.close()


# =============================================================================
# API: VM metrics (last 1 hour, 1-minute windows)
# =============================================================================
def _vm_sizes(client, vms: dict) -> None:
    """Adds mem_bytes, num_vcpu, os, state and disk_bytes to each VM of the list, and the VMs that are
    not running (from vm_inventory, which lists every VM). Memory and vCPUs come from
    vm_inventory; a VM's disks from its per-disk I/O samples (vsd_metrics carry vm_uuid and vsd_uuid),
    their sizes from block_device_metrics by vsd_uuid. CD-ROMs (mounted ISOs) do not count.
    Also when no VM is running (a passive DR cluster): the stopped ones are still listed."""
    q = client.query_api()
    # the inventory as of the last polls only: a deleted VM drops out as fast as a stopped one shows up
    fresh = max(60, int(_stale_seconds()))
    for table in q.query(org=INFLUX_ORG, query=f"""
        from(bucket: "{INFLUX_BUCKET}")
          |> range(start: -{fresh}s)
          {_flux_cluster()}
          |> filter(fn: (r) => r["_measurement"] == "vm_inventory" and (r["_field"] == "mem_bytes" or r["_field"] == "num_vcpu"))
          |> last()"""):
        for rec in table.records:
            uuid = rec.values.get('vm_uuid')
            state = rec.values.get('vm_state') or 'UNKNOWN'
            vm = vms.get(uuid)
            if vm is None and state != 'RUNNING':
                # a stopped VM has no performance samples but is listed (so it can be started)
                vm = vms[uuid] = {'uuid': uuid, 'name': rec.values.get('vm_name') or uuid,
                                  'node_uuid': rec.values.get('node_uuid'), 'node_peer_id': rec.values.get('node_peer_id'),
                                  'tag': rec.values.get('vm_tag') or '', 'vlan': rec.values.get('vm_vlan') or '0',
                                  'history': [], 'current_cpu': None}
            if vm is not None:
                vm[rec.get_field()] = rec.get_value()
                vm['os'] = rec.values.get('vm_os') or ''
                vm['state'] = state
    disks_of: dict[str, set] = {}
    for table in q.query(org=INFLUX_ORG, query=f"""
        from(bucket: "{INFLUX_BUCKET}")
          |> range(start: -15m)
          {_flux_cluster()}
          |> filter(fn: (r) => r["_measurement"] == "vsd_metrics" and r["_field"] == "read_iops")
          |> last()
          |> keep(columns: ["vm_uuid", "vsd_uuid", "vsd_type"])"""):
        for rec in table.records:
            if 'CDROM' not in str(rec.values.get('vsd_type') or '').upper():
                disks_of.setdefault(rec.values.get('vm_uuid'), set()).add(rec.values.get('vsd_uuid'))
    size: dict[str, int] = {}
    for table in q.query(org=INFLUX_ORG, query=f"""
        from(bucket: "{INFLUX_BUCKET}")
          |> range(start: -15m)
          {_flux_cluster()}
          |> filter(fn: (r) => r["_measurement"] == "block_device_metrics" and r["_field"] == "capacity_bytes")
          |> last()"""):
        for rec in table.records:
            if 'CDROM' not in str(rec.values.get('vsd_type') or '').upper():
                size[rec.values.get('vsd_uuid')] = int(rec.get_value() or 0)
    for uuid, vm in vms.items():
        disks = disks_of.get(uuid) or set()
        vm['disk_bytes'] = sum(size.get(d, 0) for d in disks) if disks else None


@app.route('/api/vms')
@requires('vm.view')
def get_vms():
    range_hours = _hours_arg()
    client = get_influx_client()
    try:
        query = f'''
        from(bucket: "{INFLUX_BUCKET}")
          |> range(start: -{range_hours}h)
          {_flux_cluster()}
          |> filter(fn: (r) => r["_measurement"] == "vm_metrics")
          |> aggregateWindow(every: {_agg_window(range_hours)}, fn: mean, createEmpty: false)
          |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
        '''
        result = client.query_api().query(org=INFLUX_ORG, query=query)
        
        # one row per VM, described as of its newest sample: a VM that migrated inside the range has a
        # series per node and belongs to the node it is on now (vm_rows.py)
        vms = vm_rows.fold((record.get_time(), record.values) for table in result for record in table.records)

        # fetch true current CPU per VM (last measurement, independent of time range)
        current_query = f'''
        from(bucket: "{INFLUX_BUCKET}")
          |> range(start: -5m)
          {_flux_cluster()}
          |> filter(fn: (r) => r["_measurement"] == "vm_metrics")
          |> filter(fn: (r) => r["_field"] == "cpu_usage")
          |> last()
        '''
        current_result = client.query_api().query(org=INFLUX_ORG, query=current_query)
        for table in current_result:
            for record in table.records:
                vid = record.values.get('vm_uuid', 'unknown')
                if vid in vms:
                    vms[vid]['current_cpu'] = record.get_value()

        # filter out VMs that haven't reported recently (deleted/stopped)
        stale_secs = _stale_seconds()
        active = [v for v in vms.values() if not is_stale(v['history'], stale_secs)]
        listed = {v['uuid']: v for v in active}
        _vm_sizes(client, listed)
        return jsonify(_filter_vms(list(listed.values()), _vm_scope()))
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        client.close()


# =============================================================================
# API: drive metrics (physical drives, nested under nodes)
# =============================================================================
@app.route('/api/drives')
@requires('cluster.view')
def get_drives():
    range_hours = _hours_arg()
    include_stale = request.args.get('include_stale', 'false').lower() == 'true'
    client = get_influx_client()
    try:
        query = f'''
        from(bucket: "{INFLUX_BUCKET}")
          |> range(start: -{range_hours}h)
          {_flux_cluster()}
          |> filter(fn: (r) => r["_measurement"] == "drive_metrics")
          |> aggregateWindow(every: {_agg_window(range_hours)}, fn: last, createEmpty: false)
          |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
        '''
        result = client.query_api().query(org=INFLUX_ORG, query=query)

        drives = {}
        for table in result:
            for record in table.records:
                drive_uuid = record.values.get('drive_uuid', 'unknown')

                if drive_uuid not in drives:
                    drives[drive_uuid] = {
                        'uuid': drive_uuid,
                        'node_uuid': record.values.get('node_uuid', 'unknown'),
                        'node_peer_id': record.values.get('node_peer_id', 'unknown'),
                        'slot': record.values.get('drive_slot', '?'),
                        'history': []
                    }

                entry = {
                    'time': record.get_time().isoformat(),
                    'used_bytes': record.values.get('used_bytes'),
                    'capacity_bytes': record.values.get('capacity_bytes'),
                    'error_count': record.values.get('error_count'),
                    'is_healthy': record.values.get('is_healthy'),
                }
                temp = record.values.get('temperature')
                if temp is not None:
                    entry['temperature'] = temp
                drives[drive_uuid]['history'].append(entry)

        # for current-state views (drive health cards) filter out stale drives.
        # for historical charts (include_stale=true) keep all drives so that
        # capacity/usage totals correctly reflect what was present at each point in time
        if include_stale:
            active = list(drives.values())
        else:
            stale_secs = _stale_seconds()
            active = [d for d in drives.values() if not is_stale(d['history'], stale_secs)]
        return jsonify(active)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        client.close()


# =============================================================================
# API: VSD metrics (virtual storage devices, nested under VMs)
# =============================================================================
@app.route('/api/vsds')
@requires('vm.view')
def get_vsds():
    """Per-disk history. With ?vm=<uuid> only that VM's disks (the VM detail panel); the overview
    views use /api/storage/summary instead, which carries no per-disk history."""
    range_hours = _hours_arg()
    vm_filter = (request.args.get('vm') or '').strip()
    vm_flux = f'|> filter(fn: (r) => r["vm_uuid"] == "{vm_filter}")' if re.fullmatch(r'[0-9a-fA-F-]{1,64}', vm_filter) else ''
    client = get_influx_client()
    try:
        query = f'''
        from(bucket: "{INFLUX_BUCKET}")
          |> range(start: -{range_hours}h)
          {_flux_cluster()}
          |> filter(fn: (r) => r["_measurement"] == "vsd_metrics")
          {vm_flux}
          |> aggregateWindow(every: {_agg_window(range_hours)}, fn: mean, createEmpty: false)
          |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
        '''
        result = client.query_api().query(org=INFLUX_ORG, query=query)
        
        vsds = {}
        for table in result:
            for record in table.records:
                vsd_uuid = record.values.get('vsd_uuid', 'unknown')
                
                if vsd_uuid not in vsds:
                    vsds[vsd_uuid] = {
                        'uuid': vsd_uuid,
                        'name': record.values.get('vsd_name', 'unknown'),
                        'type': record.values.get('vsd_type', 'unknown'),
                        'vm_uuid': record.values.get('vm_uuid', 'unknown'),
                        'vm_name': record.values.get('vm_name', 'Unknown'),
                        'node_uuid': record.values.get('node_uuid', 'unknown'),
                        'history': []
                    }
                
                vsds[vsd_uuid]['history'].append({
                    'time': record.get_time().isoformat(),
                    'read_kbps': record.values.get('read_kbps'),
                    'write_kbps': record.values.get('write_kbps'),
                    'read_latency_us': record.values.get('read_latency_us'),
                    'write_latency_us': record.values.get('write_latency_us'),
                    'read_iops': record.values.get('read_iops'),
                    'write_iops': record.values.get('write_iops'),
                    'capacity_bytes': record.values.get('capacity_bytes'),
                })
        
        # filter out VSDs that haven't reported recently
        stale_secs = _stale_seconds()
        active = [v for v in vsds.values() if not is_stale(v['history'], stale_secs)]
        return jsonify(_filter_vms(active, _vm_scope(), key='vm_uuid'))
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        client.close()


# =============================================================================
# API: storage overview — per-VM series + cluster totals, no per-disk history
# =============================================================================
@app.route('/api/storage/summary')
@requires('vm.view')
def get_storage_summary():
    """What the Infraviewer overview needs from the disks: per VM one series (IOPS and
    throughput summed over its disks, latency of the worst disk), a cluster total series, and
    each disk's newest sample for the VM cards. InfluxDB does the aggregation per VM per window,
    so the payload and the Python work scale with VMs x points, not disks x points."""
    range_hours = _hours_arg()
    client = get_influx_client()
    try:
        stale_secs = _stale_seconds()
        sum_q, max_q, latest_q = storage_summary.flux_queries(INFLUX_BUCKET, _flux_cluster(), range_hours, _agg_window(range_hours), stale_secs)
        api = client.query_api()
        rows = []
        for q in (sum_q, max_q):
            for table in api.query(org=INFLUX_ORG, query=q):
                for r in table.records:
                    rows.append((r.values.get('vm_uuid', 'unknown'), r.values.get('vm_name', 'Unknown'), r.get_field(),
                                 r.get_time().isoformat(), r.get_value()))
        latest = []
        for table in api.query(org=INFLUX_ORG, query=latest_q):
            for r in table.records:
                latest.append({'uuid': r.values.get('vsd_uuid', 'unknown'), 'name': r.values.get('vsd_name', 'unknown'),
                               'type': r.values.get('vsd_type', 'unknown'), 'vm_uuid': r.values.get('vm_uuid', 'unknown'),
                               'vm_name': r.values.get('vm_name', 'Unknown'), 'node_uuid': r.values.get('node_uuid', 'unknown'),
                               'time': r.get_time().isoformat(), **{f: r.values.get(f) for f in storage_summary.FIELDS}})
        out = storage_summary.build(rows, latest, datetime.now(timezone.utc), stale_secs)
        out['hours'] = range_hours
        scope = _vm_scope()
        if scope != 'all':
            # a user with only some VMs sees those, not the cluster's totals (that is cluster.view)
            out['vms'] = _filter_vms(out.get('vms') or [], scope)
            out['cluster'] = {'history': []}
        return jsonify(out)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        client.close()


# =============================================================================
# API: migration events (last 7 days by default)
# =============================================================================
@app.route('/api/cluster/tasks')
@requires('cluster.view')
def api_cluster_tasks():
    """Live activity of the selected cluster from the REST v2 task feed the collector keeps."""
    cid = _cluster_id()
    c = cluster_registry.get(cid)
    if not c or not c.uses_v2:
        return jsonify({'v2': False, 'tasks': []})
    try:
        return jsonify({'v2': True, 'tasks': cluster_registry.list_tasks(cid, 40)})
    except Exception as e:
        return jsonify({'v2': True, 'tasks': [], 'error': str(e)}), 200


@app.route('/api/migrations')
@requires('cluster.view')
def get_migrations():
    range_days = _days_arg()
    client = get_influx_client()
    try:
        query = f'''
        from(bucket: "{INFLUX_BUCKET}")
          |> range(start: -{range_days}d)
          {_flux_cluster()}
          |> filter(fn: (r) => r["_measurement"] == "migration_events")
          |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
        '''
        result = client.query_api().query(org=INFLUX_ORG, query=query)

        events = []
        for table in result:
            for record in table.records:
                events.append({
                    'time': record.get_time().isoformat(),
                    'vm_name': record.values.get('vm_name', 'Unknown'),
                    'vm_uuid': record.values.get('vm_uuid', ''),
                    'source_ip': record.values.get('source_ip', ''),
                    'target_ip': record.values.get('target_ip', ''),
                    'engine': record.values.get('engine', ''),
                    'mode': record.values.get('mode', ''),
                    'dry_run': record.values.get('dry_run', 'true'),
                    'reason': record.values.get('reason', '')
                })

        events.sort(key=lambda e: e['time'], reverse=True)
        return jsonify(hypercore_tasks.dedupe_external(events)[:50])
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        client.close()


# =============================================================================
# API: predictive engine — recommendation log
# =============================================================================
@app.route('/api/predictions')
@requires('cluster.view')
def get_predictions():
    range_days = _days_arg()
    client = get_influx_client()
    try:
        query = f'''
        from(bucket: "{INFLUX_BUCKET}")
          |> range(start: -{range_days}d)
          {_flux_cluster()}
          |> filter(fn: (r) => r["_measurement"] == "predictive_recommendations")
          |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
        '''
        result = client.query_api().query(org=INFLUX_ORG, query=query)

        events = []
        for table in result:
            for record in table.records:
                events.append({
                    'time':                 record.get_time().isoformat(),
                    'vm_name':              record.values.get('vm_name', 'Unknown'),
                    'vm_uuid':              record.values.get('vm_uuid', ''),
                    'source_ip':            record.values.get('source_ip', ''),
                    'target_ip':            record.values.get('target_ip', ''),
                    'reason':               record.values.get('reason', ''),
                    'peak_time_unix':       record.values.get('peak_time_unix', 0.0),
                    'predicted_peak_load':  record.values.get('predicted_peak_load', 0.0),
                    'vm_predicted_cpu':     record.values.get('vm_predicted_cpu', 0.0),
                    'lead_seconds':         record.values.get('lead_seconds', 0.0),
                })

        events.sort(key=lambda e: e['time'], reverse=True)
        return jsonify(events[:200])
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        client.close()


# =============================================================================
# API: predictive engine — per-run summary log
# =============================================================================
@app.route('/api/forecast_runs')
@requires('cluster.view')
def get_forecast_runs():
    range_days = _days_arg()
    client = get_influx_client()
    try:
        query = f'''
        from(bucket: "{INFLUX_BUCKET}")
          |> range(start: -{range_days}d)
          {_flux_cluster()}
          |> filter(fn: (r) => r["_measurement"] == "predictive_runs")
          |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
        '''
        result = client.query_api().query(org=INFLUX_ORG, query=query)

        runs = []
        for table in result:
            for record in table.records:
                runs.append({
                    'time':                       record.get_time().isoformat(),
                    'success':                    record.values.get('success', 'true'),
                    'vms_total':                  record.values.get('vms_total', 0),
                    'vms_forecast':               record.values.get('vms_forecast', 0),
                    'vms_skipped_short_history':  record.values.get('vms_skipped_short_history', 0),
                    'vms_skipped_other':          record.values.get('vms_skipped_other', 0),
                    'nodes_flagged':              record.values.get('nodes_flagged', 0),
                    'recommendations':            record.values.get('recommendations', 0),
                    'peak_node_load_percent':     record.values.get('peak_node_load_percent', 0.0),
                    'peak_node_ip':               record.values.get('peak_node_ip', ''),
                    'threshold_used':             record.values.get('threshold_used', 0.0),
                    'lookback_days':              record.values.get('lookback_days', 0),
                    'min_history_hours':          record.values.get('min_history_hours', 0),
                    'duration_seconds':           record.values.get('duration_seconds', 0.0),
                    'error':                      record.values.get('error', ''),
                })

        runs.sort(key=lambda r: r['time'], reverse=True)
        return jsonify(runs[:200])
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        client.close()


# =============================================================================
# API: module discovery
# =============================================================================
# this is the key endpoint for extensibility. It queries InfluxDB for all
# measurement names in the bucket. When you add a new container that writes
# to a new measurement (e.g. "backup_events", "snapshot_metrics"), it shows
# up here automatically — no frontend code changes needed
# =============================================================================
@app.route('/api/modules')
@requires('install.manage', 'root')
def get_modules():
    client = get_influx_client()
    try:
        query = f'''
        import "influxdata/influxdb/schema"
        schema.measurements(bucket: "{INFLUX_BUCKET}")
        '''
        result = client.query_api().query(org=INFLUX_ORG, query=query)
        
        measurements = []
        for table in result:
            for record in table.records:
                measurements.append(record.get_value())
        
        return jsonify({
            'measurements': sorted(measurements),
            'description': 'All data sources writing to InfluxDB. Add a new container that writes to a new measurement and it appears here automatically.'
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        client.close()


# =============================================================================
# API: generic measurement query
# =============================================================================
# this lets the frontend query ANY measurement dynamically. When a new module
# appears in /api/modules, the frontend can immediately query its data without
# any backend changes
# =============================================================================
@app.route('/api/query/<measurement>')
@requires('install.manage', 'root')
def query_measurement(measurement):
    try:
        range_hours = _hours_arg()
        limit       = max(1, min(int(request.args.get('limit', '100')), 10000))
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid hours or limit parameter'}), 400
    client = get_influx_client()
    try:
        # sanitize measurement name (alphanumeric + underscores only)
        if not re.match(r'^[a-zA-Z0-9_]+$', measurement):
            return jsonify({'error': 'Invalid measurement name'}), 400

        query = f'''
        from(bucket: "{INFLUX_BUCKET}")
          |> range(start: -{range_hours}h)
          {_flux_cluster()}
          |> filter(fn: (r) => r["_measurement"] == "{measurement}")
          |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
          |> sort(columns: ["_time"], desc: true)
          |> limit(n: {limit})
        '''
        result = client.query_api().query(org=INFLUX_ORG, query=query)
        
        records = []
        for table in result:
            for record in table.records:
                row = {'time': record.get_time().isoformat()}
                for key, value in record.values.items():
                    if key not in ('result', 'table', '_start', '_stop', '_time', '_measurement'):
                        row[key] = value
                records.append(row)
        
        return jsonify(records)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        client.close()


# =============================================================================
# API: data quality — gap detection for forecast reliability warning
# =============================================================================
@app.route('/api/data-quality')
@requires('cluster.view')
def get_data_quality():
    """
    Checks the last 14 days of node_metrics for collection gaps.
    A gap is any 1-hour window with zero data points.
    Only active nodes (still reporting recently) are included — removed or
    failed nodes would otherwise inflate the gap count for every hour since
    they went silent.
    Returns {reliable, gap_count, total_gap_hours, checked_hours}.
    """
    WINDOW_DAYS   = 14     # history Foresight needs before forecasts are meaningful
    GAP_THRESHOLD = 0.08   # flag if >8 % of 1-hour windows are empty
    MIN_HOURS     = 24     # younger clusters have too little history to judge gaps
    REQUIRED_HOURS = WINDOW_DAYS * 24

    # the window never reaches back before the cluster was registered: a cluster added an
    # hour ago is not "missing" the previous 13 days
    cid = _cluster_id()
    since = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)
    try:
        c = cluster_registry.get(cid)
        if c and c.registered_at and c.registered_at > since:
            since = c.registered_at
    except Exception:
        pass
    window_hours = (datetime.now(timezone.utc) - since).total_seconds() / 3600
    progress = {
        'cluster_id':      cid,
        'window_hours':    round(window_hours, 1),
        'required_hours':  REQUIRED_HOURS,
        'history_pct':     round(min(100.0, window_hours / REQUIRED_HOURS * 100), 1),
        'enough_history':  window_hours >= REQUIRED_HOURS,
        'min_judge_hours': MIN_HOURS,
    }
    if window_hours < MIN_HOURS:
        return jsonify({'reliable': True, 'gap_count': 0, 'total_gap_hours': 0, 'checked_hours': 0,
                        'insufficient_history': True, **progress})
    range_start = since.strftime('%Y-%m-%dT%H:%M:%SZ')

    client = get_influx_client()
    try:
        # step 1: find nodes that have reported recently (active nodes only)
        stale_secs = config_db.get('STALE_SECONDS') or STALE_SECONDS
        active_query = f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -{stale_secs}s)
  {_flux_cluster()}
  |> filter(fn: (r) => r._measurement == "node_metrics")
  |> filter(fn: (r) => r._field == "cpu_usage")
  |> keep(columns: ["node_uuid"])
  |> distinct(column: "node_uuid")
'''
        active_tables = client.query_api().query(active_query, org=INFLUX_ORG)
        active_uuids  = [row.values['node_uuid']
                         for table in active_tables
                         for row in table.records
                         if row.values.get('node_uuid')]

        if not active_uuids:
            # no active nodes — nothing meaningful to check
            return jsonify({'reliable': True, 'gap_count': 0,
                            'total_gap_hours': 0, 'checked_hours': 0, **progress})

        # step 2: check for gaps only within those active nodes
        node_filter = ' or '.join(f'r.node_uuid == "{uid}"' for uid in active_uuids)
        query = f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: {range_start})
  {_flux_cluster()}
  |> filter(fn: (r) => r._measurement == "node_metrics")
  |> filter(fn: (r) => r._field == "cpu_usage")
  |> filter(fn: (r) => {node_filter})
  |> aggregateWindow(every: 1h, fn: count, createEmpty: true)
  |> keep(columns: ["_time", "_value"])
'''
        tables = client.query_api().query(query, org=INFLUX_ORG)
        windows       = [row.values['_value'] for table in tables for row in table.records]
        checked_hours = len(windows)
        gap_count     = sum(1 for v in windows if not v)

        if checked_hours == 0:
            return jsonify({'reliable': False, 'gap_count': 0,
                            'total_gap_hours': 0, 'checked_hours': 0, **progress})

        reliable = (gap_count / checked_hours) <= GAP_THRESHOLD
        return jsonify({
            'reliable':        reliable,
            'gap_count':       gap_count,
            'total_gap_hours': gap_count,
            'checked_hours':   checked_hours,
            **progress,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        client.close()


# =============================================================================
# API: tunables config (live read/write)
# =============================================================================
@app.route('/api/config', methods=['GET'])
@requires('cluster.view')
def get_config():
    """Return all tunables with metadata for the settings UI (selected cluster, or the
    defaults template when ?cluster=defaults)."""
    try:
        if request.args.get('cluster') == 'defaults' and not _access().held_anywhere('cluster.settings'):
            return jsonify({'error': 'Access denied'}), 403
        cid = config_db.TEMPLATE_CLUSTER_ID if request.args.get('cluster') == 'defaults' else _cluster_id()
        return jsonify(config_db.get_with_meta(cid))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/config', methods=['POST'])
@requires('cluster.settings', 'any')
def post_config():
    """Update one or more tunables. Accepts {key: value, ...} JSON body. Every value is cast and
    range-checked before anything is stored: one value no reader can cast (an emptied number
    field arrived as null and was stored as 'None') cost the cluster all its settings."""
    try:
        updates = request.get_json(force=True, silent=True)
        if not isinstance(updates, dict):
            return jsonify({'error': 'Expected a JSON object'}), 400
        try:
            updates = {k: config_db.validate(k, v) for k, v in updates.items()}
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        acc = _access()
        if request.args.get('cluster') == 'defaults':
            if not acc.can('install.manage'):
                return jsonify({'error': 'Access denied'}), 403
            cid = config_db.TEMPLATE_CLUSTER_ID
        else:
            cid = _named_cluster('cluster.settings')
        config_db.save_many(updates, cid)
        if request.args.get('apply_to_all') == '1':
            # every cluster this user may change settings on, never more
            for c in _enabled_clusters():
                if acc.can('cluster.settings', c.cluster_id):
                    config_db.save_many(updates, c.cluster_id)
        return jsonify({'ok': True})
    except AccessDenied:
        raise
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# =============================================================================
# cluster registry API — clusters are configured here, not in .env
# =============================================================================

def _rail_folders(acc, folders: list) -> list:
    """Only the 'folder' level of the tree goes into the rail's folder lists (MSPs and customers are
    tree nodes above them), each with its path and the permissions that decide its menu."""
    tree = acc.tree
    out = []
    see_all = acc.superuser or any(g.kind == 'root' for g in acc.grants)
    vis = None if see_all else acc.visible_nodes()
    mine = lambda n: n if n and (vis is None or n in vis) else None   # noqa: E731  (no id of a layer above either)
    for f in folders:
        fid = f['folder_id']
        if tree.level.get(fid, 'folder') != 'folder':
            continue
        anc = tree.ancestors(fid)
        out.append(dict(f, parent_id=mine(tree.parent.get(fid)),
                        customer_id=mine(next((a for a in anc if tree.level.get(a) == 'customer'), None)),
                        msp_id=mine(next((a for a in anc if tree.level.get(a) == 'msp'), None)),
                        path=(tree.path(fid) if see_all else acc.path(fid))[:-1],   # never the layers above the user
                        can_manage_tree=acc.can('tree.manage', node_id=fid),
                        can_manage_clusters=acc.can('cluster.manage', node_id=fid),
                        can_assign_share=acc.can('backup.targets.manage')))   # targets are listed at root only
    return out


def _cluster_payload(c, extras=None):
    d = c.to_dict()
    d['has_password'] = True
    if extras:
        d.update(extras.get(c.cluster_id, {}))
    return d


def _audit(action: str, target: str, node_id: str | None = None, detail: dict | None = None,
           actor: str | None = None) -> None:
    """One row in the access audit (Settings -> Access -> Audit), best effort: a failed audit write is logged
    and never fails the action it describes. node_id: where in the tree it happened (who may read the row)."""
    try:
        with _pg() as conn, conn.cursor() as cur:
            access_db.audit(cur, actor or session.get('username') or ('anonymous' if not _AUTH_ENABLED else 'system'),
                            action, str(target)[:300], node_id=node_id, detail=detail)
    except Exception as exc:  # noqa: BLE001
        app.logger.warning('audit %s %s not written: %s', action, target, exc)


def _set_cluster_role(cluster_id: str, role: str) -> None:
    """A DR cluster is normally passive: its backup proxies verify restore points around the clock."""
    with _pg() as conn, conn.cursor() as cur:
        cur.execute('UPDATE config.clusters SET role = %s, updated_at = NOW() WHERE cluster_id = %s', (role, cluster_id))
        access_db.audit(cur, session.get('username') or 'system', 'cluster.role', cluster_id, detail={'role': role})


def _cluster_extras(clusters):
    """Per-cluster facts for the rail: balancer mode and latest Foresight health score."""
    ids = [c.cluster_id for c in clusters]
    out = {cid: {'dry_run': None, 'health_score': None, 'health_severity': None} for cid in ids}
    if not ids:
        return out
    try:
        with _pg() as conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT cluster_id::text AS cluster_id, value FROM config.tunables
                               WHERE key = 'DRY_RUN' AND cluster_id = ANY(%s::uuid[])""", (ids,))
                for r in cur.fetchall():
                    out[r['cluster_id']]['dry_run'] = str(r['value']).lower() == 'true'
                cur.execute("""SELECT DISTINCT ON (cluster_id) cluster_id::text AS cluster_id,
                                      health_score, severity::text AS severity
                               FROM recommendations.cluster_health_scores
                               WHERE cluster_id = ANY(%s::uuid[]) AND expires_at > NOW()
                               ORDER BY cluster_id, calculated_at DESC""", (ids,))
                for r in cur.fetchall():
                    out[r['cluster_id']]['health_score'] = r['health_score']
                    out[r['cluster_id']]['health_severity'] = r['severity']
    except Exception as exc:
        app.logger.warning('cluster extras unavailable: %s', exc)
    try:
        with _pg() as conn, conn.cursor() as cur:
            cur.execute("SELECT cluster_id::text AS cluster_id, role FROM config.clusters WHERE cluster_id = ANY(%s::uuid[])", (ids,))
            for r in cur.fetchall():
                out.setdefault(r['cluster_id'], {})['role'] = r['role'] or 'production'
    except Exception as exc:                             # noqa: BLE001 — before migration 052
        app.logger.debug('cluster roles unavailable: %s', exc)
    try:
        for cid, nodes in cluster_registry.nodes_for(ids).items():
            out.setdefault(cid, {})['nodes'] = nodes
    except Exception as exc:
        app.logger.warning('cluster nodes unavailable: %s', exc)
    return out


@app.route('/api/clusters', methods=['GET'])
@requires(None)
def api_clusters_list():
    """All registered clusters (never the defaults template) plus the selection the
    server would make for this request, so the UI can initialise its picker."""
    try:
        clusters = cluster_registry.list_all()
        folders = cluster_registry.list_folders()
    except Exception as e:
        return jsonify({'error': str(e), 'clusters': [], 'folders': [], 'selected': None}), 200
    acc = _access()
    admin = acc.is_root_admin()
    folders = _rail_folders(acc, folders)
    if not admin:
        allowed = set(acc.visible_clusters([c.cluster_id for c in clusters]))
        clusters = [c for c in clusters if c.cluster_id in allowed]
        nodes = acc.visible_nodes()
        folders = [{k: v for k, v in f.items() if k != 'members'} for f in folders if f['folder_id'] in nodes]
    enabled = [c for c in clusters if c.enabled]
    extras = _cluster_extras(clusters)
    groups = cluster_registry.duplicate_groups(enabled)
    return jsonify({
        'clusters': [_cluster_payload(c, extras) for c in clusters],
        'folders': folders,
        'is_admin': admin,
        'enabled_count': len(enabled),
        'selected': _cluster_id() if enabled else None,
        'secret_key_configured': hcm_crypto.key_configured(),
        # entries that point at the same HyperCore cluster (same uuid): the first keeps balancing
        'duplicate_groups': [[c.cluster_id for c in g] for g in groups],
    })


def _cluster_form(body: dict) -> dict:
    return {
        'name':       (body.get('name') or '').strip(),
        'host':       cluster_registry.normalize_host(body.get('host') or ''),
        'username':   (body.get('username') or '').strip(),
        'password':   body.get('password') or '',
        'verify_tls': bool(body.get('verify_tls', False)),
        'colour':     (body.get('colour') or '').strip() or None,
        'api_version': (body.get('api_version') or 'auto').strip() if (body.get('api_version') or 'auto').strip() in ('auto', 'v1', 'v2') else 'auto',
    }


@app.route('/api/clusters', methods=['POST'])
@requires('cluster.manage', 'any')
def api_clusters_create():
    body = request.get_json(silent=True) or {}
    target_folder = (body.get('folder_id') or '').strip() or None
    if target_folder and not cluster_registry.is_uuid(target_folder):
        return jsonify({'error': 'invalid folder id'}), 400
    acc = _access()
    if not acc.can('cluster.manage', node_id=target_folder):
        return jsonify({'error': 'Access denied'}), 403
    if target_folder and acc.tree.level.get(target_folder) != 'folder':
        return jsonify({'error': 'a cluster belongs in a folder'}), 400
    f = _cluster_form(body)
    if not (f['name'] and f['host'] and f['username'] and f['password']):
        return jsonify({'error': 'name, host, username and password are required'}), 400
    if not hcm_crypto.key_configured():
        return jsonify({'error': f'{hcm_crypto.ENV_VAR} is not set on the dashboard; cannot store credentials'}), 500
    probe = cluster_registry.test_connection(f['host'], f['username'], f['password'], f['verify_tls'])
    if not probe.get('ok') and not body.get('force'):
        return jsonify({'error': probe.get('error', 'connection test failed'), 'probe': probe}), 400
    hc_uuid = probe.get('hc_cluster_uuid') or None
    probe_nodes = probe.get('nodes') or []
    if hc_uuid:
        dup = cluster_registry.registered_as(hc_uuid)          # a removed entry does not count
        if dup is None:
            # a cluster whose uuid we never learned (registered with force) still lists its nodes
            owner = cluster_registry.node_owner([n.get('node_uuid') for n in probe_nodes])
            dup = cluster_registry.get(owner['cluster_id']) if owner else None
        # a second entry for the same HyperCore cluster: only an administrator of the whole installation may
        # force one, and only someone who may see the first entry learns its name
        if dup and not (body.get('force') and acc.is_root_admin()):
            if dup.cluster_id not in set(acc.visible_clusters([dup.cluster_id])):
                return jsonify({'error': 'this cluster is already registered in HyperCore Manager; ask an administrator'}), 409
            mine = next((n for n in probe_nodes if n.get('lan_ip') and n['lan_ip'] == f['host']), None)
            which = f" (node {mine['peer_id']} of it)" if mine and mine.get('peer_id') is not None else ''
            return jsonify({'error': f"this cluster is already registered as '{dup.name}'{which}", 'duplicate_of': dup.cluster_id,
                            'may_force': acc.is_root_admin()}), 409
    try:
        cid = cluster_registry.create(f['name'], f['host'], f['username'], f['password'],
                                      verify_tls=f['verify_tls'], colour=f['colour'], hc_cluster_uuid=hc_uuid,
                                      api_version=f['api_version'], api_v2_available=probe.get('api_v2') if probe.get('ok') else None,
                                      hc_version=probe.get('version') or None, folder_id=target_folder)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        msg = str(e)
        if 'ux_clusters_folder_name' in msg or 'duplicate key' in msg:
            return jsonify({'error': 'this folder already has a cluster with that name'}), 409
        return jsonify({'error': msg}), 500
    if probe_nodes:
        cluster_registry.upsert_nodes(cid, probe_nodes, seed_host=f['host'])
    if probe.get('timezone'):
        cluster_registry.mark_seen(cid, timezone=probe['timezone'])
    _audit('cluster.register', f['name'], node_id=target_folder,
           detail={'cluster_id': cid, 'host': f['host'], 'forced': bool(body.get('force')) and not probe.get('ok')})
    _invalidate_registry_cache()
    c = cluster_registry.get(cid)
    return jsonify({'ok': True, 'cluster': _cluster_payload(c), 'probe': probe}), 201


@app.route('/api/clusters/<cluster_id>', methods=['PUT'])
@requires('cluster.manage', 'any')
def api_clusters_update(cluster_id):
    before = cluster_registry.get(cluster_id)
    if not before:
        return jsonify({'error': 'cluster not found'}), 404
    acc = _access()
    if not acc.can('cluster.manage', cluster_id):
        return jsonify({'error': 'cluster not found'}), 404
    body = request.get_json(silent=True) or {}
    if 'folder_id' in body and not _may_move_cluster(acc, cluster_id, body['folder_id'] or None):
        return jsonify({'error': 'Access denied'}), 403
    fields = {}
    for k in ('name', 'host', 'username', 'password', 'colour', 'api_version', 'timezone'):
        if k in body:
            fields[k] = body[k]
    for k in ('verify_tls', 'enabled'):
        if k in body:
            fields[k] = bool(body[k])
    if 'folder_id' in body:
        fields['folder_id'] = body['folder_id'] or None   # null unfiles the cluster
    role = body.get('role')
    if role is not None and role not in ('production', 'dr'):
        return jsonify({'error': "role must be 'production' or 'dr'"}), 400
    try:
        cluster_registry.update(cluster_id, **fields)
        if role is not None:
            _set_cluster_role(cluster_id, role)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        msg = str(e)
        if 'ux_clusters_folder_name' in msg or 'duplicate key' in msg:
            return jsonify({'error': 'that folder already has a cluster with that name'}), 409
        return jsonify({'error': msg}), 500
    changed = sorted(k for k in fields if k != 'folder_id' and (k != 'password' or fields[k]))
    if role is not None:
        changed.append('role')
    if changed:
        detail = {'cluster_id': cluster_id, 'fields': changed}
        if 'host' in fields:
            detail.update(host_from=before.host, host_to=fields['host'])
        if 'enabled' in fields:
            detail['enabled'] = fields['enabled']
        _audit('cluster.edit', before.name, node_id=before.folder_id, detail=detail)
    if 'folder_id' in fields and (fields['folder_id'] or None) != (before.folder_id or None):
        _audit('cluster.move', before.name, node_id=fields['folder_id'] or before.folder_id,
               detail={'cluster_id': cluster_id, 'from': before.folder_id, 'to': fields['folder_id']})
    _invalidate_registry_cache()
    return jsonify({'ok': True, 'cluster': _cluster_payload(cluster_registry.get(cluster_id))})


@app.route('/api/clusters/<cluster_id>', methods=['DELETE'])
@requires('cluster.manage', 'any')
def api_clusters_delete(cluster_id):
    c = cluster_registry.get(cluster_id)
    if not c or not _access().can('cluster.manage', cluster_id):
        return jsonify({'error': 'cluster not found'}), 404
    purge = request.args.get('purge') == '1'
    try:
        cluster_registry.delete(cluster_id, purge=purge)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    _audit('cluster.purge' if purge else 'cluster.remove', c.name, node_id=c.folder_id, detail={'cluster_id': cluster_id})
    _invalidate_registry_cache()
    return jsonify({'ok': True, 'purged': purge})


@app.route('/api/clusters/test', methods=['POST'])
@requires('cluster.manage', 'any')
def api_clusters_test_new():
    """Connection test for the add/edit form (credentials from the body)."""
    f = _cluster_form(request.get_json(silent=True) or {})
    if not (f['host'] and f['username']):
        return jsonify({'ok': False, 'error': 'host and username are required'}), 400
    return jsonify(cluster_registry.test_connection(f['host'], f['username'], f['password'], f['verify_tls']))


@app.route('/api/clusters/<cluster_id>/test', methods=['POST'])
@requires('cluster.manage', 'any')
def api_clusters_test_existing(cluster_id):
    """Connection test with the stored credentials; records the outcome on the cluster."""
    c = cluster_registry.get(cluster_id)
    if not c or not _access().can('cluster.manage', cluster_id):
        return jsonify({'ok': False, 'error': 'cluster not found'}), 404
    try:
        username, password = cluster_registry.credentials(cluster_id)
    except Exception as e:
        return jsonify({'ok': False, 'error': f'cannot decrypt stored password: {e}'}), 500
    result = _test_every_node(c, username, password)
    if result.get('ok'):
        cluster_registry.mark_seen(cluster_id, result.get('hc_cluster_uuid') or None,
                                   api_v2_available=result.get('api_v2'), hc_version=result.get('version') or None,
                                   active_host=result.get('via'), timezone=result.get('timezone'))
        if result.get('nodes'):
            cluster_registry.upsert_nodes(cluster_id, result['nodes'], seed_host=c.host)
    else:
        cluster_registry.mark_error(cluster_id, result.get('error', 'connection test failed'))
    _invalidate_registry_cache()
    return jsonify(result)


def _may_move_cluster(acc, cluster_id: str, folder_id) -> bool:
    """Moving a cluster to another folder (or out of every folder: root only). Both ends must be the
    user's to manage, and below root a cluster never leaves its customer (plans/PLAN-roles.md, Q3)."""
    if folder_id is not None and not cluster_registry.is_uuid(str(folder_id)):
        return False
    if acc.is_root_admin():
        return True
    if folder_id is None or not acc.can('cluster.manage', cluster_id) or not acc.can('cluster.manage', node_id=folder_id):
        return False
    tree = acc.tree
    here = tree.cluster_folder.get(cluster_id)
    cust = lambda n: next((a for a in tree.ancestors(n) if tree.level.get(a) == 'customer'), None)  # noqa: E731
    return cust(here) == cust(folder_id)


def _test_every_node(c, username, password) -> dict:
    """Log in on the configured address and on every known present node, in parallel (one
    login each). The first success supplies the cluster facts; per-node outcomes ride along."""
    from concurrent.futures import ThreadPoolExecutor
    addresses: list[tuple[str, object]] = []
    known = {}
    for n in cluster_registry.nodes(c.cluster_id):
        if n.get('present') and (n.get('address') or n.get('lan_ip')):
            known[cluster_registry.normalize_host(n.get('address') or n['lan_ip'])] = n.get('peer_id')
    seed = cluster_registry.normalize_host(c.host)
    if seed not in known:
        addresses.append((seed, None))
    addresses.extend(known.items())
    def probe(item):
        addr, peer = item
        r = cluster_registry.test_connection(addr, username, password, c.verify_tls)
        return addr, peer, r
    with ThreadPoolExecutor(max_workers=min(8, len(addresses))) as pool:
        probes = list(pool.map(probe, addresses))
    per_node = [{'address': a, 'peer_id': p, 'ok': bool(r.get('ok')), 'error': r.get('error')} for a, p, r in probes]
    first = next(((a, r) for a, p, r in probes if r.get('ok')), None)
    if first:
        out = dict(first[1]); out['via'] = first[0]
    else:
        out = {'ok': False, 'error': probes[0][2].get('error', 'connection test failed') if probes else 'no address to test'}
    out['nodes_tested'] = per_node
    out['nodes_ok'] = sum(1 for n in per_node if n['ok'])
    out['nodes_total'] = len(per_node)
    return out


MANUAL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'manual')


@app.route('/manual/')
@app.route('/manual/<slug>')
@requires(None)
def manual_page(slug=''):
    """The user manual, rendered from docs/manual (behind the login: it shows this installation's
    screenshots and names)."""
    import manual
    path = manual.resolve(MANUAL_DIR, slug)
    if path is None:
        return Response('<h1>Not found</h1>', status=404, mimetype='text/html')
    body = manual.render(path.read_text(encoding='utf-8'))
    nav = manual.chapters(MANUAL_DIR)
    title = next((c['title'] for c in nav if c['slug'] == slug), path.stem)
    return Response(manual.page(title, body, nav, slug, _read_version_file()), mimetype='text/html')


@app.route('/manual/img/<path:filename>')
@requires(None)
def manual_image(filename):
    return send_from_directory(os.path.join(MANUAL_DIR, 'img'), filename)


@app.route('/api/clusters/<cluster_id>/nodes/<node_uuid>', methods=['DELETE'])
@requires('cluster.manage', 'any')
def api_clusters_forget_node(cluster_id, node_uuid):
    """Drop a node row that is no longer part of the cluster (present nodes are kept)."""
    if not cluster_registry.get(cluster_id) or not _access().can('cluster.manage', cluster_id):
        return jsonify({'error': 'cluster not found'}), 404
    try:
        gone = cluster_registry.forget_node(cluster_id, node_uuid)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    if not gone:
        return jsonify({'error': 'node is still present on the cluster or unknown'}), 409
    _invalidate_registry_cache()
    return jsonify({'ok': True})


# folders: grouping in the rail plus access rights for non-admin users
@app.route('/api/folders', methods=['GET'])
@requires(None)
def api_folders_list():
    try:
        folders = cluster_registry.list_folders()
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    acc = _access()
    folders = _rail_folders(acc, folders)
    if not acc.is_root_admin():
        nodes = acc.visible_nodes()
        folders = [{k: v for k, v in f.items() if k != 'members'} for f in folders if f['folder_id'] in nodes]
    return jsonify(folders)


@app.route('/api/folders', methods=['POST'])
@requires('tree.manage', 'any')
def api_folders_create():
    body = request.get_json(silent=True) or {}
    acc = _access()
    parent = (body.get('parent_id') or '').strip() or None
    if parent is None:
        # the rail's "New folder": under the only customer of a single-company install
        customers = [n for n, lv in acc.tree.level.items() if lv == 'customer' and acc.can('tree.manage', node_id=n)]
        if len(customers) != 1:
            return jsonify({'error': 'choose the customer the folder belongs to'}), 400
        parent = customers[0]
    if not acc.can('tree.manage', node_id=parent):
        return jsonify({'error': 'Access denied'}), 403
    try:
        fid = access_db.create_node(body.get('name', ''), 'folder', parent, body.get('colour'),
                                    session.get('username') or 'anonymous')
    except access_db.AccessError as e:
        return jsonify({'error': str(e)}), e.status
    _invalidate_registry_cache()
    return jsonify({'ok': True, 'folder_id': fid}), 201


@app.route('/api/folders/<folder_id>', methods=['PUT'])
@requires('tree.manage', 'any')
def api_folders_update(folder_id):
    if not cluster_registry.is_uuid(folder_id):
        return jsonify({'error': 'invalid folder id'}), 400
    acc = _access()
    if not acc.can('tree.manage', node_id=folder_id):
        return jsonify({'error': 'Access denied'}), 403
    body = request.get_json(silent=True) or {}
    share = ...
    if 'share_id' in body:
        # a folder's backup target decides where its VMs' backups are written: targets are an installation
        # matter (listed to root target managers only), so a folder admin may not pick one, not even by id
        if not acc.can('backup.targets.manage'):
            return jsonify({'error': 'Access denied'}), 403
        share = body['share_id']
        if share not in (None, '', 0):
            try:
                share = int(share)
            except (TypeError, ValueError):
                return jsonify({'error': 'share_id must be a backup target id'}), 400
    try:
        cluster_registry.update_folder(folder_id, name=body.get('name'), colour=body.get('colour'), share_id=share)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        if 'ux_folders_name' in str(e) or 'duplicate key' in str(e):
            return jsonify({'error': 'a folder with that name already exists'}), 409
        if 'foreign key' in str(e).lower() and 'share' in str(e).lower():
            return jsonify({'error': 'no such backup target'}), 404
        return jsonify({'error': str(e)}), 500
    if share is not ...:
        _audit('folder.target', acc.tree.name.get(folder_id, folder_id), node_id=folder_id,
               detail={'target_id': share if share not in (None, '', 0) else None})
    _invalidate_registry_cache()
    return jsonify({'ok': True})


@app.route('/api/folders/<folder_id>', methods=['DELETE'])
@requires('tree.manage', 'any')
def api_folders_delete(folder_id):
    if not cluster_registry.is_uuid(folder_id):
        return jsonify({'error': 'invalid folder id'}), 400
    acc = _access()
    if not acc.can('tree.manage', node_id=acc.tree.parent.get(folder_id)):
        return jsonify({'error': 'Access denied'}), 403
    try:
        result = access_db.delete_node(folder_id, session.get('username') or 'anonymous')
    except access_db.AccessError as e:
        return jsonify({'error': str(e)}), e.status
    _invalidate_registry_cache()
    return jsonify({'ok': True, **result})


# =============================================================================
# Backup module proxy — the `backup` container owns plans, runs, imports and targets
# =============================================================================

_BACKUP_MUTATING = {'POST', 'PUT', 'DELETE'}


# keys that would reveal a target's path or account to a non-admin; the UI shows alias/folder only
_BACKUP_SENSITIVE_KEYS = frozenset({'base', 'user', 'username', 'path_uri_base', 'prune_path', 'smb_user',
                                    'source_uri', 'uri_base', 'uri', 'effective_base'})
# who did something, in the backup service's answers: names above the reader are not shown (as elsewhere)
_BACKUP_NAME_KEYS = ('created_by', 'requested_by', 'updated_by', 'muted_by')


def _hide_backup_names(obj, names):
    """Every dict in a backup API answer: the _BACKUP_NAME_KEYS the reader may not see named become None."""
    if isinstance(obj, list):
        for v in obj:
            _hide_backup_names(v, names)
    elif isinstance(obj, dict):
        if any(k in obj for k in _BACKUP_NAME_KEYS):
            access_db.hide_names([obj], _BACKUP_NAME_KEYS, names)
        for v in obj.values():
            if isinstance(v, (dict, list)):
                _hide_backup_names(v, names)
    return obj


def _strip_sensitive(obj):
    """Recursively drop target paths and accounts from a backup API response (non-admin callers)."""
    if isinstance(obj, dict):
        return {k: _strip_sensitive(v) for k, v in obj.items() if k not in _BACKUP_SENSITIVE_KEYS}
    if isinstance(obj, list):
        return [_strip_sensitive(v) for v in obj]
    return obj


def _backup_env():
    """BACKUP_* wins; the EXPORTS_* names from the module's first release still work."""
    url = os.getenv('BACKUP_API_URL') or os.getenv('EXPORTS_API_URL') or 'http://backup:8082'
    token = os.getenv('BACKUP_API_TOKEN') or os.getenv('EXPORTS_API_TOKEN') or ''
    return url, token


# ─── proxy image export: the backup service pushes this installation's proxy image to proxy VMs ───
_PROXY_IMAGE_NAME = 'hcm-proxy'


def _proxy_image_source() -> str:
    """The image compose built for the `proxy` service of this project."""
    return f"{os.getenv('COMPOSE_PROJECT_NAME', 'drsproject')}-proxy"


def _proxy_image_version() -> str | None:
    """The proxy version in the checkout the images were built from (proxy/identity.py)."""
    try:
        with open(os.path.join(_REPO_PATH, 'proxy', 'identity.py'), encoding='utf-8') as fh:
            m = re.search(r"^VERSION = '([^']+)'", fh.read(), re.M)
        return m.group(1) if m else None
    except OSError:
        return None


def _internal_token_ok() -> bool:
    """Bearer BACKUP_API_TOKEN. Without a token configured nothing is accepted: these routes are reachable on
    the dashboard's public port and hand out the proxy image (with its token-checking code)."""
    _, token = _backup_env()
    if not token:
        return False
    auth = request.headers.get('Authorization', '')
    return hmac.compare_digest(auth.encode(), f'Bearer {token}'.encode())


@app.route('/internal/proxy-image/version')
@public('backup service, bearer BACKUP_API_TOKEN')
def proxy_image_version():
    """{version, tag, available, detail}: what a push would carry. Bearer BACKUP_API_TOKEN."""
    if not _internal_token_ok():
        return jsonify({'error': 'Unauthorized'}), 401
    version = _proxy_image_version()
    src = _proxy_image_source()
    r = subprocess.run(['docker', 'image', 'inspect', src, '--format', '{{.Id}} {{.Architecture}}'], capture_output=True, text=True, timeout=20)
    if r.returncode != 0 or not version:
        detail = f'image {src} not built on this host' if r.returncode != 0 else 'proxy version not readable from the checkout'
        return jsonify({'version': version, 'tag': None, 'available': False, 'detail': detail})
    arch = (r.stdout.split() + ['', ''])[1]
    return jsonify({'version': version, 'tag': f'{_PROXY_IMAGE_NAME}:{version}', 'available': True, 'image': src, 'arch': arch})


@app.route('/internal/proxy-image')
@public('backup service, bearer BACKUP_API_TOKEN')
def proxy_image_export():
    """Stream `docker save` of the stack's proxy image, tagged hcm-proxy:<version>, to the backup
    service. Nothing is spooled; the tag travels in the X-Image-Tag header."""
    if not _internal_token_ok():
        return jsonify({'error': 'Unauthorized'}), 401
    version = _proxy_image_version()
    src = _proxy_image_source()
    if not version:
        return jsonify({'error': 'proxy version not readable from the checkout'}), 500
    tag = f'{_PROXY_IMAGE_NAME}:{version}'
    r = subprocess.run(['docker', 'tag', src, tag], capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        return jsonify({'error': f'image {src} not built on this host: {(r.stderr or r.stdout).strip()}'}), 500
    proc = subprocess.Popen(['docker', 'save', tag], stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def stream():
        try:
            while True:
                chunk = proc.stdout.read(1 << 20)
                if not chunk:
                    break
                yield chunk
        finally:
            proc.stdout.close()
            proc.wait()
            if proc.returncode != 0:
                app.logger.error('docker save %s failed: %s', tag, proc.stderr.read().decode(errors='replace').strip())
    return Response(stream(), mimetype='application/x-tar', headers={'X-Image-Tag': tag, 'X-Image-Version': version})


# The backup service knows no users; this table is the only thing standing between a browser and it.
# One line per endpoint: (methods, sub-path, permission, target). Anything not listed is refused.
# Targets:
#   'cluster'  the cluster named in the body, else the selected one; the whole cluster
#   'some'     the selected cluster, held there on some VMs at least (lists, narrowed to those VMs)
#   'vm'       the VM in the body (vm_uuid) or the query (?vm= / ?vm_uuid=) on that cluster
#   'folder'   the folder of the selected cluster (backup schedules belong to a folder)
#   'root'     the whole installation
#   'any'      held anywhere (nothing cluster-specific in it)
#   ('owner', kind[, group])  the resource whose id is in the path (or body), looked up in the service
#   'vmchange' VM edits: the permission follows the kind of change (power, delete, snapshot, ...)
_BACKUP_ROUTES = [
    ('GET',    r'vmedit/vm',                              'vm.view',               'vm'),
    ('GET',    r'vmedit/options',                         'vm.create',             'cluster'),
    ('POST',   r'vmedit/new',                             'vm.create',             'cluster'),
    ('GET',    r'vmedit/changes',                         'vm.view',               'some'),
    ('POST',   r'vmedit/changes',                         None,                    'vmchange'),
    ('POST',   r'vmedit/changes/(\d+)/cancel',            None,                    ('owner', 'change')),
    ('GET',    r'notices',                                'cluster.view',          'cluster'),
    ('POST',   r'notices/mute',                           'cluster.alerts.mute',   'cluster'),
    ('GET',    r'vms',                                    'backup.view',           'some'),
    ('GET',    r'plans',                                  'backup.view',           'folder_some'),
    ('POST',   r'plans',                                  'backup.schedule',       'vm'),
    ('PUT',    r'plans/(\d+)',                            'backup.schedule',       ('owner', 'plan')),
    ('POST',   r'plans/(\d+)/toggle',                     'backup.schedule',       ('owner', 'plan')),
    ('DELETE', r'plans/(\d+)',                            'backup.schedule',       ('owner', 'plan')),
    ('POST',   r'plans/(\d+)/run',                        'backup.run',            ('owner', 'plan')),
    ('GET',    r'policies',                               'backup.view',           'folder_some'),
    ('GET',    r'policies/(\d+)/night',                   'backup.view',           'folder_some'),
    ('POST',   r'policies/preview',                       'backup.schedule',       'any'),
    ('POST',   r'policies',                               'backup.schedule',       'folder'),
    ('PUT',    r'policies/(\d+)',                         'backup.schedule',       'folder'),
    ('POST',   r'policies/(\d+)/toggle',                  'backup.schedule',       'folder'),
    ('POST',   r'policies/(\d+)/run',                     'backup.run',            'folder'),
    ('DELETE', r'policies/(\d+)',                         'backup.schedule',       'folder'),
    ('POST',   r'policies/(\d+)/members',                 'backup.schedule',       'vm'),
    ('DELETE', r'policies/(\d+)/members/(\d+)',           'backup.schedule',       ('owner', 'plan', 2)),
    ('GET',    r'runs',                                   'backup.view',           'folder_some'),
    ('GET',    r'runs/status',                            'backup.view',           'folder_some'),
    ('GET',    r'import/known',                           'backup.import',         'cluster'),
    ('POST',   r'import/browse',                          'backup.import',         'cluster'),
    ('POST',   r'import/start',                           'backup.import',         'cluster'),
    ('GET',    r'targets',                                'backup.targets.manage', 'root'),
    ('POST',   r'targets(/probe|/\d+/test)?',             'backup.targets.manage', 'root'),
    ('PUT',    r'targets/\d+',                            'backup.targets.manage', 'root'),
    ('DELETE', r'targets/\d+',                            'backup.targets.manage', 'root'),
    ('GET',    r'proxies',                                'backup.proxies.manage', 'cluster'),
    ('POST',   r'proxies',                                'backup.proxies.manage', 'cluster'),
    ('GET',    r'proxies/image',                          'backup.proxies.manage', 'any'),
    ('GET',    r'proxies/deployments',                    'backup.proxies.manage', 'cluster'),
    ('DELETE', r'proxies/deployments',                    'backup.proxies.manage', 'cluster'),
    ('POST',   r'proxies/deploy',                         'backup.proxies.manage', 'cluster'),
    ('PUT',    r'proxies/(\d+)',                          'backup.proxies.manage', ('owner', 'proxy')),
    ('DELETE', r'proxies/(\d+)',                          'backup.proxies.manage', ('owner', 'proxy')),
    ('POST',   r'proxies/(\d+)/(update|test|bench)',      'backup.proxies.manage', ('owner', 'proxy')),
    ('GET',    r'proxies/(\d+)/(logs|bench)',             'backup.proxies.manage', ('owner', 'proxy')),
    ('GET',    r'test-plans',                             'backup.view',           'folder'),
    ('POST',   r'test-plans',                             'backup.schedule',       'folder'),
    ('PUT',    r'test-plans/(\d+)',                       'backup.schedule',       'folder'),
    ('DELETE', r'test-plans/(\d+)',                       'backup.schedule',       'folder'),
    ('POST',   r'test-plans/(\d+)/run',                   'backup.run',            'folder'),
    ('POST',   r'test-plans/(\d+)/vms',                   'backup.schedule',       'vm'),
    ('DELETE', r'test-plans/(\d+)/vms',                   'backup.schedule',       'folder'),
    ('GET',    r'test-runs',                              'backup.view',           'folder_some'),
    ('GET',    r'test-runs/(\d+)/screenshot',             'backup.view',           ('owner', 'test_run')),
    ('GET',    r'verification',                           'backup.view',           'folder'),
    ('PUT',    r'verification',                           'backup.schedule',       'folder'),
    ('GET',    r'chains',                                 'backup.view',           'folder_some'),
    ('GET',    r'chains/(\d+)/points',                    'backup.view',           ('owner', 'chain')),
    ('DELETE', r'chains/(\d+)',                           'backup.delete',         ('owner', 'chain')),
    ('POST',   r'chains/(\d+)/verify',                    'backup.verify',         ('owner', 'chain')),
    ('GET',    r'chains/(\d+)/verify',                    'backup.view',           ('owner', 'chain')),
    ('GET',    r'restores',                               'backup.view',           'folder_some'),
    ('POST',   r'restores',                               'backup.restore.full',   ('owner', 'point', 'point_id')),
    ('GET',    r'flr/mounts',                             'backup.browse',         'folder_some'),
    ('POST',   r'flr/mounts',                             'backup.browse',         ('owner', 'point', 'point_id')),
    ('GET',    r'flr/mounts/([0-9A-Za-z_-]+)/ls',         'backup.browse',         ('owner', 'mount')),
    ('GET',    r'flr/mounts/([0-9A-Za-z_-]+)/(file|zip)', 'backup.download',       ('owner', 'mount')),
    ('POST',   r'flr/mounts/([0-9A-Za-z_-]+)/iso',        'backup.restore.file',   ('owner', 'mount')),
    ('POST',   r'flr/iso/(\d+)/eject',                    'backup.restore.file',   ('owner', 'restore')),
    ('DELETE', r'flr/mounts/([0-9A-Za-z_-]+)',            'backup.browse',         ('owner', 'mount')),
]
_BACKUP_ROUTES_RE = [(m.split('|'), re.compile(rx), perm, tgt) for m, rx, perm, tgt in _BACKUP_ROUTES]

# VM edits: which permission a change set needs
_VMCHANGE_PERMS = {'edit': 'vm.edit', 'power': 'vm.power', 'delete': 'vm.delete', 'snapshot': 'vm.snapshot',
                   'clone': 'vm.clone', 'create': 'vm.create'}


def _backup_route(method: str, sub: str):
    for methods, rx, perm, tgt in _BACKUP_ROUTES_RE:
        if method in methods:
            m = rx.fullmatch(sub)
            if m:
                return perm, tgt, m
    return None


def _backup_call(method: str, sub: str, query: str = '', timeout: float = 15):
    url, token = _backup_env()
    rq = urllib.request.Request(f'{url}/api/v1/{sub}' + (f'?{query}' if query else ''), method=method,
                                headers={'Authorization': f'Bearer {token}'} if token else {})
    with urllib.request.urlopen(rq, timeout=timeout) as resp:
        return json.loads(resp.read().decode() or 'null')


def _backup_owner(kind: str, ident) -> dict | None:
    try:
        return _backup_call('GET', 'owner', urllib.parse.urlencode({'kind': kind, 'id': ident}))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def _vmchange_perms(payload: dict) -> list[str]:
    """Permissions a change set needs: its kind's, plus tag editing when it touches the tags."""
    kind = str(payload.get('kind') or 'edit')
    perms = [_VMCHANGE_PERMS.get(kind, 'vm.edit')]
    if kind == 'edit' and any((o or {}).get('op') == 'vm' and 'tags' in ((o or {}).get('fields') or {})
                              for o in payload.get('ops') or []):
        perms.append('vm.tags.edit')
    return perms


def _tags_of(value) -> set:
    if isinstance(value, list):
        return {str(t).strip() for t in value if str(t).strip()}
    return {t.strip() for t in str(value or '').split(',') if t.strip()}


def _tag_guard(cid: str, vm: str | None, payload: dict, kind: str) -> str | None:
    """Tags can hand out access (tag-scoped assignments), so adding or removing a tag that some assignment
    depends on needs users.manage on the cluster (plans/PLAN-roles.md, the escalation guard). Returns the
    refusal text, or None."""
    scoped = access_db.scoped_tags(cid)
    if not scoped or _access().can('users.manage', cid):
        return None
    old = set(_vm_tags(cid, vm)) if vm else set()
    if kind == 'edit':
        new = None
        for o in payload.get('ops') or []:
            f = (o or {}).get('fields') or {}
            if (o or {}).get('op') == 'vm' and 'tags' in f:
                new = _tags_of(f['tags'])
        if new is None:
            return None
        touched = (old ^ new) & set(scoped)
    elif kind == 'clone':
        # the backup service clones from ops[0] only (and a clone set has one op: _backup_check refuses more)
        ops = payload.get('ops') or []
        first = ops[0] if ops and isinstance(ops[0], dict) else {}
        new = _tags_of(first.get('tags'))
        touched = (new - old) & set(scoped)
    elif kind == 'create':
        touched = _tags_of(((payload.get('spec') or {}).get('tags'))) & set(scoped)
    else:
        return None
    if not touched:
        return None
    t = sorted(touched)[0]
    return f"the tag '{t}' gives access to this VM ({'; '.join(scoped[t][:3])}): changing it needs users.manage on the cluster"


def _backup_check(method: str, sub: str, cid: str, payload: dict | None):
    """(scope for narrowing lists or None, error response or None) for one forwarded request."""
    route = _backup_route(method, sub)
    if route is None:
        return None, (jsonify({'error': 'Not found'}), 404)
    perm, tgt, m = route
    acc = _access()
    denied = (jsonify({'error': 'Access denied'}), 403)
    payload = payload or {}
    body_cid = str(payload.get('cluster_id') or '') or cid
    if perm and not acc.held_anywhere(perm):
        return None, denied                          # deny first: input is only looked at for someone who may
    if perm is None and not any(acc.held_anywhere(p) for p in _VMCHANGE_PERMS.values()):
        return None, denied
    if request.args.get('all') and not acc.is_root_admin():
        return None, denied                          # ?all= lists every cluster's rows
    if tgt == 'root':
        return None, (None if acc.can(perm) else denied)
    if tgt == 'any':
        return None, (None if acc.held_anywhere(perm) else denied)
    if tgt == 'cluster':
        if not acc.can(perm, body_cid):
            return None, denied
        if sub == 'vmedit/new':
            why = _tag_guard(body_cid, None, payload, 'create')
            if why:
                return None, (jsonify({'error': why}), 403)
        return None, None
    if tgt == 'some':
        sc = acc.scope_for(perm, [cid]).get(cid)
        return sc, (None if sc else denied)
    if tgt == 'folder_some':
        # backups belong to the folder: a list covers every cluster of the selected cluster's folder on
        # which the user holds the right (?only= narrows to one of them), each row judged by its own cluster
        scopes = backup_scope.folder_scopes(acc, perm, cid)
        only = (request.args.get('only') or '').strip()
        if only:
            scopes = {only: scopes[only]} if only in scopes else {}
        return ({'multi': scopes} if scopes else None), (None if scopes else denied)
    if tgt == 'folder':
        return None, (None if acc.can(perm, node_id=acc.tree.cluster_folder.get(cid)) else denied)
    if tgt == 'vm':
        vm = str(payload.get('vm_uuid') or request.args.get('vm') or request.args.get('vm_uuid') or '')
        if not vm:
            return None, (jsonify({'error': 'vm_uuid required'}), 400)
        return None, (None if _can_vm(perm, body_cid, vm) else denied)
    if tgt == 'vmchange':
        vm = str(payload.get('vm_uuid') or '')
        if not vm:
            return None, (jsonify({'error': 'vm_uuid required'}), 400)
        ops = payload.get('ops')
        if ops is not None and not isinstance(ops, list):
            return None, (jsonify({'error': 'ops must be a list'}), 400)
        if str(payload.get('kind') or 'edit') in ('clone', 'snapshot') and len(ops or []) > 1:
            # the service acts on the first op only: what the guard checks must be all there is
            return None, (jsonify({'error': 'a clone or snapshot is one operation'}), 400)
        ok = all(_can_vm(p, body_cid, vm) for p in _vmchange_perms(payload))
        if not ok:
            return None, denied
        why = _tag_guard(body_cid, vm, payload, str(payload.get('kind') or 'edit'))
        return None, ((jsonify({'error': why}), 403) if why else None)
    if isinstance(tgt, tuple) and tgt[0] == 'owner':
        kind = tgt[1]
        where = tgt[2] if len(tgt) > 2 else 1
        ident = payload.get(where) if isinstance(where, str) else m.group(where)
        if ident in (None, ''):
            return None, (jsonify({'error': f'{where} required'}), 400)
        try:
            owner = _backup_owner(kind, ident)
        except Exception as e:  # noqa: BLE001
            return None, (jsonify({'error': f'Backup service unreachable: {e}'}), 503)
        if owner is None:
            return None, (jsonify({'error': 'Not found'}), 404)
        need = perm or _VMCHANGE_PERMS.get(owner.get('kind') or 'edit', 'vm.edit')
        if owner.get('vm_uuid'):
            ok = _can_vm(need, owner['cluster_id'], owner['vm_uuid'])
        else:
            ok = acc.can(need, owner['cluster_id'])
        if ok and sub.endswith('/iso') and payload.get('vm_uuid'):
            # files restored into a VM: that VM must be the user's to restore into as well
            ok = _can_vm('backup.restore.file', owner['cluster_id'], str(payload['vm_uuid']))
        # 404 rather than 403: do not confirm that another tenant's id exists
        return None, (None if ok else (jsonify({'error': 'Not found'}), 404))
    return None, (jsonify({'error': 'Not found'}), 404)


def _narrow_backup(sub: str, data, scope, cid: str):
    """A list answer narrowed to the VMs of a VM-scoped user ('all' passes untouched)."""
    if isinstance(scope, dict) and 'multi' in scope:
        return backup_scope.narrow(sub, data, scope['multi'], cid, _vm_index_rows)
    if scope == 'all' or data is None:
        return data
    tags = {v.get('uuid'): v.get('tags') or [] for v in _vm_index_rows(cid)} if scope.get('tags') else {}
    keep = lambda u: rbac.vm_in_scope(scope, u, tags.get(u, []))  # noqa: E731
    rows = lambda xs, key='vm_uuid': [x for x in (xs or []) if isinstance(x, dict) and keep(x.get(key))]  # noqa: E731
    if sub == 'vms':
        return rows(data, 'uuid')
    if sub in ('plans', 'chains', 'restores', 'flr/mounts', 'test-runs'):
        return rows(data)
    if sub == 'vmedit/changes':
        return dict(data, changes=rows(data.get('changes')))
    if sub in ('runs', 'runs/status'):
        # imports are cluster-level (backup.import): a VM-scoped user does not see them
        return dict(data, runs=rows(data.get('runs')), restores=rows(data.get('restores')), imports=[],
                    verifications=rows(data.get('verifications')))
    if sub == 'policies':
        pols = []
        for p in data.get('policies') or []:
            mine = rows(p.get('members'))
            if mine:
                pols.append(dict(p, members=mine))
        return dict(data, policies=pols)
    if re.fullmatch(r'policies/\d+/night', sub):
        items = rows(data.get('items'))
        if not items:
            return {'items': []}
        return dict(data, items=items)
    return data


# writes that create or change a VM's backup plan (directly, or by adding the VM to a schedule)
_PLAN_WRITE_RE = re.compile(r'plans|plans/\d+|policies/\d+/members|test-plans/\d+/vms')
# where a plan writes: only a root target manager chooses that; everyone else gets the folder's target
_BACKUP_DEST_KEYS = ('path_uri_base', 'smb_user', 'smb_password', 'prune_path')


def _clean_plan_payload(payload: dict, cid: str):
    """The VM's name comes from the cluster, not the browser: it is the export prefix and the key the plan's
    pruning deletes by, so a user allowed one VM could otherwise name another VM and have that VM's backups
    pruned. A destination of the browser's choosing is dropped unless the user manages targets on the whole
    installation (else a backup.schedule holder could send a VM's export to an SMB server of their own).
    Returns an error response or None."""
    if not _access().can('backup.targets.manage'):
        for k in _BACKUP_DEST_KEYS:
            payload.pop(k, None)
    vm = str(payload.get('vm_uuid') or '').strip()
    if not vm:
        payload.pop('vm_name', None)              # an update of an existing plan keeps the plan's own name
        return None
    def find(rows):
        return next((v.get('name') for v in rows if v.get('uuid') == vm), None)
    name = find(_vm_index_rows(cid)) or find(_vm_index_rows(cid, fresh=True))
    if not name:
        return jsonify({'error': 'that VM is not on the cluster'}), 404
    payload['vm_name'] = name
    return None


@app.route('/api/backup/status')
@requires('backup.view')
def backup_status():
    """Reachability of the optional backup container; the tab hides when it is off."""
    url, token = _backup_env()
    try:
        rq = urllib.request.Request(f'{url}/api/v1/status?cluster={_cluster_id()}',
                                    headers={'Authorization': f'Bearer {token}'} if token else {})
        with urllib.request.urlopen(rq, timeout=5) as resp:
            data = json.loads(resp.read().decode())
        data['available'] = True
        return jsonify(data if _access().can('backup.targets.manage') else _strip_sensitive(data))
    except Exception as e:  # noqa: BLE001
        # a slow answer is not a missing service: keep the tab (it hid while a node was down); only a
        # refused or unresolvable connection means the backup container is not running
        if _is_timeout(e):
            return jsonify({'available': True, 'status_slow': True})
        return jsonify({'available': False})


def _is_timeout(e: BaseException) -> bool:
    import socket
    reason = getattr(e, 'reason', e)
    return isinstance(reason, (socket.timeout, TimeoutError)) or 'timed out' in str(reason).lower()


@app.route('/api/backup/<path:sub>', methods=['GET', 'POST', 'PUT', 'DELETE'])
@requires(None)
def backup_proxy(sub):
    """Forward to the backup API with the selected cluster and the bearer token, after the check in
    _BACKUP_ROUTES. Lists are narrowed to the user's VMs; target paths and accounts are stripped for
    anyone who does not manage targets."""
    url, token = _backup_env()
    route = _backup_route(request.method, sub)
    g.hcm_perm = route[0] if route and route[0] else 'vm.view'
    if route is not None and route[1] not in ('root', 'any'):
        # a write acts on the cluster it names, never on the user's first one; a resource found by its id
        # ('owner') is checked on its own cluster, so it needs no cluster in the query, but may not name a wrong one
        problem = _write_cluster_problem(g.hcm_perm, need=not isinstance(route[1], tuple))
        if problem is not None:
            return problem
    cid = _cluster_id()
    payload = None
    if request.method in ('POST', 'PUT'):
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return jsonify({'error': 'expected a JSON object'}), 400
    scope, problem = _backup_check(request.method, sub, cid, payload)
    if problem is not None:
        return problem
    if request.method == 'POST' and sub == 'restores' and payload.get('target_cluster_id'):
        # onto another cluster: one of the folder of the point's cluster (the backup service checks the
        # point's folder too), with the right to restore there on the whole cluster
        tgt = str(payload['target_cluster_id'])
        acc = _access()
        try:
            owner = _backup_owner('point', payload.get('point_id')) or {}
        except Exception:                                    # noqa: BLE001 — unreachable: refuse, never guess
            owner = {}
        src = str(owner.get('cluster_id') or '')
        if not (src and cluster_registry.is_uuid(tgt) and acc.can('backup.restore.full', tgt)
                and acc.tree.cluster_folder.get(tgt) is not None
                and acc.tree.cluster_folder.get(tgt) == acc.tree.cluster_folder.get(src)):
            return jsonify({'error': 'you may not restore onto that cluster'}), 403
    # the validated cluster only: the backup service reads the first `cluster` it gets, so one sent by
    # the browser would otherwise win over the check above and reach a cluster the user may not see
    extra = [('cluster', cid)]
    if isinstance(scope, dict) and 'multi' in scope:          # the folder's clusters this user may see, checked above
        extra.append(('clusters', ','.join(sorted(scope['multi']))))
    qs = urllib.parse.urlencode([(k, v) for k, v in request.args.items(multi=True) if k not in ('cluster', 'clusters', 'only')] + extra)
    body = None
    headers = {'Authorization': f'Bearer {token}'} if token else {}
    if payload is not None:
        # a cluster named in the body is checked, not replaced (forms with their own cluster choice,
        # e.g. proxy deployment); a body without one gets the selected cluster where it is needed
        perm_for_body = g.hcm_perm
        body_cid, err = body_cluster(payload, cid, {c.cluster_id for c in _visible_clusters(perm_for_body)},
                                     fill='cluster_id' in payload or sub.endswith(('plans', 'import/start')))
        if err:
            return jsonify({'error': err}), 403
        if body_cid:
            payload['cluster_id'] = body_cid
        if request.method in ('POST', 'PUT') and _PLAN_WRITE_RE.fullmatch(sub):
            problem = _clean_plan_payload(payload, body_cid or cid)
            if problem is not None:
                return problem
        user = session.get('username') or ('anonymous' if not _AUTH_ENABLED else None)
        if sub == 'notices/mute':                    # who muted it is the session's user, not the body's
            payload['muted_by'] = user
        if sub.startswith('vmedit'):                 # who changed or cancelled it, likewise
            payload['created_by'] = user
        if sub in ('restores', 'flr/mounts') or sub.endswith('/iso'):
            payload['requested_by'] = user
        if sub == 'verification' or sub.startswith('test-plans'):
            payload['updated_by'] = user
        body = json.dumps(payload).encode()
        headers['Content-Type'] = 'application/json'
    if request.method == 'GET' and (sub.startswith('flr/mounts/') and sub.rsplit('/', 1)[-1] in ('file', 'zip')
                                    or re.fullmatch(r'proxies/\d+/logs', sub) or re.fullmatch(r'test-runs/\d+/screenshot', sub)):
        # downloads stream through; nothing in them is a target path or credential
        try:
            rq = urllib.request.Request(f'{url}/api/v1/{sub}?{qs}', headers=headers)
            resp = urllib.request.urlopen(rq, timeout=600)
        except urllib.error.HTTPError as e:
            return Response(e.read(), status=e.code, mimetype='application/json')
        except Exception as e:
            return jsonify({'error': f'Backup service unreachable: {e}'}), 503
        passthrough = {k: v for k, v in resp.headers.items() if k.lower() in ('content-length', 'content-disposition', 'content-type')}

        def gen():
            with resp:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    yield chunk
        return Response(gen(), status=resp.status, headers=passthrough)
    try:
        rq = urllib.request.Request(f'{url}/api/v1/{sub}?{qs}', data=body, headers=headers, method=request.method)
        with urllib.request.urlopen(rq, timeout=60) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:
        return Response(e.read(), status=e.code, mimetype='application/json')
    except Exception as e:
        return jsonify({'error': f'Backup service unreachable: {e}'}), 503
    sees_targets = _access().can('backup.targets.manage')
    if sees_targets and scope in (None, 'all'):
        return Response(raw, status=status, mimetype='application/json')
    try:
        data = json.loads(raw.decode() or 'null')
    except ValueError:
        return Response(raw, status=status, mimetype='application/json')
    if scope not in (None, 'all'):
        data = _narrow_backup(sub, data, scope, cid)
    if any(k.encode() in raw for k in _BACKUP_NAME_KEYS):
        names = access_db.nameable_usernames(_access(), session.get('username'))
        if names is not None:
            _hide_backup_names(data, names)
    return jsonify(data if sees_targets else _strip_sensitive(data)), status


# =============================================================================
# Cluster alerts (backlog #3): the badges on a cluster's card. One list per cluster from three
# sources: HyperCore conditions (latest Collector point per condition and node, from InfluxDB, no
# extra HyperCore login), the backup service's notices (a terminated update) and open console
# sessions found by the console check. Mutes live in config.notice_mutes, shared by all users.
# An overview, not alerting: up to one slow poll (5 min) late; HyperCore alerts by itself.
# =============================================================================
def _flux_set(values) -> str:
    return '[' + ', '.join(json.dumps(v) for v in values) + ']'


def _flux_clusters(cids: list[str]) -> str:
    """Flux filter line restricting a query to several clusters (all of them visible to the user)."""
    return f'|> filter(fn: (r) => contains(value: r["cluster_id"], set: {_flux_set(cids)}))'


def _active_conditions(cids: list[str]) -> dict[str, list[dict]]:
    """Per cluster: the conditions that are SET and OK right now (the spec: `value` must be ignored
    when isOK is false), from the latest point of each (condition, node)."""
    out: dict[str, list[dict]] = {c: [] for c in cids}
    if not cids:
        return out
    client = get_influx_client()
    try:
        query = f"""
        from(bucket: "{INFLUX_BUCKET}")
          |> range(start: -1h)
          {_flux_clusters(cids)}
          |> filter(fn: (r) => r["_measurement"] == "condition_metrics")
          |> filter(fn: (r) => r["_field"] == "value" or r["_field"] == "is_ok" or r["_field"] == "description" or r["_field"] == "last_set_time")
          |> last()
        """
        latest: dict[tuple, dict] = {}
        for table in client.query_api().query(org=INFLUX_ORG, query=query):
            for rec in table.records:
                v = rec.values
                key = (v.get('cluster_id'), v.get('condition_name'), v.get('node_uuid') or '')
                row = latest.setdefault(key, {'_t': {}, 'fields': {}})
                f = rec.get_field()
                t = rec.get_time()
                if f in row['_t'] and row['_t'][f] >= t:
                    continue                      # a severity change makes a second series; the newest wins
                row['_t'][f] = t
                row['fields'][f] = rec.get_value()
                if f == 'value':
                    row['severity'] = v.get('severity') or 'INFO'
                    row['node_ip'] = v.get('node_lan_ip') or ''
    finally:
        client.close()
    for cid, items in active_items(latest).items():
        out.setdefault(cid, []).extend(items)
    return out


def _backup_notices(cid: str) -> list[dict]:
    url, token = _backup_env()
    try:
        rq = urllib.request.Request(f'{url}/api/v1/notices?cluster={cid}',
                                    headers={'Authorization': f'Bearer {token}'} if token else {})
        with urllib.request.urlopen(rq, timeout=4) as resp:
            data = json.loads(resp.read().decode() or '{}')
    except Exception:  # noqa: BLE001 — the backup module is optional
        return []
    return [{'key': n.get('key'), 'kind': 'notice', 'notice_kind': n.get('kind'),
             'severity': n.get('severity') if n.get('severity') in ALERT_LEVELS else 'warning',
             'description': n.get('detail') or '', 'node_ip': n.get('node') or '', 'since': n.get('since'),
             'vm_name': n.get('vm'), 'seq': n.get('seq')}
            for n in data.get('notices') or [] if n.get('key')]


def _console_flags(cids: list[str]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {c: [] for c in cids}
    try:
        with _pg() as conn, conn.cursor() as cur:
            cur.execute("""SELECT cluster_id, vm_uuid, vm_name, first_open_at FROM console.check_vm
                           WHERE cluster_id = ANY(%s) AND state = 'open' AND NOT excluded""", (cids,))
            for r in cur.fetchall():
                out.setdefault(r['cluster_id'], []).append({
                    'key': f"console:{r['vm_uuid']}", 'kind': 'console', 'severity': 'warning',
                    'vm_uuid': r['vm_uuid'], 'vm_name': r['vm_name'] or r['vm_uuid'], 'node_ip': '',
                    'description': r['vm_name'] or r['vm_uuid'],
                    'since': r['first_open_at'].isoformat() if r['first_open_at'] else None})
    except psycopg2.Error:
        pass                                      # console schema not migrated yet
    return out


def _alert_mutes(cids: list[str]) -> dict[tuple, dict]:
    try:
        with _pg() as conn, conn.cursor() as cur:
            cur.execute('SELECT cluster_id, key, muted_until, muted_by FROM config.notice_mutes '
                        'WHERE cluster_id = ANY(%s) AND muted_until > now()', (cids,))
            return {(r['cluster_id'], r['key']): r for r in cur.fetchall()}
    except psycopg2.Error:
        return {}


@app.route('/api/cluster-alerts')
@requires('cluster.view', 'any')
def cluster_alerts():
    """{clusters: {id: {items, counts}}} for every visible cluster, or only ?cluster=<id>."""
    visible = [c.cluster_id for c in _visible_clusters()]
    wanted = request.args.get('cluster')
    cids = [c for c in visible if c == wanted] if wanted else visible
    try:
        conditions = _active_conditions(cids)
    except Exception as e:  # noqa: BLE001 — InfluxDB down: show the rest
        app.logger.warning('cluster alerts: conditions unavailable: %s', e)
        conditions = {c: [] for c in cids}
    consoles_open = _console_flags(cids)
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(cids)))) as pool:
        notices = dict(zip(cids, pool.map(_backup_notices, cids)))
    mutes = _alert_mutes(cids)
    out = {}
    for cid in cids:
        items = conditions.get(cid, []) + notices.get(cid, []) + consoles_open.get(cid, [])
        counts = dict.fromkeys(ALERT_LEVELS, 0)
        for it in items:
            m = mutes.get((cid, it['key']))
            it['muted_until'] = m['muted_until'].isoformat() if m else None
            it['muted_by'] = m['muted_by'] if m else None
            if not m:
                counts[it['severity']] += 1
        items.sort(key=lambda i: (ALERT_LEVELS.index(i['severity']), i.get('since') or '', i['key']))
        out[cid] = {'items': items, 'counts': counts}
    if mutes:
        # who muted it: not named to a reader below that person (an MSP's staff stay unnamed to a customer)
        names = access_db.nameable_usernames(_access(), session.get('username'))
        if names is not None:
            for v in out.values():
                access_db.hide_names(v['items'], ('muted_by',), names)
    return jsonify({'clusters': out})


@app.route('/api/cluster-alerts/mute', methods=['POST'])
@requires('cluster.alerts.mute', 'any')
def cluster_alert_mute():
    """{cluster_id, key, days}: mute one alert for 1-365 days, for every user; days 0 lifts it."""
    b = request.get_json(silent=True) or {}
    cid, key = str(b.get('cluster_id') or ''), str(b.get('key') or '').strip()
    if cid not in {c.cluster_id for c in _visible_clusters()} or not _access().can('cluster.alerts.mute', cid):
        return jsonify({'error': 'Unknown cluster'}), 404
    if not key or len(key) > 300:
        return jsonify({'error': 'key required'}), 400
    try:
        days = int(b.get('days'))
    except (TypeError, ValueError):
        return jsonify({'error': 'days must be a whole number'}), 400
    with _pg() as conn, conn.cursor() as cur:
        if days == 0:
            cur.execute('DELETE FROM config.notice_mutes WHERE cluster_id = %s AND key = %s', (cid, key))
        elif 1 <= days <= 365:
            cur.execute("""INSERT INTO config.notice_mutes (cluster_id, key, muted_until, muted_by, muted_at)
                           VALUES (%s, %s, now() + make_interval(days => %s), %s, now())
                           ON CONFLICT (cluster_id, key) DO UPDATE SET muted_until = EXCLUDED.muted_until,
                             muted_by = EXCLUDED.muted_by, muted_at = now()""",
                        (cid, key, days, session.get('username') or None))
        else:
            return jsonify({'error': 'days must be between 1 and 365'}), 400
    return jsonify({'ok': True})


@app.route('/api/cluster-alerts/history')
@requires('cluster.view')
def cluster_alert_history():
    """Past condition episodes of one cluster: ?cluster=&days=30&severity=critical,warning"""
    cid = _cluster_id()
    try:
        days = max(1, min(int(request.args.get('days', 30)), 365))
    except ValueError:
        return jsonify({'error': 'days must be a whole number'}), 400
    levels = {s for s in (request.args.get('severity') or '').split(',') if s in ALERT_LEVELS} or set(ALERT_LEVELS)
    raw_levels = [k for k, v in ALERT_SEVERITY.items() if v in levels]
    since_ms = int((time.time() - days * 86400) * 1000)
    base = f"""
        from(bucket: "{INFLUX_BUCKET}")
          |> range(start: -{days}d)
          {_flux_cluster()}
          |> filter(fn: (r) => r["_measurement"] == "condition_metrics")
          |> filter(fn: (r) => contains(value: r["severity"], set: {_flux_set(raw_levels)}))"""
    set_times: dict = {}
    clear_times: dict = {}
    latest: dict = {}
    meta: dict = {}
    client = get_influx_client()
    try:
        q = client.query_api()
        for field, target in (('last_set_time', set_times), ('last_clear_time', clear_times)):
            for table in q.query(org=INFLUX_ORG, query=base + f"""
          |> filter(fn: (r) => r["_field"] == "{field}")
          |> unique(column: "_value")"""):
                for rec in table.records:
                    key = (rec.values.get('condition_name'), rec.values.get('node_uuid') or '')
                    target.setdefault(key, set()).add(int(rec.get_value() or 0))
                    meta.setdefault(key, {'severity': rec.values.get('severity'), 'node_ip': rec.values.get('node_lan_ip') or ''})
        for table in q.query(org=INFLUX_ORG, query=base + """
          |> filter(fn: (r) => r["_field"] == "value" or r["_field"] == "is_ok" or r["_field"] == "last_set_time" or r["_field"] == "description")
          |> last()"""):
            for rec in table.records:
                key = (rec.values.get('condition_name'), rec.values.get('node_uuid') or '')
                latest.setdefault(key, {})[rec.get_field()] = rec.get_value()
    except Exception as e:  # noqa: BLE001
        return jsonify({'error': f'history unavailable: {e}'}), 503
    finally:
        client.close()
    mutes = _alert_mutes([cid])
    rows = []
    for ep in condition_episodes(set_times, clear_times, latest, since_ms):
        name, node = ep['key']
        m = meta.get(ep['key'], {})
        end = ep['end']
        rows.append({'name': name, 'node_ip': m.get('node_ip', ''),
                     'severity': ALERT_SEVERITY.get(str(m.get('severity', '')).upper(), 'info'),
                     'description': (latest.get(ep['key']) or {}).get('description') or name,
                     'started': ms_iso(ep['start']), 'ended': ms_iso(end), 'active': ep['active'],
                     'duration_s': round(((end or time.time() * 1000) - ep['start']) / 1000) if (end or ep['active']) else None,
                     'muted': (cid, f'condition:{name}:{node}') in mutes})
    rows.sort(key=lambda r: r['started'] or '', reverse=True)
    return jsonify({'episodes': rows, 'days': days})


# =============================================================================
# VM console (backlog #1/#2) — the console service holds the HyperCore session and relays the VNC
# stream; the browser only ever talks to this dashboard with its HCM session. Admin only.
# =============================================================================
CONSOLE_API_URL = os.getenv('CONSOLE_API_URL', 'http://console:8084').rstrip('/')


def _console_token() -> str:
    try:
        return hcm_crypto.derived_token('console')
    except hcm_crypto.SecretKeyMissing:
        return ''


def _console_binding(create: bool) -> str:
    """A hash naming this browser session: a console ticket only works in the session it was issued
    to. The nonce lives in the signed session cookie; a ticket request creates it, the websocket
    upgrade only reads it."""
    nonce = session.get('console_nonce')
    if not nonce:
        if not create:
            return ''
        nonce = session['console_nonce'] = secrets.token_hex(16)
    return hashlib.sha256(f"{session.get('user_id') or ''}:{nonce}".encode()).hexdigest()


def _trusted_proxies() -> int:
    try:
        return max(0, int(os.getenv('HCM_TRUSTED_PROXIES', '0') or 0))
    except ValueError:
        return 0


def _client_ip() -> str:
    """The client's address: the connecting peer, unless HCM_TRUSTED_PROXIES (how many reverse proxies stand
    in front of HCM) says which X-Forwarded-For entry a proxy of ours wrote. The leftmost entry is whatever
    the client sent, so it is never believed (it would let anyone pick the address that sign-in limits count
    and the audit records)."""
    peer = request.remote_addr or ''
    n = _trusted_proxies()
    if n <= 0:
        return peer
    hops = [h.strip() for h in (request.headers.get('X-Forwarded-For') or '').split(',') if h.strip()]
    if not hops:
        return peer
    return hops[-n] if len(hops) >= n else hops[0]


def _console_user() -> str:
    return session.get('username') or ('anonymous' if not _AUTH_ENABLED else '?')


def _console_call(method: str, sub: str, *, query: str = '', payload=None, timeout: float = 60):
    """(status, body bytes, content type) from the console service."""
    headers = {'Authorization': f'Bearer {_console_token()}'}
    body = None
    if payload is not None:
        body = json.dumps(payload).encode()
        headers['Content-Type'] = 'application/json'
    rq = urllib.request.Request(f'{CONSOLE_API_URL}/api/v1/{sub}' + (f'?{query}' if query else ''),
                                data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(rq, timeout=timeout) as resp:
            return resp.status, resp.read(), resp.headers.get('Content-Type', 'application/json')
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers.get('Content-Type', 'application/json')
    except Exception as e:  # noqa: BLE001
        return 503, json.dumps({'error': f'Console service unreachable: {e}'}).encode(), 'application/json'


def _visible_cluster(cid: str):
    return next((c for c in _visible_clusters() if c.cluster_id == cid), None)


@app.route('/console/<cluster_id>/<vm_uuid>')
@requires('vm.console', 'any')
def console_page(cluster_id, vm_uuid):
    """The console window of one VM (opened from the VM's details)."""
    c = _visible_cluster(cluster_id)
    if c is None or not re.fullmatch(r'[0-9A-Za-z-]{1,64}', vm_uuid) or not _can_vm('vm.console', cluster_id, vm_uuid):
        return Response('Unknown cluster or VM', status=404, mimetype='text/plain')
    return render_template('console.html', cluster_id=cluster_id, vm_uuid=vm_uuid, mode='user', title=vm_uuid,
                           subtitle=c.name, ticket_url='/api/console/ticket', ws_path='/console/ws', view_only=False,
                           asset_base='/static')


@app.route('/api/console/ticket', methods=['POST'])
@requires('vm.console', 'any')
def console_ticket():
    """A one-time ticket (30 s, single use, this browser session, this VM) for /console/ws."""
    b = request.get_json(silent=True) or {}
    cid, vm = str(b.get('cluster_id') or ''), str(b.get('vm_uuid') or '')
    if _visible_cluster(cid) is None or not re.fullmatch(r'[0-9A-Za-z-]{1,64}', vm) or not _can_vm('vm.console', cid, vm):
        return jsonify({'error': 'Unknown cluster'}), 404
    status, raw, ctype = _console_call('POST', 'tickets', payload={
        'cluster_id': cid, 'vm_uuid': vm, 'username': _console_user(),
        'binding': _console_binding(create=True), 'client_ip': _client_ip()}, timeout=30)
    if status == 200:
        try:
            ticket = json.loads(raw.decode() or '{}').get('ticket')
        except ValueError:
            ticket = None
        if ticket:
            # which VM a ticket opens, so the websocket can re-check the right to it while the console is open
            seen = dict(session.get('console_vms') or {})
            seen[hashlib.sha256(ticket.encode()).hexdigest()[:16]] = [cid, vm]
            session['console_vms'] = dict(list(seen.items())[-4:])
    return Response(raw, status=status, mimetype=ctype)


_CONSOLE_RECHECK_SECONDS = 120


@sock.route('/console/ws')
@public('checks the session and the one-time ticket itself')
def console_ws(ws):
    """The browser's console websocket: checks the HCM session and the ticket's binding, then pipes
    bytes to and from the console service's relay. The ticket itself is checked (and consumed) there."""
    # the ticket was only issued after the vm.console check for that one VM (console_ticket); here the
    # session must still be a live, enabled login, and the ticket must be bound to it
    if _AUTH_ENABLED and (not session.get('user_id') or _login_problem() is not None):
        ws.close(reason=4401, message='not logged in')
        return
    binding = _console_binding(create=False)
    ticket = request.args.get('ticket', '')
    if not binding or not ticket:
        ws.close(reason=4401, message='console ticket required')
        return
    target = (session.get('console_vms') or {}).get(hashlib.sha256(ticket.encode()).hexdigest()[:16])
    recheck = None
    if _AUTH_ENABLED and target:
        cid, vm = target

        def recheck() -> bool:
            # an open console outlives the checks made when it opened: every few minutes the login must still
            # be live (not disabled, sessions not ended) and the user must still hold vm.console on this VM
            access_db.begin_request()
            g.pop('hcm_access', None)
            try:
                return _login_problem() is None and _can_vm('vm.console', cid, vm)
            except Exception:  # noqa: BLE001 — the database is down: keep the console, check again later
                return True
    _relay_pipe(ws, ticket, binding, recheck=recheck)


def _relay_pipe(ws, ticket: str, binding: str, recheck=None) -> None:
    """Pipe a browser websocket to the console service's relay for a ticket bound to `binding`. The
    ticket is checked (and consumed) there. Shared by the users' console and the guest console.
    recheck(): called every _CONSOLE_RECHECK_SECONDS; False closes the console."""
    url = CONSOLE_API_URL.replace('http://', 'ws://', 1).replace('https://', 'wss://', 1)
    qs = urllib.parse.urlencode({'ticket': ticket, 'binding': binding})
    try:
        up = simple_websocket.Client.connect(f'{url}/api/v1/relay?{qs}', max_message_size=None,
                                             headers={'Authorization': f'Bearer {_console_token()}'})
    except Exception as e:  # noqa: BLE001
        app.logger.warning('console relay unreachable: %s', e)
        ws.close(reason=1011, message='console service unreachable')
        return

    def upstream_to_browser():
        try:
            while True:
                data = up.receive()
                if data is None:
                    continue
                ws.send(data)
        except Exception:  # noqa: BLE001 — either side closed
            pass
        finally:
            reason = getattr(up, 'close_reason', None) or 1000
            message = getattr(up, 'close_message', None) or ''
            try:
                ws.close(reason=reason if reason >= 1000 else 1000, message=message)
            except Exception:  # noqa: BLE001
                pass

    pump = threading.Thread(target=upstream_to_browser, daemon=True, name='console-pump')
    pump.start()
    checked = time.monotonic()
    try:
        while True:
            data = ws.receive(timeout=30 if recheck else None)
            if recheck and time.monotonic() - checked >= _CONSOLE_RECHECK_SECONDS:
                checked = time.monotonic()
                if not recheck():
                    try:
                        ws.close(reason=4403, message='console access ended')
                    except Exception:  # noqa: BLE001
                        pass
                    break
            if data is None:
                continue
            up.send(data if isinstance(data, (bytes, bytearray)) else data.encode())
    except Exception:  # noqa: BLE001 — browser went away or the relay closed
        pass
    finally:
        try:
            up.close()
        except Exception:  # noqa: BLE001
            pass
        pump.join(timeout=5)


@app.route('/api/vm-index')
@requires('vm.view')
def vm_index():
    """Searchable facts of every VM of the selected cluster (live, through the console service)."""
    cid = _cluster_id()
    q = {'cluster': cid}
    if request.args.get('fresh') == '1':
        q['fresh'] = '1'
    status, raw, ctype = _console_call('GET', 'vm-index', query=urllib.parse.urlencode(q), timeout=30)
    scope = _vm_scope()
    if status != 200 or scope == 'all':
        return Response(raw, status=status, mimetype=ctype.split(';')[0])
    try:
        data = json.loads(raw.decode() or '{}')
    except ValueError:
        return Response(raw, status=status, mimetype=ctype.split(';')[0])
    return jsonify(dict(data, vms=_filter_vms(data.get('vms') or [], scope, tags_key='tags')))


@app.route('/api/vm-info')
@requires('vm.view')
def vm_info():
    """Live details of one VM from HyperCore, through the console service's session (read-only, so
    for every user who may see the cluster)."""
    vm = request.args.get('vm') or ''
    if not re.fullmatch(r'[0-9A-Za-z-]{1,64}', vm):
        return jsonify({'error': 'vm required'}), 400
    cid = _cluster_id()
    if not _can_vm('vm.view', cid, vm):
        return jsonify({'error': 'Not found'}), 404
    status, raw, ctype = _console_call('GET', 'vm', query=urllib.parse.urlencode({'cluster': cid, 'vm': vm}),
                                       timeout=30)
    return Response(raw, status=status, mimetype=ctype.split(';')[0])


@app.route('/api/console/<path:sub>', methods=['GET', 'POST', 'PUT', 'DELETE'])
@requires(None)
def console_proxy(sub):
    """The console service's API: the console audit log, screenshots, the open-session check."""
    if not re.fullmatch(r'(sessions|screenshot|check(/(settings|run|learn|exclude|image|refs(/\d+)?|flags))?)', sub):
        return jsonify({'error': 'Not found'}), 404
    perm = 'console.audit' if sub == 'sessions' else ('cluster.view' if sub == 'check/flags' else 'console_check.manage')
    if request.method not in ('GET', 'HEAD') and sub == 'sessions':
        return jsonify({'error': 'Not found'}), 404
    g.hcm_perm = perm
    acc = _access()
    if not acc.held_anywhere(perm):
        return jsonify({'error': 'Access denied'}), 403
    if sub == 'sessions' and not request.args.get('cluster'):
        cid = None                                    # the audit log across every cluster the user may audit
    else:
        problem = _write_cluster_problem(perm, need=True)
        if problem is not None:
            return problem
        cid = _cluster_id()
        if perm != 'console.audit' and not acc.can(perm, cid):
            return jsonify({'error': 'Access denied'}), 403
    args = [(k, v) for k, v in request.args.items(multi=True) if k != 'cluster']
    if cid:
        args.append(('cluster', cid))
    payload = None
    if request.method in ('POST', 'PUT'):
        payload = request.get_json(silent=True) or {}
        body_cid, err = body_cluster(payload, cid, {c.cluster_id for c in _visible_clusters()}, fill=True)
        if err:
            return jsonify({'error': err}), 403
        payload['cluster_id'] = body_cid
        payload['username'] = _console_user()
    status, raw, ctype = _console_call(request.method, sub, query=urllib.parse.urlencode(args), payload=payload,
                                       timeout=90)
    if sub == 'sessions' and status == 200:
        # the audit log, narrowed to the clusters and VMs this user may audit
        try:
            data = json.loads(raw.decode() or '{}')
        except ValueError:
            return Response(raw, status=status, mimetype=ctype.split(';')[0])
        ids = [c.cluster_id for c in _enabled_clusters()]
        scopes = acc.scope_for('console.audit', ids)
        rows = data.get('sessions') if isinstance(data, dict) else data
        keep = []
        for r in rows or []:
            sc = scopes.get(str(r.get('cluster_id')))
            if sc and (sc == 'all' or rbac.vm_in_scope(sc, r.get('vm_uuid'), _vm_tags(str(r.get('cluster_id')), r.get('vm_uuid'))
                                                         if sc.get('tags') else [])):
                keep.append(r)
        # sessions of accounts above the reader: no name, and no address (an office IP names the MSP too)
        access_db.hide_names(keep, ('username',), access_db.nameable_usernames(acc, session.get('username')))
        for r in keep:
            if 'username' in r.get('hidden', ()):
                r['client_ip'] = None
        return jsonify(dict(data, sessions=keep) if isinstance(data, dict) else keep)
    return Response(raw, status=status, mimetype=ctype.split(';')[0])


# =============================================================================
# Foresight (sentinel) API — E1-E6
# all routes query the recommendations schema in PostgreSQL directly
# =============================================================================

def _sentinel_query(sql: str, params: list) -> list[dict]:
    """Run a SELECT against the recommendations schema and return rows as dicts."""
    with _pg() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    result = []
    for row in rows:
        d = {}
        for k, v in row.items():
            from datetime import date
            if hasattr(v, 'isoformat'):
                d[k] = v.isoformat()
            else:
                d[k] = v
        result.append(d)
    return result


@app.route('/api/sentinel/summary')
@requires('cluster.view')
def sentinel_summary():
    """E3/E6 — Health score + counts for the overview dashboard."""
    try:
        cid = _cluster_id()
        with _pg() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    '''SELECT health_score, bottleneck_resource, efficiency_index,
                              severity, calculated_at, component_scores, growth_rates,
                              component_reasons
                       FROM recommendations.cluster_health_scores
                       WHERE cluster_id = %s AND expires_at > NOW()
                       ORDER BY calculated_at DESC LIMIT 1''',
                    [cid],
                )
                health_row = cur.fetchone()

                cur.execute(
                    '''SELECT sizing_direction, COUNT(*) AS cnt
                       FROM recommendations.right_sizing
                       WHERE cluster_id = %s AND expires_at > NOW()
                       GROUP BY sizing_direction''',
                    [cid],
                )
                sizing = {r['sizing_direction']: r['cnt'] for r in cur.fetchall()}

                cur.execute(
                    '''SELECT resource_dimension, days_until_exhaustion, severity
                       FROM recommendations.resource_exhaustion
                       WHERE cluster_id = %s AND expires_at > NOW()
                       ORDER BY CASE severity
                           WHEN 'EMERGENCY' THEN 1 WHEN 'CRITICAL' THEN 2
                           WHEN 'WARNING' THEN 3 ELSE 4 END,
                           days_until_exhaustion ASC NULLS LAST
                       LIMIT 1''',
                    [cid],
                )
                ex = cur.fetchone()

                cur.execute(
                    'SELECT COUNT(*) AS cnt FROM recommendations.forecast_snapshots WHERE cluster_id = %s AND expires_at > NOW()',
                    [cid],
                )
                fc = cur.fetchone()

                # latest cluster-wide forecast row per dimension — gives the
                # current_capacity used for N+1 calculations so the dashboard
                # can express growth rates as "% of capacity per year".  The
                # model_metadata also carries the daily_aggregate that was
                # active when this analysis ran, so the breakdown badge
                # reflects what the bars are *actually* based on rather than
                # the current pending setting (which may have changed since)
                cur.execute(
                    '''SELECT DISTINCT ON (resource_dimension)
                              resource_dimension, current_usage, current_capacity,
                              model_metadata
                       FROM recommendations.forecast_snapshots
                       WHERE cluster_id = %s
                         AND entity_label = 'Cluster'
                         AND expires_at > NOW()
                       ORDER BY resource_dimension, snapshot_at DESC''',
                    [cid],
                )
                cluster_metrics = {}
                daily_aggregate = None
                for r in cur.fetchall():
                    cluster_metrics[r['resource_dimension']] = {
                        'usage':    float(r['current_usage'])    if r['current_usage']    is not None else None,
                        'capacity': float(r['current_capacity']) if r['current_capacity'] is not None else None,
                    }
                    if daily_aggregate is None and r['model_metadata']:
                        daily_aggregate = r['model_metadata'].get('daily_aggregate')

        return jsonify({
            'cluster_id': cid,
            'health': {
                'score':             float(health_row['health_score']) if health_row else None,
                'bottleneck':        health_row['bottleneck_resource'] if health_row else None,
                'efficiency':        float(health_row['efficiency_index']) if health_row and health_row['efficiency_index'] is not None else None,
                'severity':          health_row['severity'] if health_row else None,
                'as_of':             health_row['calculated_at'].isoformat() if health_row and health_row['calculated_at'] else None,
                'component_scores':  health_row['component_scores'] if health_row else None,
                'component_reasons': health_row['component_reasons'] if health_row else None,
                'growth_rates':      health_row['growth_rates']     if health_row else None,
                'cluster_metrics':   cluster_metrics,
                'daily_aggregate':   daily_aggregate,
            },
            'right_sizing': {
                'over_provisioned':  sizing.get('over_provisioned',  0),
                'under_provisioned': sizing.get('under_provisioned', 0),
            },
            'worst_exhaustion': {
                'dimension': ex['resource_dimension'],
                'days':      ex['days_until_exhaustion'],
                'severity':  ex['severity'],
            } if ex else None,
            'active_forecasts': fc['cnt'] if fc else 0,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/sentinel/forecasts')
@requires('cluster.view')
def sentinel_forecasts():
    """E1 — Latest forecast snapshots, optionally filtered by ?dimension= or ?horizon="""
    try:
        cid       = _cluster_id()
        dimension = request.args.get('dimension')
        horizon   = request.args.get('horizon')
        limit     = max(1, min(int(request.args.get('limit', 200)), 1000))

        where  = ['cluster_id = %s', 'expires_at > NOW()']
        params: list = [cid]
        if dimension:
            where.append('resource_dimension = %s'); params.append(dimension)
        if horizon:
            where.append('forecast_horizon_days = %s'); params.append(int(horizon))
        params.append(limit)

        rows = _sentinel_query(
            f'''SELECT id, node_uuid, entity_label, resource_dimension, forecast_horizon_days,
                       current_usage, current_capacity, forecast_value,
                       confidence_score, driving_metric, exhaustion_date,
                       severity, model_metadata, snapshot_at
                FROM recommendations.forecast_snapshots
                WHERE {" AND ".join(where)}
                ORDER BY snapshot_at DESC LIMIT %s''',
            params,
        )
        return jsonify(rows)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/sentinel/rightsizing')
@requires('cluster.view')
def sentinel_rightsizing():
    """E2 — Right-sizing results (C1, C2)."""
    try:
        cid       = _cluster_id()
        direction = request.args.get('direction')
        limit     = max(1, min(int(request.args.get('limit', 200)), 1000))

        where  = ['cluster_id = %s', 'expires_at > NOW()']
        params: list = [cid]
        if direction:
            where.append('sizing_direction = %s'); params.append(direction)
        params.append(limit)

        rows = _sentinel_query(
            f'''SELECT id, vm_uuid, vm_name, node_uuid, sizing_direction, severity,
                       analysis_window_days, cpu_signal_score, cpu_avg_usage_pct,
                       cpu_p95_usage_pct, latency_signal_score, spike_signal_score,
                       composite_score, signal_detail, detected_at
                FROM recommendations.right_sizing
                WHERE {" AND ".join(where)}
                ORDER BY composite_score DESC, detected_at DESC LIMIT %s''',
            params,
        )
        return jsonify(rows)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/sentinel/resize')
@requires('cluster.view')
def sentinel_resize():
    """E2 — Concrete resize advice (C3-C6)."""
    try:
        cid   = _cluster_id()
        limit = max(1, min(int(request.args.get('limit', 200)), 1000))
        rows  = _sentinel_query(
            '''SELECT id, vm_uuid, vm_name, node_uuid, sizing_direction,
                      current_vcpu, recommended_vcpu,
                      suggested_cpu_change_pct, target_cpu_avg_pct,
                      confidence_score, capacity_reclaim_cpu_pct,
                      risk_level, risk_detail, severity, detected_at
               FROM recommendations.resize_recommendations
               WHERE cluster_id = %s AND expires_at > NOW()
               ORDER BY confidence_score DESC, detected_at DESC LIMIT %s''',
            [cid, limit],
        )
        return jsonify(rows)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/sentinel/exhaustion')
@requires('cluster.view')
def sentinel_exhaustion():
    """E4 — Resource exhaustion alerts, most urgent first."""
    try:
        cid   = _cluster_id()
        limit = max(1, min(int(request.args.get('limit', 100)), 500))
        rows  = _sentinel_query(
            '''SELECT id, node_uuid, resource_dimension, days_until_exhaustion,
                      exhaustion_date, current_usage_pct, growth_rate_per_day,
                      severity, calculated_at
               FROM recommendations.resource_exhaustion
               WHERE cluster_id = %s AND expires_at > NOW()
               ORDER BY CASE severity
                   WHEN 'EMERGENCY' THEN 1 WHEN 'CRITICAL' THEN 2
                   WHEN 'WARNING' THEN 3 ELSE 4 END,
                   days_until_exhaustion ASC NULLS LAST
               LIMIT %s''',
            [cid, limit],
        )
        return jsonify(rows)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/sentinel/cluster-health')
@requires('cluster.view')
def sentinel_cluster_health():
    """E3 — Cluster health score history."""
    try:
        cid   = _cluster_id()
        limit = max(1, min(int(request.args.get('limit', 50)), 500))
        rows  = _sentinel_query(
            '''SELECT id, health_score, bottleneck_resource, growth_rates,
                      efficiency_index, component_scores, cluster_count,
                      severity, calculated_at
               FROM recommendations.cluster_health_scores
               WHERE cluster_id = %s AND expires_at > NOW()
               ORDER BY calculated_at DESC LIMIT %s''',
            [cid, limit],
        )
        return jsonify(rows)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/sentinel/status')
@requires('cluster.view', 'any')
def sentinel_status():
    """Proxy the sentinel health/detail endpoint so the dashboard knows when the next analysis run is."""
    sentinel_url = os.getenv('SENTINEL_HEALTH_URL', 'http://sentinel:8080')
    try:
        with urllib.request.urlopen(f'{sentinel_url}/health/detail', timeout=3) as resp:
            data = json.loads(resp.read().decode())
        return jsonify(data)
    except Exception:
        # sentinel not reachable — return minimal stub so UI can show "not running" state
        return jsonify({'status': 'unavailable'}), 200


@app.route('/api/sentinel/report.pdf')
@requires('cluster.view')
def sentinel_report_pdf():
    """E5 — Generate and download a PDF capacity intelligence report."""
    try:
        from reports.pdf_report import generate as gen_pdf
    except ImportError:
        return jsonify({'error': 'reportlab not installed — PDF generation unavailable'}), 503

    try:
        cid = _cluster_id()
        now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')

        with _pg() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    '''SELECT health_score, bottleneck_resource, efficiency_index,
                              severity, calculated_at
                       FROM recommendations.cluster_health_scores
                       WHERE cluster_id = %s AND expires_at > NOW()
                       ORDER BY calculated_at DESC LIMIT 1''',
                    [cid],
                )
                health_row = cur.fetchone()

                def _q(sql, params):
                    cur.execute(sql, params)
                    rows = []
                    for r in cur.fetchall():
                        d = {}
                        for k, v in r.items():
                            if hasattr(v, 'isoformat'):
                                d[k] = v.isoformat()
                            elif v.__class__.__name__ == 'UUID':
                                d[k] = str(v)
                            else:
                                d[k] = v
                        rows.append(d)
                    return rows

                forecasts   = _q('SELECT * FROM recommendations.forecast_snapshots WHERE cluster_id = %s AND expires_at > NOW() ORDER BY snapshot_at DESC LIMIT 100', [cid])
                rightsizing = _q('SELECT * FROM recommendations.right_sizing WHERE cluster_id = %s AND expires_at > NOW() ORDER BY composite_score DESC LIMIT 50', [cid])
                resize      = _q('SELECT * FROM recommendations.resize_recommendations WHERE cluster_id = %s AND expires_at > NOW() ORDER BY confidence_score DESC LIMIT 50', [cid])
                exhaustion  = _q('''SELECT * FROM recommendations.resource_exhaustion WHERE cluster_id = %s AND expires_at > NOW()
                                   ORDER BY CASE severity WHEN 'EMERGENCY' THEN 1 WHEN 'CRITICAL' THEN 2 WHEN 'WARNING' THEN 3 ELSE 4 END,
                                   days_until_exhaustion ASC NULLS LAST LIMIT 50''', [cid])

        health_dict = None
        if health_row:
            health_dict = {k: (v.isoformat() if hasattr(v, 'isoformat') else v) for k, v in health_row.items()}

        # E7: fetch branding (best-effort — PDF still generates without it)
        try:
            branding = _get_branding()
        except Exception:
            branding = None

        pdf_bytes = gen_pdf({
            'cluster_id':        cid,
            'generated_at':      now,
            'health':            health_dict,
            'forecasts':         forecasts,
            'rightsizing':       rightsizing,
            'resize':            resize,
            'exhaustion':        exhaustion,
            'forecast_count':    len(forecasts),
            'rightsizing_count': len(rightsizing),
            'exhaustion_count':  len(exhaustion),
            'branding':          branding,
        })

        filename = f'scforesight-report-{datetime.now(timezone.utc).strftime("%Y%m%d")}.pdf'
        return Response(
            pdf_bytes,
            mimetype='application/pdf',
            headers={'Content-Disposition': f'attachment; filename="{filename}"'},
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# E7 — report branding configuration
# ---------------------------------------------------------------------------

def _get_branding() -> dict:
    """Fetch branding config for the current cluster from PostgreSQL."""
    cid = _cluster_id()
    with _pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                '''
                SELECT company_name, accent_color, logo_b64, logo_mime_type, updated_at
                FROM config.branding
                WHERE cluster_id = %s
                ''',
                [cid],
            )
            row = cur.fetchone()
    if row is None:
        return {'company_name': '', 'accent_color': '#3b82f6',
                'logo_b64': '', 'logo_mime_type': 'image/png', 'updated_at': None}
    return {
        'company_name':  row['company_name'] or '',
        'accent_color':  row['accent_color']  or '#3b82f6',
        'logo_b64':      row['logo_b64']      or '',
        'logo_mime_type': row['logo_mime_type'] or 'image/png',
        'updated_at':    row['updated_at'].isoformat() if row['updated_at'] else None,
    }


@app.route('/api/sentinel/branding', methods=['GET'])
@requires('cluster.view')
def sentinel_get_branding():
    """E7: Return current report branding configuration."""
    try:
        return jsonify(_get_branding())
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/sentinel/branding', methods=['PUT'])
@requires('cluster.settings')
def sentinel_put_branding():
    """E7: Save report branding configuration (UPSERT)."""
    body = request.get_json(silent=True) or {}
    cid  = _cluster_id()

    company_name   = str(body.get('company_name', ''))[:200]
    accent_color   = str(body.get('accent_color', '#3b82f6'))[:20]
    logo_b64       = body.get('logo_b64') or ''
    logo_mime_type = str(body.get('logo_mime_type', 'image/png'))[:40]

    # validate accent_color is a 6- or 3-digit hex
    import re as _re
    if not _re.match(r'^#[0-9a-fA-F]{3,6}$', accent_color):
        accent_color = '#3b82f6'

    try:
        with _pg() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    '''
                    INSERT INTO config.branding
                        (cluster_id, company_name, accent_color, logo_b64, logo_mime_type, updated_at)
                    VALUES (%s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (cluster_id) DO UPDATE
                        SET company_name   = EXCLUDED.company_name,
                            accent_color   = EXCLUDED.accent_color,
                            logo_b64       = EXCLUDED.logo_b64,
                            logo_mime_type = EXCLUDED.logo_mime_type,
                            updated_at     = NOW()
                    ''',
                    [cid, company_name or None, accent_color, logo_b64 or None, logo_mime_type],
                )
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# D5 — VM list + what-if scenario modelling (proxy to sentinel REST API)
# ---------------------------------------------------------------------------

@app.route('/api/sentinel/vms')
@requires('cluster.view')
def sentinel_vms():
    """Return VM list with profile data available for add_vms_like simulation."""
    import urllib.request as _req
    import urllib.error

    sentinel_api_url = os.getenv('SENTINEL_API_URL', 'http://sentinel:8081')
    sentinel_token   = os.getenv('SENTINEL_API_TOKEN', '')

    try:
        headers = {}
        if sentinel_token:
            headers['Authorization'] = f'Bearer {sentinel_token}'
        rq = _req.Request(f'{sentinel_api_url}/api/v1/vms?cluster={_cluster_id()}', headers=headers)
        with _req.urlopen(rq, timeout=10) as resp:
            return jsonify(json.loads(resp.read().decode()))
    except urllib.error.HTTPError as e:
        return jsonify({'error': e.read().decode(errors='replace')}), e.code
    except Exception as e:
        return jsonify({'error': f'Sentinel unreachable: {e}'}), 503


@app.route('/api/sentinel/whatif', methods=['POST'])
@requires('cluster.view')
def sentinel_whatif():
    """
    D5: Forward a what-if scenario simulation request to the Sentinel REST API
    and return the result.  The computation lives in sentinel/analysis/whatif.py;
    the dashboard proxies rather than duplicates it.
    """
    import urllib.request as _req
    import urllib.error

    sentinel_api_url = os.getenv('SENTINEL_API_URL', 'http://sentinel:8081')
    sentinel_token   = os.getenv('SENTINEL_API_TOKEN', '')

    try:
        body = request.get_json(silent=True) or {}
        payload = json.dumps(body).encode()
        headers = {'Content-Type': 'application/json'}
        if sentinel_token:
            headers['Authorization'] = f'Bearer {sentinel_token}'

        rq = _req.Request(
            f'{sentinel_api_url}/api/v1/whatif?cluster={_cluster_id()}',
            data=payload,
            headers=headers,
            method='POST',
        )
        with _req.urlopen(rq, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        return jsonify(data)

    except urllib.error.HTTPError as e:
        body_err = e.read().decode(errors='replace')
        return jsonify({'error': body_err}), e.code
    except Exception as e:
        return jsonify({'error': f'Sentinel unreachable: {e}'}), 503


# T3 — sentinel live tunables proxy (Phase 3.5)

@app.route('/api/sentinel/tunables', methods=['GET'])
@requires('cluster.view')
def sentinel_get_tunables():
    """T3: Forward GET /api/sentinel/tunables → sentinel /api/v1/tunables."""
    import urllib.request as _req
    import urllib.error

    sentinel_api_url = os.getenv('SENTINEL_API_URL', 'http://sentinel:8081')
    sentinel_token   = os.getenv('SENTINEL_API_TOKEN', '')

    try:
        headers = {}
        if sentinel_token:
            headers['Authorization'] = f'Bearer {sentinel_token}'
        rq = _req.Request(f'{sentinel_api_url}/api/v1/tunables?cluster={_cluster_id()}', headers=headers)
        with _req.urlopen(rq, timeout=10) as resp:
            return jsonify(json.loads(resp.read().decode()))
    except urllib.error.HTTPError as e:
        return jsonify({'error': e.read().decode(errors='replace')}), e.code
    except Exception as e:
        return jsonify({'error': f'Sentinel unreachable: {e}'}), 503


@app.route('/api/sentinel/tunables', methods=['PUT'])
@requires('cluster.settings')
def sentinel_put_tunables():
    """T3: Forward PUT /api/sentinel/tunables → sentinel /api/v1/tunables."""
    import urllib.request as _req
    import urllib.error

    sentinel_api_url = os.getenv('SENTINEL_API_URL', 'http://sentinel:8081')
    sentinel_token   = os.getenv('SENTINEL_API_TOKEN', '')

    try:
        body    = request.get_json(silent=True) or {}
        payload = json.dumps(body).encode()
        headers = {'Content-Type': 'application/json'}
        if sentinel_token:
            headers['Authorization'] = f'Bearer {sentinel_token}'
        rq = _req.Request(
            f'{sentinel_api_url}/api/v1/tunables?cluster={_cluster_id()}',
            data=payload, headers=headers, method='PUT',
        )
        cid = _cluster_id()
        with _req.urlopen(rq, timeout=10) as resp:
            out = json.loads(resp.read().decode())
        _audit('tunables.foresight', cid, node_id=_access().tree.cluster_folder.get(cid),
               detail={'cluster_id': cid, 'changes': {str(k)[:80]: v for k, v in list(body.items())[:50]}
                       if isinstance(body, dict) else None})
        return jsonify(out)
    except urllib.error.HTTPError as e:
        body_err = e.read().decode(errors='replace')
        return jsonify({'error': body_err}), e.code
    except Exception as e:
        return jsonify({'error': f'Sentinel unreachable: {e}'}), 503


@app.route('/api/sentinel/trend/run', methods=['POST'])
@requires('cluster.settings')
def sentinel_trigger_trend_run():
    """Forward POST /api/sentinel/trend/run → sentinel /api/v1/trend/run."""
    import urllib.request as _req
    import urllib.error

    sentinel_api_url = os.getenv('SENTINEL_API_URL', 'http://sentinel:8081')
    sentinel_token   = os.getenv('SENTINEL_API_TOKEN', '')

    try:
        headers = {'Content-Type': 'application/json'}
        if sentinel_token:
            headers['Authorization'] = f'Bearer {sentinel_token}'
        rq = _req.Request(
            f'{sentinel_api_url}/api/v1/trend/run?cluster={_cluster_id()}',
            data=b'', headers=headers, method='POST',
        )
        with _req.urlopen(rq, timeout=10) as resp:
            return jsonify(json.loads(resp.read().decode()))
    except urllib.error.HTTPError as e:
        body_err = e.read().decode(errors='replace')
        return jsonify({'error': body_err}), e.code
    except Exception as e:
        return jsonify({'error': f'Sentinel unreachable: {e}'}), 503


# =============================================================================
# auth routes
# =============================================================================

_OTP_PENDING_SECONDS = 300          # the code must follow the password within five minutes


@app.route('/login', methods=['GET', 'POST'])
@public('the login page')
def login_page():
    if not _AUTH_ENABLED:
        return redirect(url_for('index'))
    if session.get('user_id'):
        return redirect(url_for('index'))

    step = request.args.get('step', 'password')
    error = None

    if request.method == 'POST':
        client = _client_ip()
        if step == 'otp':
            # guard: must have completed the password step first, a few minutes ago at most
            pending_username = session.get('otp_pending')
            pending_at = float(session.get('otp_pending_at') or 0)
            if not pending_username or time.time() - pending_at > _OTP_PENDING_SECONDS:
                session.pop('otp_pending', None)
                session.pop('otp_pending_at', None)
                return redirect(url_for('login_page'))
            # wrong codes are counted in the database since this password step: replaying an older cookie
            # (the session lives in the client) does not give more guesses
            if user_db.otp_failures_since(pending_username, pending_at) >= user_db.MAX_OTP_FAILURES:
                session.pop('otp_pending', None)
                session.pop('otp_pending_at', None)
                return _login_render('password', 'err_otp_restart', False)
            code = request.form.get('otp', '').strip()
            if user_db.verify_otp_code(pending_username, code):
                u = user_db.get_by_username(pending_username)
                session.pop('otp_pending', None)
                session.pop('otp_pending_at', None)
                user_db.clear_login_failures(pending_username)
                dest = _after_login(session.pop('next', None))
                _start_session(u)
                _audit('login', u['username'], detail={'ip': client, 'otp': True}, actor=u['username'])
                return redirect(dest)
            user_db.record_login_failure(pending_username, client, 'otp')
            _audit('login.otp_failed', pending_username, detail={'ip': client}, actor=pending_username)
            if user_db.otp_failures_since(pending_username, pending_at) >= user_db.MAX_OTP_FAILURES:
                session.pop('otp_pending', None)
                session.pop('otp_pending_at', None)
                return _login_render('password', 'err_otp_restart', False)
            error = 'err_otp'
        else:
            typed = request.form.get('username', '')
            if user_db.login_locked(typed, client):
                # counted by what was typed and by address, so the answer is the same for an unknown account
                _audit('login.locked', typed.strip()[:254], detail={'ip': client}, actor='anonymous')
                return _login_render('password', 'err_locked', False)
            found = user_db.get_by_login(typed)
            username = found['username'] if found else typed.strip()
            password = request.form.get('password', '')
            # always one password check, also for an unknown address (against a dummy hash): same time either way
            if user_db.verify_password(username, password) and found:
                u = found
                if u.get('enabled') is False:
                    # only after a correct password: say why, so they do not retry into a lockout
                    error = ('err_invited' if u.get('awaiting_email') and '@' in u['username'] else
                             'err_needs_email' if u.get('awaiting_email') else
                             'err_awaiting' if u.get('awaiting_access') else 'err_disabled')
                    return _login_render('password', error, False)
                if u['otp_enabled']:
                    session['otp_pending'] = username
                    session['otp_pending_at'] = time.time()
                    return redirect(url_for('login_page', step='otp'))
                user_db.clear_login_failures(typed)
                dest = _after_login(session.pop('next', None))
                _start_session(u)
                _audit('login', u['username'], detail={'ip': client}, actor=u['username'])
                return redirect(dest)
            user_db.record_login_failure(typed, client, 'password')
            # what was typed, never the password; an unknown address is recorded as typed (that is the attempt)
            _audit('login.failed', typed.strip()[:254], detail={'ip': client}, actor='anonymous')
            error = 'err_login'

    # pass otp_enabled=True only when on the OTP step (drives login.html hint text)
    pending = session.get('otp_pending')
    otp_hint = bool(pending and user_db.get_by_username(pending or ''))
    return _login_render(step, error, otp_hint)


def _login_render(step: str, error_key: str | None, otp_enabled: bool):
    import login_texts
    lang = login_texts.lang(request)
    t = login_texts.TEXTS[lang]
    return render_template('login.html', step=step, error=t.get(error_key) if error_key else None,
                           otp_enabled=otp_enabled, t=t, lang=lang)


def _after_login(dest):
    """Where a login continues: back to an invitation or guest-request link that sent the person here, else
    the dashboard. Only these exact shapes: never an open redirect."""
    ok = isinstance(dest, str) and (re.fullmatch(r'/invite/[A-Za-z0-9_-]{20,200}', dest) or re.fullmatch(r'/guest-requests/\d{1,12}', dest))
    return dest if ok else url_for('index')


@app.route('/logout', methods=['GET', 'POST'])        # POST: a form button (cross-site safe); GET: the old link
@public('logging out')
def logout():
    nxt = request.values.get('next')
    session.clear()
    if _after_login(nxt) != url_for('index'):
        session['next'] = nxt                 # 'sign out and switch' on an invitation for another address
    return redirect(url_for('login_page'))


# =============================================================================
# user management API
# =============================================================================

@app.route('/api/users/me')
@requires(None)
def api_me():
    if not _AUTH_ENABLED:
        return jsonify({'user_id': None, 'username': 'dev', 'role': 'admin',
                        'otp_enabled': False, 'dev_mode': True, 'community_ack': None,
                        'access': _access_summary(_access())})
    u = user_db.get_by_username(session['username'])
    if not u:
        session.clear()                     # see require_auth: the session outlived its user
        return jsonify({'error': 'Authentication required'}), 401
    acc = _access()
    return jsonify({
        'user_id':     str(u['user_id']),
        'username':    u['username'],
        'display_name': u.get('display_name'),
        'role':        'admin' if acc.is_root_admin() else 'user',
        'otp_enabled': u['otp_enabled'],
        'dev_mode':    False,
        'community_ack': bool(u.get('community_ack', False)),
        'access':      _access_summary(acc),
    })


@app.route('/api/users/me/community-ack', methods=['POST'])
@requires(None)
def api_me_community_ack():
    """Record that the current user has seen the community-support notice."""
    if not _AUTH_ENABLED:
        # no user row to persist on; the frontend falls back to localStorage
        return jsonify({'ok': True, 'dev_mode': True})
    user_db.set_community_ack(session['username'])
    return jsonify({'ok': True, 'dev_mode': False})


@app.route('/api/users/me/otp/setup', methods=['GET', 'POST'])
@requires(None)
def api_otp_setup():
    """The secret to enrol an authenticator: POST {"password": <current password>}. Never once two-factor is
    on (the secret would let whoever holds the session make codes), and never without the password (a stolen
    session could otherwise enrol its own authenticator and lock the person out)."""
    username = session.get('username', 'dev')
    if _AUTH_ENABLED:
        u = user_db.get_by_username(username)
        if u and u.get('otp_enabled'):
            return jsonify({'error': 'two-factor authentication is already on; turn it off first to set it up again'}), 409
        data = (request.get_json(silent=True) or {}) if request.method == 'POST' else {}
        if not user_db.verify_password(username, str(data.get('password') or '')):
            return jsonify({'error': 'enter your current password to set up two-factor authentication',
                            'password_required': True}), 403
    try:
        return jsonify(user_db.build_otp_setup(username))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/users/me/otp/enable', methods=['POST'])
@requires(None)
def api_otp_enable():
    username = session.get('username', 'dev')
    data = request.get_json(silent=True) or {}
    code = (data.get('code') or '').strip()
    if not code:
        return jsonify({'error': 'code is required'}), 400
    if user_db.enable_otp(username, code):
        _audit('user.otp_enabled', username, actor=username)
        if _AUTH_ENABLED:
            gen = user_db.bump_session_gen(username)     # other sessions signed in without the code end
            if gen is not None:
                session['gen'] = gen
        return jsonify({'ok': True})
    return jsonify({'error': 'Invalid code — check your authenticator app and try again'}), 400


@app.route('/api/users/me/otp/disable', methods=['POST'])
@requires(None)
def api_otp_disable():
    username = session.get('username', 'dev')
    data = request.get_json(silent=True) or {}
    if _AUTH_ENABLED and not user_db.verify_password(username, data.get('password', '')):
        return jsonify({'error': 'Incorrect password'}), 403
    try:
        user_db.disable_otp(username)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    _audit('user.otp_disabled', username, actor=username)
    if _AUTH_ENABLED:
        gen = user_db.bump_session_gen(username)         # every other session of the account ends
        if gen is not None:
            session['gen'] = gen
    return jsonify({'ok': True})


# =============================================================================
# auto-update
# =============================================================================

_REPO_PATH       = os.getenv('SC_REPO_PATH', '/repo')
_UPDATE_FILE     = '/tmp/hcm_update.json'
_UPDATE_FILE_LOCK = threading.Lock()
_APPLY_MAX_SECONDS = 2 * 3600            # an apply still "in progress" after this was interrupted


def _read_update_file():
    try:
        with open(_UPDATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


@contextmanager
def _update_lock():
    """The updater state is one file shared by four gunicorn workers: a thread lock within the worker and an
    flock across them, so a read-modify-write (and Apply's check-and-set) is never interleaved."""
    import fcntl
    with _UPDATE_FILE_LOCK:
        with open(_UPDATE_FILE + '.lock', 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


def _store_update_file(state: dict) -> None:
    """Write through a temporary file and os.replace: a reader never sees a half-written file."""
    tmp = f'{_UPDATE_FILE}.{os.getpid()}.{threading.get_ident()}.tmp'
    with open(tmp, 'w') as f:
        json.dump(state, f)
    os.replace(tmp, _UPDATE_FILE)


def _write_update_patch(patch):
    with _update_lock():
        state = _read_update_file()
        state.update(patch)
        _store_update_file(state)


def _container_marker() -> str:
    """Something that changes when this container starts again: PID 1's start time (field 22 of /proc/1/stat)."""
    try:
        with open('/proc/1/stat') as f:
            return f.read().rsplit(')', 1)[1].split()[19]
    except (OSError, IndexError):
        return ''


def _apply_is_stale(state: dict) -> bool:
    """An apply marked in progress whose thread cannot be running any more: started in an earlier life of this
    container (a restart during the build), by a worker that is gone, or longer ago than any apply takes."""
    if not state.get('apply_in_progress'):
        return False
    if state.get('apply_marker') != _container_marker():
        return True
    pid = state.get('apply_pid')
    if pid:
        try:
            os.kill(int(pid), 0)
        except (OSError, ValueError):
            return True
    return time.time() - float(state.get('apply_started') or 0) > _APPLY_MAX_SECONDS


def _clear_stale_apply() -> None:
    try:
        with _update_lock():
            state = _read_update_file()
            if _apply_is_stale(state):
                state['apply_in_progress'] = False
                state.setdefault('apply_log', []).append('The previous apply was interrupted (the dashboard restarted); '
                                                         'it can be applied again.')
                _store_update_file(state)
    except OSError as exc:
        app.logger.warning('updater state not checked: %s', exc)


_clear_stale_apply()


def _git(*args, timeout=10):
    """Run a git command in _REPO_PATH and return stdout, or '' on failure."""
    r = subprocess.run(
        ['git', '-C', _REPO_PATH] + list(args),
        capture_output=True, text=True, timeout=timeout,
    )
    return r.stdout.strip() if r.returncode == 0 else ''


# the services an update recreates: everything but the two databases, whose volumes and
# config do not change on a release. Recreating them too is what the restart container did
# when its own compose version hashed the config differently from the host's.
_APP_SERVICES = ('collector', 'balancer', 'dashboard', 'sentinel', 'backup', 'console', 'proxy')
_DB_SERVICES = ('influxdb', 'postgres')


def restart_command(fallback=_APP_SERVICES) -> str:
    """Shell command the restart container runs: recreate the services of the compose file that was just
    pulled, except the databases. The list comes from that new file, not from this (old) code: an updater
    from 1.1.x had a fixed list without `console` and never started 1.2.0's new service. The fixed list is
    the fallback when the file cannot be read.

    Of those, only the ones running now and the ones that have no container yet (new in this release): a
    service the administrator stopped stays stopped (the manual says `docker compose stop balancer`; starting
    it again with DRY_RUN false would resume migrating VMs)."""
    skip = '|'.join(_DB_SERVICES)
    return ("S=$(docker-compose config --services 2>/dev/null | grep -vxE '" + skip + "' | tr '\\n' ' '); "
            "[ -n \"$S\" ] || S='" + ' '.join(fallback) + "'; "
            "R=\" $(docker-compose ps --services --filter status=running 2>/dev/null | tr '\\n' ' ') \"; "
            "A=\" $(docker-compose ps -a --services 2>/dev/null | tr '\\n' ' ') \"; "
            "U=''; for s in $S; do case \"$R\" in *\" $s \"*) U=\"$U $s\";; "
            "*) case \"$A\" in *\" $s \"*) ;; *) U=\"$U $s\";; esac;; esac; done; "
            "[ -n \"$U\" ] || exit 0; "
            "exec docker-compose up -d --no-deps $U")


# Services added after 1.1.x. A stack updated by a 1.1.x dashboard has their images (the update builds
# everything) but no containers: that updater only started the services on its own fixed list. The
# current dashboard starts them once, shortly after it comes up. Deliberately not "every service of the
# compose file": an admin may run without one (Sentinel stopped, no backup proxy), and those stay as they are.
SELF_HEAL_SERVICES = ('console',)


def _self_heal_services(delay: float = 30) -> None:
    import fcntl
    time.sleep(delay)                                   # let an update that is still recreating services finish
    try:
        lock = open('/tmp/hcm-self-heal.lock', 'w')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)   # one of the gunicorn workers does it
    except OSError:
        return
    _self_heal_services.lock = lock                     # keep the lock for the life of the worker
    compose = os.path.join(_REPO_PATH, 'docker-compose.yaml')
    if not (os.path.exists('/var/run/docker.sock') and os.path.exists(compose)):
        return
    project = os.getenv('COMPOSE_PROJECT_NAME', 'drsproject')
    try:
        r = subprocess.run(['docker', 'ps', '-a', '--filter', f'label=com.docker.compose.project={project}',
                            '--format', '{{.Label "com.docker.compose.service"}}'], capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return
        present = set(r.stdout.split())
        with open(compose) as f:
            text = f.read()
        missing = [s for s in SELF_HEAL_SERVICES if s not in present and re.search(rf'(?m)^  {s}:\s*$', text)]
        if not missing:
            return
        app.logger.warning('starting service(s) missing after an update from an older version: %s', ', '.join(missing))
        host_repo = os.getenv('SC_HOST_REPO_PATH', _REPO_PATH)
        # the same way as the updater: a helper container with the host's path of the checkout, which the
        # Docker daemon needs for build contexts and bind mounts
        # no --env-file: docker run parses .env differently from Compose (quotes, $$); Compose in the helper
        # reads /repo/.env itself
        r = subprocess.run(['docker', 'run', '-d', '--rm', '--name', 'hcm-self-heal',
                            '-v', '/var/run/docker.sock:/var/run/docker.sock', '-v', f'{host_repo}:/repo',
                            '-w', '/repo', f'{project}-dashboard',
                            'docker-compose', 'up', '-d', '--no-deps', '--build', *missing],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            app.logger.error('could not start %s: %s', ', '.join(missing), (r.stderr or r.stdout).strip()[:300])
    except Exception as e:  # noqa: BLE001 — never take the dashboard down over this
        app.logger.error('self-heal of services failed: %s', e)


if os.getenv('HCM_SELF_HEAL', '1') != '0':
    threading.Thread(target=_self_heal_services, daemon=True, name='self-heal').start()


def _origin_url() -> str:
    """The browsable URL of the checkout's origin; nothing about the repository is hardcoded."""
    return origin_to_web_url(_git('remote', 'get-url', 'origin'))


def _restore_ownership(log=None) -> None:
    """git runs as root inside the container on the bind-mounted checkout and leaves root-owned
    files behind (seen 2026-10-22: the host user's next git pull failed with 'cannot lock ref:
    Permission denied'). Give everything back to the owner of the checkout's root."""
    try:
        if os.geteuid() != 0:
            return
        st = os.stat(_REPO_PATH)
        if st.st_uid == 0 and st.st_gid == 0:
            return
        changed = 0
        for root, dirs, files in os.walk(_REPO_PATH):
            for name in dirs + files:
                p = os.path.join(root, name)
                try:
                    cur = os.lstat(p)
                    if cur.st_uid != st.st_uid or cur.st_gid != st.st_gid:
                        os.lchown(p, st.st_uid, st.st_gid); changed += 1
                except OSError:
                    pass
        if changed and log:
            log(f'Restored ownership of {changed} file(s) to uid {st.st_uid}.')
    except Exception as exc:  # noqa: BLE001
        if log:
            log(f'Warning: could not restore ownership: {exc}')


def _read_version_file() -> str:
    """Return the version of the checkout (<repo>/VERSION), or '' if unreadable."""
    try:
        with open(os.path.join(_REPO_PATH, 'VERSION')) as f:
            return f.read().strip()
    except (FileNotFoundError, OSError):
        return ''


def _image_version() -> str:
    """The version this dashboard image was built from (VERSION copied in by the Dockerfile), or '' for an
    image from before 1.8.1. This, not the checkout, is what runs: a checkout moved to a release whose build
    failed (or was never built) showed the new number for a day while the old containers ran."""
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'VERSION')) as f:
            return f.read().strip()
    except OSError:
        return ''


def _versions() -> tuple[str, str]:
    """(running, checkout): the image's version, else the checkout's; and the checkout's."""
    checkout = _read_version_file()
    return (_image_version() or checkout), checkout


def _latest_remote_tag() -> str:
    """Return the highest semver-like tag known to git, or '' if none.

    Assumes `git fetch --tags` has run, so local tags mirror origin.
    """
    raw = _git('tag', '-l')
    return latest_semver_tag(raw.splitlines()) if raw else ''


def _changelog_between(local_tag: str, remote_tag: str) -> list[str]:
    if not (local_tag and remote_tag):
        return []
    raw = _git('log', f'{local_tag}..{remote_tag}',
               '--pretty=format:%s', '--no-merges')
    return [l for l in raw.splitlines() if l.strip()] if raw else []


def _check_for_update():
    # the running version is the image's and must be published even when the remote check fails, so the
    # footer always shows what is installed (not what the checkout says)
    current_version, checkout_version = _versions()
    _write_update_patch({
        'current_version': current_version or None,
        'current_label':   f'v{current_version}' if current_version else None,
        'checkout_version': checkout_version or None,
    })
    try:
        r = subprocess.run(
            # --force: a tag that was moved on GitHub replaces the local one instead of failing every check
            # (a plain fetch refuses to clobber an existing tag, and with --quiet it does not even say why)
            ['git', '-C', _REPO_PATH, 'fetch', '--tags', '--force', '--quiet'],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            _write_update_patch({
                'error': (r.stderr or r.stdout).strip() or 'git fetch failed',
                'last_checked': datetime.now(timezone.utc).isoformat(),
            })
            return

        _restore_ownership()
        remote_tag      = _latest_remote_tag()
        # tag conventionally has a leading 'v'; the version string itself does not
        remote_version  = remote_tag.lstrip('v') if remote_tag else ''
        local_tag       = f'v{current_version}' if current_version else ''

        update_available = is_newer(remote_version, current_version)
        # the checkout is ahead of what runs (an apply whose build or restart failed, or a manual git pull
        # without a rebuild): Apply builds and restarts it
        rebuild = is_newer(checkout_version, current_version)
        if rebuild and not update_available:
            update_available, remote_version = True, checkout_version
            remote_tag = remote_tag or f'v{checkout_version}'

        changelog = _changelog_between(local_tag, remote_tag) if update_available else []
        # applying moves the checkout to exactly that tag: say now if it would refuse (another branch,
        # local commits, an inconsistent release) instead of after the administrator presses Apply
        blocked = resolve_apply(_REPO_PATH, _latest_remote_tag())['error'] if update_available else None

        _write_update_patch({
            'update_available': update_available,
            'rebuild_needed':   rebuild,
            'current_version':  current_version or None,
            'checkout_version': checkout_version or None,
            'remote_version':   remote_version  or None,
            # rendered by the existing frontend; keep populated for back-compat
            'current_label':    f'v{current_version}' if current_version else None,
            'remote_label':     f'v{remote_version}'  if remote_version  else None,
            'changelog':        changelog,
            'apply_blocked':    blocked,
            'repo_url':         _origin_url() or None,
            'last_checked':     datetime.now(timezone.utc).isoformat(),
            'error':            None,
        })
    except Exception as exc:
        _write_update_patch({
            'error': str(exc),
            'last_checked': datetime.now(timezone.utc).isoformat(),
        })


def _update_check_daemon():
    import time
    _check_for_update()
    while True:
        time.sleep(86400)
        _check_for_update()


threading.Thread(target=_update_check_daemon, daemon=True, name='hcm-update-checker').start()

# mails about failed / overdue backup verifications to the closest admin (verify_mail.py); every worker runs
# one, a row is claimed atomically so it is mailed once
import verify_mail  # noqa: E402
verify_mail.start()


def _apply_migrations_on_startup():
    """
    Apply any pending Postgres migrations once the dashboard process boots.

    Runs in a background thread so that:
      - the dashboard's HTTP server isn't blocked waiting for Postgres to be
        reachable (the two containers start in parallel under compose);
      - a deployment that ships a new migration but only restarts services
        (not the user-triggered Apply Update flow) still picks it up.
    A failing migration is retried every few minutes instead of given up on:
    the backup service needs the new tables, and a fix that lands on disk (the
    SQL is read from the repo mount) must not wait for a dashboard restart.
    Errors are logged but never raised — the auto-updater path
    (_do_apply_update) is the authoritative migration application moment;
    this is a belt-and-braces backstop.
    """
    init_dir = os.path.join(_REPO_PATH, 'postgres', 'init')
    if not os.path.isdir(init_dir):
        return
    import migrations as _migrations
    applied = _migrations.apply_until_done(_pg, init_dir)
    try:
        new_perms = access_db.sync_known_permissions()
        if new_perms:
            app.logger.warning('this version brings new permissions (custom roles do not get them by themselves): %s',
                               ', '.join(new_perms))
    except Exception as exc:  # noqa: BLE001
        app.logger.warning('could not record the known permissions: %s', exc)
    if applied:
        app.logger.info(
            'Applied %d pending migration(s) on startup: %s',
            len(applied), ', '.join(applied),
        )


threading.Thread(target=_apply_migrations_on_startup, daemon=True, name='hcm-migrations').start()


def _do_apply_update():
    def log(msg):
        ts = datetime.now(timezone.utc).strftime('%H:%M:%S')
        with _update_lock():
            state = _read_update_file()
            state.setdefault('apply_log', []).append(f'[{ts}] {msg}')
            _store_update_file(state)

    def failed(why: str):
        # the old containers still run: keep the old version on show and Apply on offer
        log(why)
        _write_update_patch({'apply_in_progress': False, 'apply_failed': why[:500]})

    try:
        # exactly the advertised release: fetch the tags, decide before touching anything, then move the
        # checkout to the tag's commit (never `git pull` of whatever the branch holds now)
        log('Fetching releases...')
        r = subprocess.run(['git', '-C', _REPO_PATH, 'fetch', '--tags', '--force', '--quiet'],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            log(f'git fetch failed: {(r.stderr or r.stdout).strip() or "no detail"}; nothing was changed.')
            _write_update_patch({'apply_in_progress': False})
            return
        plan = resolve_apply(_REPO_PATH, _latest_remote_tag())
        if plan['error']:
            log(f"Not applied: {plan['error']}")
            _write_update_patch({'apply_in_progress': False, 'apply_blocked': plan['error']})
            return
        where = 'detached (pinned to a release)' if plan['branch'] == 'HEAD' else f"branch {plan['branch']}"
        log(f"Release {plan['tag']} (VERSION {plan['tag_version']}); this installation is on {where}.")

        log('Stashing local changes...')
        stash = subprocess.run(
            ['git', '-C', _REPO_PATH, 'stash'],
            capture_output=True, text=True, timeout=30,
        )
        had_stash = 'No local changes' not in stash.stdout

        log(f"Moving to {plan['tag']}...")
        ok, out = move_to_tag(_REPO_PATH, plan)
        if not ok:
            if had_stash:
                subprocess.run(['git', '-C', _REPO_PATH, 'stash', 'pop'],
                               capture_output=True)
            log(f'Moving to {plan["tag"]} failed: {out}; nothing was changed.')
            _write_update_patch({'apply_in_progress': False})
            return
        log(out or f"Now at {plan['tag']}.")

        if had_stash:
            log('Restoring local settings...')
            pop = subprocess.run(
                ['git', '-C', _REPO_PATH, 'stash', 'pop'],
                capture_output=True, text=True, timeout=30,
            )
            if pop.returncode != 0:
                # building now would bake conflict markers into the images
                _restore_ownership(log)
                failed(f"Your local changes conflict with {plan['tag']}: nothing was built or restarted. On the server, "
                       'resolve them (git status; the changes are kept in git stash list), then apply again.')
                return

        _restore_ownership(log)
        now = _read_version_file()

        log('Building new images (no downtime yet)...')
        try:
            r = subprocess.run(
                ['docker-compose', 'build'],
                capture_output=True, text=True, timeout=600,
                cwd=_REPO_PATH,
            )
        except subprocess.TimeoutExpired:
            failed('docker-compose build did not finish within 10 minutes; the running version is unchanged. Apply again.')
            return
        if r.returncode != 0:
            failed(f'docker-compose build failed: {(r.stderr or r.stdout).strip()[-2000:]}')
            return

        log('Applying pending database migrations...')
        try:
            import migrations as _migrations
            init_dir = os.path.join(_REPO_PATH, 'postgres', 'init')
            with _pg() as conn:
                applied = _migrations.apply_pending(conn, init_dir)
            if applied:
                log(f'Applied {len(applied)} migration(s): {", ".join(applied)}')
            else:
                log('Schema already up-to-date — no migrations to apply.')
        except Exception as exc:
            log(f'Migration step failed: {exc}')
            failed('Aborting recreate — fix the migration error and re-run Apply Update.')
            return

        log('Build done. Spawning restart container...')
        project = os.getenv('COMPOSE_PROJECT_NAME', 'drsproject')
        # _REPO_PATH is /repo inside the container, but the Docker daemon needs
        # the real host-side path for volume mounts
        host_repo = os.getenv('SC_HOST_REPO_PATH', _REPO_PATH)
        # remove stale updater container if a previous run left one behind
        subprocess.run(['docker', 'rm', '-f', 'hcm-auto-updater'],
                       capture_output=True)
        # no --env-file: docker run parses .env differently from Compose (quotes, $$); Compose in the helper
        # reads /repo/.env itself
        r = subprocess.run(
            [
                'docker', 'run', '-d', '--rm',
                '--name', 'hcm-auto-updater',
                '-v', '/var/run/docker.sock:/var/run/docker.sock',
                '-v', f'{host_repo}:/repo',
                '-w', '/repo',
                f'{project}-dashboard',
                'sh', '-c', restart_command(),
            ],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            failed(f'docker run failed: {(r.stderr or r.stdout).strip()}')
            return
        log(f'Restart container spawned ({r.stdout.strip()[:12]}); the databases keep running. Page will reload shortly.')
        # only now is the new version on its way in: say so (until the restart replaces this container)
        _write_update_patch({'apply_in_progress': False, 'apply_failed': None, 'update_available': False,
                             'current_version': now or None, 'current_label': f'v{now}' if now else None,
                             'apply_blocked': None, 'changelog': []})
    except Exception as exc:
        failed(f'Error: {exc}')


@app.route('/api/update/status')
@requires(None)
def api_update_status():
    state = _read_update_file()
    return jsonify({
        'update_available':  state.get('update_available', False),
        'current_version':   state.get('current_version'),
        'remote_version':    state.get('remote_version'),
        'current_label':     state.get('current_label'),
        'remote_label':      state.get('remote_label'),
        'changelog':         state.get('changelog', []),
        'repo_url':          state.get('repo_url'),
        'last_checked':      state.get('last_checked'),
        'error':             state.get('error'),
        'apply_in_progress': bool(state.get('apply_in_progress')) and not _apply_is_stale(state),
        'apply_log':         state.get('apply_log', []),
        'apply_blocked':     state.get('apply_blocked'),
        'apply_failed':      state.get('apply_failed'),
        'checkout_version':  state.get('checkout_version'),
        'rebuild_needed':    state.get('rebuild_needed', False),
    })


@app.route('/api/update/check', methods=['POST'])
@requires('install.manage', 'root')
def api_update_check():
    threading.Thread(target=_check_for_update, daemon=True).start()
    return jsonify({'ok': True})


@app.route('/api/update/apply', methods=['POST'])
@requires('install.manage', 'root')
def api_update_apply():
    with _update_lock():                     # check-and-set: two clicks (or two workers) start one apply
        state = _read_update_file()
        if state.get('apply_in_progress') and not _apply_is_stale(state):
            return jsonify({'error': 'Update already in progress'}), 409
        state.update({'apply_in_progress': True, 'apply_log': [], 'apply_failed': None, 'apply_pid': os.getpid(),
                      'apply_marker': _container_marker(), 'apply_started': time.time()})
        _store_update_file(state)
    _audit('install.update_apply', state.get('remote_label') or 'update',
           detail={'from': state.get('current_version'), 'to': state.get('remote_version')})
    threading.Thread(target=_do_apply_update, daemon=True).start()
    return jsonify({'ok': True})


# =============================================================================
# serve the frontend
# =============================================================================

@app.route('/')
@requires(None)
def index():
    return send_from_directory('static', 'index.html')


import access_api  # noqa: E402 — needs requires() and the helpers above
_access_helpers = access_api.register(app, requires, _access, _enabled_clusters, _vm_index_rows)
import mail_api  # noqa: E402
mail_api.register(app, requires)
import invite_api  # noqa: E402
invite_api.register(app, requires, public, _access, _access_helpers)
import account_api  # noqa: E402
account_api.register(app, requires, public, _access_helpers)
import guest_api  # noqa: E402
from types import SimpleNamespace  # noqa: E402
guest_api.register(app, sock, requires, public, SimpleNamespace(
    access=_access, can_vm=_can_vm, console_call=_console_call, console_binding=_console_binding, client_ip=_client_ip,
    relay_pipe=_relay_pipe, clusters=_enabled_clusters, vm_index_rows=_vm_index_rows))


# =============================================================================
# access summary and the route check
# =============================================================================
def _access_summary(acc) -> dict:
    """What the browser needs to show or hide controls (the server checks again on every call)."""
    ids = [c.cluster_id for c in _enabled_clusters()]
    clusters = {}
    for cid in acc.visible_clusters(ids):
        whole = acc.cluster_perms(cid)
        some: set = set()
        grants = []
        for gr in acc.vm_grants(cid):
            some |= gr.effective()
            grants.append({'vm': gr.vm_uuid, 'tag': gr.tag, 'perms': sorted(gr.effective())})
        clusters[cid] = {'all': sorted(whole), 'some': sorted(some - whole), 'grants': grants}
    nodes = {}
    for perm in sorted(rbac.NODE_LEVEL):
        top = acc.top_nodes_with(perm)
        if top:
            nodes[perm] = ['*' if n is None else n for n in top]
    return {'root_admin': acc.is_root_admin(), 'anywhere': sorted(acc.perms_anywhere()),
            'clusters': clusters, 'nodes': nodes}


@app.route('/api/access/routes')
@requires('install.manage', 'root')
def api_access_routes():
    """Every route with its declared permission (for the access tests and for review)."""
    out = []
    for rule in app.url_map.iter_rules():
        fn = app.view_functions.get(rule.endpoint)
        decl = getattr(fn, '_hcm_perm', None)
        out.append({'rule': rule.rule, 'methods': sorted(m for m in rule.methods if m not in ('HEAD', 'OPTIONS')),
                    'endpoint': rule.endpoint, 'permission': decl[0] if decl else None,
                    'scope': decl[1] if decl else None, 'declared': decl is not None or rule.endpoint == 'static'})
    return jsonify({'routes': out, 'backup': [{'methods': m, 'path': rx, 'permission': p, 'target': t if isinstance(t, str) else list(t)}
                                               for m, rx, p, t in _BACKUP_ROUTES]})


def undeclared_routes() -> list[str]:
    """Routes without a permission declaration (requires/public). Must stay empty."""
    return sorted(ep for ep, fn in app.view_functions.items() if ep != 'static' and not hasattr(fn, '_hcm_perm'))


_undeclared = undeclared_routes()
if _undeclared:
    app.logger.error('routes without a permission declaration (they answer 500): %s', ', '.join(_undeclared))
    if os.getenv('HCM_STRICT_ROUTES') == '1':
        raise RuntimeError(f'routes without a permission declaration: {_undeclared}')

    @app.before_request
    def _refuse_undeclared():
        if request.endpoint in _undeclared:
            return jsonify({'error': 'route without a permission declaration'}), 500




if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)