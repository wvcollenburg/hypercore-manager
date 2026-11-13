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
from flask import Flask, jsonify, redirect, render_template, request, send_from_directory, Response, session, url_for
from flask_cors import CORS
from flask_sock import Sock
import simple_websocket
from influxdb_client import InfluxDBClient
import config_db
import user_db
import cluster_registry
import hypercore_tasks
import hcm_crypto
import storage_summary
from versioning import latest_semver_tag, is_newer, origin_to_web_url
from forwarding import body_cluster
from alerts import ALERT_LEVELS, ALERT_SEVERITY, active_items, condition_episodes, ms_iso

app = Flask(__name__, static_folder='static', template_folder='templates')
CORS(app)
# websockets (VM console): pings keep reverse proxies with a read timeout from closing an idle console
app.config['SOCK_SERVER_OPTIONS'] = {'ping_interval': 25, 'max_message_size': None}
sock = Sock(app)

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

# auth is enabled iff at least one user row exists. Cached at startup — restart
# is required if the table goes from empty to populated outside of the UI
_AUTH_ENABLED = user_db.has_any_users()


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _AUTH_ENABLED:
            return f(*args, **kwargs)
        if not session.get('user_id'):
            if request.path.startswith('/api/'):
                return jsonify({'error': 'Authentication required'}), 401
            return redirect(url_for('login_page'))
        if not request.path.startswith('/api/') and not user_db.get_by_username(session.get('username') or ''):
            # a cookie signed before the users table was recreated (a reinstall that kept the
            # secret key): the session passes but its user is gone, so it is treated as logged out
            session.clear()
            return redirect(url_for('login_page'))
        return f(*args, **kwargs)
    return decorated


def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _AUTH_ENABLED:
            return f(*args, **kwargs)
        if session.get('role') != 'admin':
            return jsonify({'error': 'Admin access required'}), 403
        return f(*args, **kwargs)
    return decorated


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
    _membership_cache.clear()


# folder memberships per user, cached briefly like the registry
_membership_cache: dict = {}


class BadRequest(Exception):
    pass


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
    return (not _AUTH_ENABLED) or session.get('role') == 'admin'


def _visible_folder_ids() -> set:
    uid = session.get('user_id') or ''
    now = time.time()
    hit = _membership_cache.get(uid)
    if hit and now - hit[0] < _REGISTRY_TTL:
        return hit[1]
    try:
        ids = cluster_registry.folder_ids_for_user(uid)
    except Exception as exc:
        app.logger.warning('folder memberships unavailable: %s', exc)
        ids = set()
    _membership_cache[uid] = (now, ids)
    return ids


def _visible_clusters():
    """Enabled clusters the current user may see: everything for admins, otherwise the
    clusters in folders the user is a member of. Unfiled clusters are admin-only."""
    enabled = _enabled_clusters()
    if _is_admin_request():
        return enabled
    folders = _visible_folder_ids()
    return [c for c in enabled if c.folder_id and c.folder_id in folders]


def _cluster_id() -> str:
    """Cluster the current request is about: ?cluster=<uuid> when it names a cluster the
    user may see, else the first visible cluster, else the defaults template (no data)."""
    visible = _visible_clusters()
    wanted = (request.args.get('cluster') or '').strip() if request else ''
    if wanted and cluster_registry.is_uuid(wanted):
        if any(c.cluster_id == wanted for c in visible):
            return wanted
    return visible[0].cluster_id if visible else cluster_registry.TEMPLATE_CLUSTER_ID


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
@require_auth
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

        # fetch latest allocated vCPUs per node from vm_inventory
        vcpu_query = f'''
        from(bucket: "{INFLUX_BUCKET}")
          |> range(start: -1h)
          {_flux_cluster()}
          |> filter(fn: (r) => r["_measurement"] == "vm_inventory")
          |> filter(fn: (r) => r["_field"] == "num_vcpu")
          |> filter(fn: (r) => r["vm_state"] == "RUNNING")
          |> last()
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
    their sizes from block_device_metrics by vsd_uuid. CD-ROMs (mounted ISOs) do not count."""
    if not vms:
        return
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
@require_auth
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
        
        vms = {}
        for table in result:
            for record in table.records:
                vm_uuid = record.values.get('vm_uuid', 'unknown')
                vm_name = record.values.get('vm_name', 'Unknown')
                node_uuid = record.values.get('node_uuid', 'unknown')

                # skip records from in-flight migration state (no node assignment yet)
                if node_uuid == 'unknown':
                    continue

                if vm_uuid not in vms:
                    vms[vm_uuid] = {
                        'uuid': vm_uuid,
                        'name': vm_name,
                        'node_uuid': node_uuid,
                        'node_peer_id': record.values.get('node_peer_id', 'unknown'),
                        'tag': record.values.get('vm_tag', ''),
                        'vlan': record.values.get('vm_vlan', '0'),
                        'history': []
                    }

                vms[vm_uuid]['history'].append({
                    'time': record.get_time().isoformat(),
                    'cpu_usage': record.values.get('cpu_usage'),
                    'rx_bit_rate': record.values.get('rx_bit_rate'),
                    'tx_bit_rate': record.values.get('tx_bit_rate')
                })
        
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
        return jsonify(list(listed.values()))
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        client.close()


# =============================================================================
# API: drive metrics (physical drives, nested under nodes)
# =============================================================================
@app.route('/api/drives')
@require_auth
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
@require_auth
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
        return jsonify(active)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        client.close()


# =============================================================================
# API: storage overview — per-VM series + cluster totals, no per-disk history
# =============================================================================
@app.route('/api/storage/summary')
@require_auth
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
        return jsonify(out)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        client.close()


# =============================================================================
# API: migration events (last 7 days by default)
# =============================================================================
@app.route('/api/cluster/tasks')
@require_auth
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
@require_auth
def get_migrations():
    range_days = request.args.get('days', '7')
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
@require_auth
def get_predictions():
    range_days = request.args.get('days', '7')
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
@require_auth
def get_forecast_runs():
    range_days = request.args.get('days', '7')
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
@require_auth
@require_admin
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
@require_auth
@require_admin
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
@require_auth
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
@require_auth
def get_config():
    """Return all tunables with metadata for the settings UI (selected cluster, or the
    defaults template when ?cluster=defaults)."""
    try:
        cid = config_db.TEMPLATE_CLUSTER_ID if request.args.get('cluster') == 'defaults' else _cluster_id()
        return jsonify(config_db.get_with_meta(cid))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/config', methods=['POST'])
@require_auth
@require_admin
def post_config():
    """Update one or more tunables. Accepts {key: value, ...} JSON body."""
    try:
        updates = request.get_json(force=True)
        if not isinstance(updates, dict):
            return jsonify({'error': 'Expected a JSON object'}), 400
        cid = config_db.TEMPLATE_CLUSTER_ID if request.args.get('cluster') == 'defaults' else _cluster_id()
        config_db.save_many(updates, cid)
        if request.args.get('apply_to_all') == '1':
            for c in _enabled_clusters():
                config_db.save_many(updates, c.cluster_id)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# =============================================================================
# cluster registry API — clusters are configured here, not in .env
# =============================================================================

def _cluster_payload(c, extras=None):
    d = c.to_dict()
    d['has_password'] = True
    if extras:
        d.update(extras.get(c.cluster_id, {}))
    return d


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
        for cid, nodes in cluster_registry.nodes_for(ids).items():
            out.setdefault(cid, {})['nodes'] = nodes
    except Exception as exc:
        app.logger.warning('cluster nodes unavailable: %s', exc)
    return out


@app.route('/api/clusters', methods=['GET'])
@require_auth
def api_clusters_list():
    """All registered clusters (never the defaults template) plus the selection the
    server would make for this request, so the UI can initialise its picker."""
    try:
        clusters = cluster_registry.list_all()
        folders = cluster_registry.list_folders()
    except Exception as e:
        return jsonify({'error': str(e), 'clusters': [], 'folders': [], 'selected': None}), 200
    admin = _is_admin_request()
    if not admin:
        allowed = _visible_folder_ids()
        clusters = [c for c in clusters if c.folder_id and c.folder_id in allowed]
        folders = [{k: v for k, v in f.items() if k != 'members'} for f in folders if f['folder_id'] in allowed]
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
@require_auth
@require_admin
def api_clusters_create():
    body = request.get_json(force=True, silent=True) or {}
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
        dup = next((c for c in cluster_registry.list_all() if c.hc_cluster_uuid == hc_uuid), None)
        if dup is None:
            # a cluster whose uuid we never learned (registered with force) still lists its nodes
            owner = cluster_registry.node_owner([n.get('node_uuid') for n in probe_nodes])
            dup = cluster_registry.get(owner['cluster_id']) if owner else None
        if dup and not body.get('force'):
            mine = next((n for n in probe_nodes if n.get('lan_ip') and n['lan_ip'] == f['host']), None)
            which = f" (node {mine['peer_id']} of it)" if mine and mine.get('peer_id') is not None else ''
            return jsonify({'error': f"this cluster is already registered as '{dup.name}'{which}", 'duplicate_of': dup.cluster_id}), 409
    try:
        cid = cluster_registry.create(f['name'], f['host'], f['username'], f['password'],
                                      verify_tls=f['verify_tls'], colour=f['colour'], hc_cluster_uuid=hc_uuid,
                                      api_version=f['api_version'], api_v2_available=probe.get('api_v2') if probe.get('ok') else None,
                                      hc_version=probe.get('version') or None)
    except Exception as e:
        msg = str(e)
        if 'ux_clusters_name' in msg or 'duplicate key' in msg:
            return jsonify({'error': 'a cluster with that name already exists'}), 409
        return jsonify({'error': msg}), 500
    if probe_nodes:
        cluster_registry.upsert_nodes(cid, probe_nodes, seed_host=f['host'])
    if probe.get('timezone'):
        cluster_registry.mark_seen(cid, timezone=probe['timezone'])
    _invalidate_registry_cache()
    c = cluster_registry.get(cid)
    return jsonify({'ok': True, 'cluster': _cluster_payload(c), 'probe': probe}), 201


@app.route('/api/clusters/<cluster_id>', methods=['PUT'])
@require_auth
@require_admin
def api_clusters_update(cluster_id):
    if not cluster_registry.get(cluster_id):
        return jsonify({'error': 'cluster not found'}), 404
    body = request.get_json(force=True, silent=True) or {}
    fields = {}
    for k in ('name', 'host', 'username', 'password', 'colour', 'api_version', 'timezone'):
        if k in body:
            fields[k] = body[k]
    for k in ('verify_tls', 'enabled'):
        if k in body:
            fields[k] = bool(body[k])
    if 'folder_id' in body:
        fields['folder_id'] = body['folder_id'] or None   # null unfiles the cluster
    try:
        cluster_registry.update(cluster_id, **fields)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        msg = str(e)
        if 'ux_clusters_name' in msg or 'duplicate key' in msg:
            return jsonify({'error': 'a cluster with that name already exists'}), 409
        return jsonify({'error': msg}), 500
    _invalidate_registry_cache()
    return jsonify({'ok': True, 'cluster': _cluster_payload(cluster_registry.get(cluster_id))})


@app.route('/api/clusters/<cluster_id>', methods=['DELETE'])
@require_auth
@require_admin
def api_clusters_delete(cluster_id):
    if not cluster_registry.get(cluster_id):
        return jsonify({'error': 'cluster not found'}), 404
    purge = request.args.get('purge') == '1'
    try:
        cluster_registry.delete(cluster_id, purge=purge)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    _invalidate_registry_cache()
    return jsonify({'ok': True, 'purged': purge})


@app.route('/api/clusters/test', methods=['POST'])
@require_auth
@require_admin
def api_clusters_test_new():
    """Connection test for the add/edit form (credentials from the body)."""
    f = _cluster_form(request.get_json(force=True, silent=True) or {})
    if not (f['host'] and f['username']):
        return jsonify({'ok': False, 'error': 'host and username are required'}), 400
    return jsonify(cluster_registry.test_connection(f['host'], f['username'], f['password'], f['verify_tls']))


@app.route('/api/clusters/<cluster_id>/test', methods=['POST'])
@require_auth
@require_admin
def api_clusters_test_existing(cluster_id):
    """Connection test with the stored credentials; records the outcome on the cluster."""
    c = cluster_registry.get(cluster_id)
    if not c:
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
@require_auth
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
@require_auth
def manual_image(filename):
    return send_from_directory(os.path.join(MANUAL_DIR, 'img'), filename)


@app.route('/api/clusters/<cluster_id>/nodes/<node_uuid>', methods=['DELETE'])
@require_auth
@require_admin
def api_clusters_forget_node(cluster_id, node_uuid):
    """Drop a node row that is no longer part of the cluster (present nodes are kept)."""
    if not cluster_registry.get(cluster_id):
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
@require_auth
def api_folders_list():
    try:
        folders = cluster_registry.list_folders()
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    if not _is_admin_request():
        allowed = _visible_folder_ids()
        folders = [{k: v for k, v in f.items() if k != 'members'} for f in folders if f['folder_id'] in allowed]
    return jsonify(folders)


@app.route('/api/folders', methods=['POST'])
@require_auth
@require_admin
def api_folders_create():
    body = request.get_json(force=True, silent=True) or {}
    try:
        fid = cluster_registry.create_folder(body.get('name', ''), body.get('colour'))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        if 'ux_folders_name' in str(e) or 'duplicate key' in str(e):
            return jsonify({'error': 'a folder with that name already exists'}), 409
        return jsonify({'error': str(e)}), 500
    _invalidate_registry_cache()
    return jsonify({'ok': True, 'folder_id': fid}), 201


@app.route('/api/folders/<folder_id>', methods=['PUT'])
@require_auth
@require_admin
def api_folders_update(folder_id):
    if not cluster_registry.is_uuid(folder_id):
        return jsonify({'error': 'invalid folder id'}), 400
    body = request.get_json(force=True, silent=True) or {}
    try:
        cluster_registry.update_folder(folder_id, name=body.get('name'), colour=body.get('colour'),
                                       share_id=body['share_id'] if 'share_id' in body else ...)
    except Exception as e:
        if 'ux_folders_name' in str(e) or 'duplicate key' in str(e):
            return jsonify({'error': 'a folder with that name already exists'}), 409
        return jsonify({'error': str(e)}), 500
    _invalidate_registry_cache()
    return jsonify({'ok': True})


@app.route('/api/folders/<folder_id>', methods=['DELETE'])
@require_auth
@require_admin
def api_folders_delete(folder_id):
    if not cluster_registry.is_uuid(folder_id):
        return jsonify({'error': 'invalid folder id'}), 400
    cluster_registry.delete_folder(folder_id)
    _invalidate_registry_cache()
    return jsonify({'ok': True})


@app.route('/api/folders/<folder_id>/members', methods=['PUT'])
@require_auth
@require_admin
def api_folders_members(folder_id):
    """Body: {"user_id": "...", "member": true|false}."""
    if not cluster_registry.is_uuid(folder_id):
        return jsonify({'error': 'invalid folder id'}), 400
    body = request.get_json(force=True, silent=True) or {}
    uid = (body.get('user_id') or '').strip()
    if not cluster_registry.is_uuid(uid):
        return jsonify({'error': 'invalid user id'}), 400
    cluster_registry.set_folder_member(folder_id, uid, bool(body.get('member', True)))
    _invalidate_registry_cache()
    return jsonify({'ok': True})


# =============================================================================
# Backup module proxy — the `backup` container owns plans, runs, imports and targets
# =============================================================================

_BACKUP_MUTATING = {'POST', 'PUT', 'DELETE'}


# keys that would reveal a target's path or account to a non-admin; the UI shows alias/folder only
_BACKUP_SENSITIVE_KEYS = frozenset({'base', 'user', 'username', 'path_uri_base', 'prune_path', 'smb_user',
                                    'source_uri', 'uri_base', 'uri'})


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
    _, token = _backup_env()
    auth = request.headers.get('Authorization', '')
    return not token or auth == f'Bearer {token}'


@app.route('/internal/proxy-image/version')
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


@app.route('/api/backup/status')
@require_auth
def backup_status():
    """Reachability of the optional backup container; the tab hides when it is off."""
    url, token = _backup_env()
    try:
        rq = urllib.request.Request(f'{url}/api/v1/status?cluster={_cluster_id()}',
                                    headers={'Authorization': f'Bearer {token}'} if token else {})
        with urllib.request.urlopen(rq, timeout=3) as resp:
            data = json.loads(resp.read().decode())
        data['available'] = True
        return jsonify(data if _is_admin_request() else _strip_sensitive(data))
    except Exception:
        return jsonify({'available': False})


@app.route('/api/backup/<path:sub>', methods=['GET', 'POST', 'PUT', 'DELETE'])
@require_auth
def backup_proxy(sub):
    """Forward to the backup API with the selected cluster and the bearer token.
    Reads are open to every user who can see the cluster; changes need admin."""
    # folder members manage plans and imports for their clusters (the target and its credentials
    # are the admin's business); target management itself is admin-only
    # VM edits and power actions: admins only for now (finer roles will follow, the console too)
    if sub.startswith(('targets', 'proxies', 'vmedit')) and not _is_admin_request():
        return jsonify({'error': 'Admin access required'}), 403
    url, token = _backup_env()
    cid = _cluster_id()
    # the validated cluster only: the backup service reads the first `cluster` it gets, so one sent by
    # the browser would otherwise win over the check above and reach a cluster the user may not see
    qs = urllib.parse.urlencode([(k, v) for k, v in request.args.items(multi=True) if k != 'cluster'] + [('cluster', cid)])
    body = None
    headers = {'Authorization': f'Bearer {token}'} if token else {}
    if request.method in ('POST', 'PUT'):
        payload = request.get_json(silent=True) or {}
        # a cluster named in the body is checked, not replaced (forms with their own cluster choice,
        # e.g. proxy deployment); a body without one gets the selected cluster where it is needed
        body_cid, err = body_cluster(payload, cid, {c.cluster_id for c in _visible_clusters()},
                                     fill='cluster_id' in payload or sub.endswith(('plans', 'import/start')))
        if err:
            return jsonify({'error': err}), 403
        if body_cid:
            payload['cluster_id'] = body_cid
        if sub == 'notices/mute':                    # who muted it is the session's user, not the body's
            payload['muted_by'] = session.get('username') or None
        if sub.startswith('vmedit'):                 # who changed or cancelled it, likewise
            payload['created_by'] = session.get('username') or ('anonymous' if not _AUTH_ENABLED else None)
        body = json.dumps(payload).encode()
        headers['Content-Type'] = 'application/json'
    if request.method == 'GET' and (sub.startswith('flr/mounts/') and sub.rsplit('/', 1)[-1] in ('file', 'zip')
                                    or re.fullmatch(r'proxies/\d+/logs', sub)):
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
            if _is_admin_request():
                return Response(raw, status=resp.status, mimetype='application/json')
            try:
                return jsonify(_strip_sensitive(json.loads(raw.decode() or 'null'))), resp.status
            except ValueError:
                return Response(raw, status=resp.status, mimetype='application/json')
    except urllib.error.HTTPError as e:
        return Response(e.read(), status=e.code, mimetype='application/json')
    except Exception as e:
        return jsonify({'error': f'Backup service unreachable: {e}'}), 503


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
    return [{'key': n.get('key'), 'kind': 'notice', 'notice_kind': n.get('kind'), 'severity': 'warning',
             'description': n.get('detail') or '', 'node_ip': n.get('node') or '', 'since': None}
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
@require_auth
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
    return jsonify({'clusters': out})


@app.route('/api/cluster-alerts/mute', methods=['POST'])
@require_auth
def cluster_alert_mute():
    """{cluster_id, key, days}: mute one alert for 1-365 days, for every user; days 0 lifts it."""
    b = request.get_json(silent=True) or {}
    cid, key = str(b.get('cluster_id') or ''), str(b.get('key') or '').strip()
    if cid not in {c.cluster_id for c in _visible_clusters()}:
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
@require_auth
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


def _client_ip() -> str:
    fwd = (request.headers.get('X-Forwarded-For') or '').split(',')[0].strip()
    return fwd or request.headers.get('X-Real-IP') or request.remote_addr or ''


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
@require_auth
def console_page(cluster_id, vm_uuid):
    """The console window of one VM (opened from the VM's details)."""
    if not _is_admin_request():
        return Response('Admin access required', status=403, mimetype='text/plain')
    c = _visible_cluster(cluster_id)
    if c is None or not re.fullmatch(r'[0-9A-Za-z-]{1,64}', vm_uuid):
        return Response('Unknown cluster or VM', status=404, mimetype='text/plain')
    return render_template('console.html', cluster_id=cluster_id, cluster_name=c.name, vm_uuid=vm_uuid)


@app.route('/api/console/ticket', methods=['POST'])
@require_auth
@require_admin
def console_ticket():
    """A one-time ticket (30 s, single use, this browser session, this VM) for /console/ws."""
    b = request.get_json(silent=True) or {}
    cid, vm = str(b.get('cluster_id') or ''), str(b.get('vm_uuid') or '')
    if _visible_cluster(cid) is None:
        return jsonify({'error': 'Unknown cluster'}), 404
    status, raw, ctype = _console_call('POST', 'tickets', payload={
        'cluster_id': cid, 'vm_uuid': vm, 'username': _console_user(),
        'binding': _console_binding(create=True), 'client_ip': _client_ip()}, timeout=30)
    return Response(raw, status=status, mimetype=ctype)


@sock.route('/console/ws')
def console_ws(ws):
    """The browser's console websocket: checks the HCM session and the ticket's binding, then pipes
    bytes to and from the console service's relay. The ticket itself is checked (and consumed) there."""
    if _AUTH_ENABLED and (not session.get('user_id') or session.get('role') != 'admin'):
        ws.close(reason=4401, message='not logged in as an admin')
        return
    binding = _console_binding(create=False)
    ticket = request.args.get('ticket', '')
    if not binding or not ticket:
        ws.close(reason=4401, message='console ticket required')
        return
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
    try:
        while True:
            data = ws.receive()
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
@require_auth
def vm_index():
    """Searchable facts of every VM of the selected cluster (live, through the console service)."""
    q = {'cluster': _cluster_id()}
    if request.args.get('fresh') == '1':
        q['fresh'] = '1'
    status, raw, ctype = _console_call('GET', 'vm-index', query=urllib.parse.urlencode(q), timeout=30)
    return Response(raw, status=status, mimetype=ctype.split(';')[0])


@app.route('/api/vm-info')
@require_auth
def vm_info():
    """Live details of one VM from HyperCore, through the console service's session (read-only, so
    for every user who may see the cluster)."""
    vm = request.args.get('vm') or ''
    if not re.fullmatch(r'[0-9A-Za-z-]{1,64}', vm):
        return jsonify({'error': 'vm required'}), 400
    status, raw, ctype = _console_call('GET', 'vm', query=urllib.parse.urlencode({'cluster': _cluster_id(), 'vm': vm}),
                                       timeout=30)
    return Response(raw, status=status, mimetype=ctype.split(';')[0])


@app.route('/api/console/<path:sub>', methods=['GET', 'POST', 'PUT', 'DELETE'])
@require_auth
@require_admin
def console_proxy(sub):
    """The console service's admin API: audit log, screenshots, the open-session check."""
    if not re.fullmatch(r'(sessions|screenshot|check(/(settings|run|learn|exclude|image|refs(/\d+)?|flags))?)', sub):
        return jsonify({'error': 'Not found'}), 404
    if sub == 'sessions' and not request.args.get('cluster'):
        cid = None                                    # the audit log across every cluster the admin sees
    else:
        cid = _cluster_id()
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
@require_auth
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
@require_auth
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
@require_auth
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
@require_auth
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
@require_auth
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
@require_auth
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
@require_auth
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
@require_auth
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
@require_auth
def sentinel_get_branding():
    """E7: Return current report branding configuration."""
    try:
        return jsonify(_get_branding())
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/sentinel/branding', methods=['PUT'])
@require_auth
@require_admin
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
@require_auth
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
@require_auth
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
@require_auth
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
@require_auth
@require_admin
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
        with _req.urlopen(rq, timeout=10) as resp:
            return jsonify(json.loads(resp.read().decode()))
    except urllib.error.HTTPError as e:
        body_err = e.read().decode(errors='replace')
        return jsonify({'error': body_err}), e.code
    except Exception as e:
        return jsonify({'error': f'Sentinel unreachable: {e}'}), 503


@app.route('/api/sentinel/trend/run', methods=['POST'])
@require_auth
@require_admin
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

@app.route('/login', methods=['GET', 'POST'])
def login_page():
    if not _AUTH_ENABLED:
        return redirect(url_for('index'))
    if session.get('user_id'):
        return redirect(url_for('index'))

    step = request.args.get('step', 'password')
    error = None

    if request.method == 'POST':
        if step == 'otp':
            # guard: must have completed the password step first
            pending_username = session.get('otp_pending')
            if not pending_username:
                return redirect(url_for('login_page'))
            code = request.form.get('otp', '').strip()
            if user_db.verify_otp_code(pending_username, code):
                u = user_db.get_by_username(pending_username)
                session.pop('otp_pending', None)
                session.pop('next', None)
                session['user_id']  = str(u['user_id'])
                session['username'] = u['username']
                session['role']     = u['role']
                return redirect(url_for('index'))
            error = 'Invalid OTP code. Check your authenticator app and try again.'
        else:
            username = request.form.get('username', '').strip()
            password = request.form.get('password', '')
            if user_db.verify_password(username, password):
                u = user_db.get_by_username(username)
                if u['otp_enabled']:
                    session['otp_pending'] = username
                    return redirect(url_for('login_page', step='otp'))
                session.pop('next', None)
                session['user_id']  = str(u['user_id'])
                session['username'] = u['username']
                session['role']     = u['role']
                return redirect(url_for('index'))
            error = 'Invalid username or password.'

    # pass otp_enabled=True only when on the OTP step (drives login.html hint text)
    pending = session.get('otp_pending')
    otp_hint = bool(pending and user_db.get_by_username(pending or ''))
    return render_template('login.html', step=step, error=error, otp_enabled=otp_hint)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login_page'))


# =============================================================================
# user management API
# =============================================================================

@app.route('/api/users/me')
@require_auth
def api_me():
    if not _AUTH_ENABLED:
        return jsonify({'user_id': None, 'username': 'dev', 'role': 'admin',
                        'otp_enabled': False, 'dev_mode': True, 'community_ack': None})
    u = user_db.get_by_username(session['username'])
    if not u:
        session.clear()                     # see require_auth: the session outlived its user
        return jsonify({'error': 'Authentication required'}), 401
    return jsonify({
        'user_id':     str(u['user_id']),
        'username':    u['username'],
        'role':        u['role'],
        'otp_enabled': u['otp_enabled'],
        'dev_mode':    False,
        'community_ack': bool(u.get('community_ack', False)),
    })


@app.route('/api/users/me/community-ack', methods=['POST'])
@require_auth
def api_me_community_ack():
    """Record that the current user has seen the community-support notice."""
    if not _AUTH_ENABLED:
        # no user row to persist on; the frontend falls back to localStorage
        return jsonify({'ok': True, 'dev_mode': True})
    user_db.set_community_ack(session['username'])
    return jsonify({'ok': True, 'dev_mode': False})


@app.route('/api/users', methods=['GET'])
@require_auth
@require_admin
def api_users_list():
    users = user_db.get_all()
    return jsonify([{
        'user_id':     str(u['user_id']),
        'username':    u['username'],
        'role':        u['role'],
        'otp_enabled': u['otp_enabled'],
        'created_at':  u['created_at'].isoformat() if u['created_at'] else None,
    } for u in users])


@app.route('/api/users', methods=['POST'])
@require_auth
@require_admin
def api_users_create():
    data = request.get_json(force=True) or {}
    username = (data.get('username') or '').strip()
    password = data.get('password', '')
    role     = data.get('role', 'user')
    if not username or not password:
        return jsonify({'error': 'username and password are required'}), 400
    if role not in ('admin', 'user'):
        return jsonify({'error': 'role must be "admin" or "user"'}), 400
    try:
        user_db.create_user(username, password, role)
        return jsonify({'ok': True})
    except psycopg2.errors.UniqueViolation:
        return jsonify({'error': f'Username "{username}" already exists'}), 409
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/users/<username>', methods=['DELETE'])
@require_auth
@require_admin
def api_users_delete(username):
    if username == session.get('username'):
        return jsonify({'error': 'Cannot delete your own account'}), 400
    try:
        user_db.delete_user(username)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/users/<username>/password', methods=['PUT'])
@require_auth
def api_users_set_password(username):
    data     = request.get_json(force=True) or {}
    is_self  = (_AUTH_ENABLED and username == session.get('username'))
    is_admin = (not _AUTH_ENABLED or session.get('role') == 'admin')
    if not is_self and not is_admin:
        return jsonify({'error': 'Access denied'}), 403
    new_password = data.get('new_password', '')
    if not new_password:
        return jsonify({'error': 'new_password is required'}), 400
    # non-admin users must supply their current password
    if is_self and not is_admin:
        if not user_db.verify_password(username, data.get('current_password', '')):
            return jsonify({'error': 'Current password is incorrect'}), 403
    try:
        user_db.set_password(username, new_password)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/users/me/otp/setup', methods=['GET'])
@require_auth
def api_otp_setup():
    username = session.get('username', 'dev')
    try:
        return jsonify(user_db.build_otp_setup(username))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/users/me/otp/enable', methods=['POST'])
@require_auth
def api_otp_enable():
    username = session.get('username', 'dev')
    data = request.get_json(force=True) or {}
    code = (data.get('code') or '').strip()
    if not code:
        return jsonify({'error': 'code is required'}), 400
    if user_db.enable_otp(username, code):
        return jsonify({'ok': True})
    return jsonify({'error': 'Invalid code — check your authenticator app and try again'}), 400


@app.route('/api/users/me/otp/disable', methods=['POST'])
@require_auth
def api_otp_disable():
    username = session.get('username', 'dev')
    data = request.get_json(force=True) or {}
    if _AUTH_ENABLED and not user_db.verify_password(username, data.get('password', '')):
        return jsonify({'error': 'Incorrect password'}), 403
    try:
        user_db.disable_otp(username)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# =============================================================================
# auto-update
# =============================================================================

_REPO_PATH       = os.getenv('SC_REPO_PATH', '/repo')
_UPDATE_FILE     = '/tmp/hcm_update.json'
_UPDATE_FILE_LOCK = threading.Lock()


def _read_update_file():
    try:
        with open(_UPDATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def _write_update_patch(patch):
    with _UPDATE_FILE_LOCK:
        state = _read_update_file()
        state.update(patch)
        with open(_UPDATE_FILE, 'w') as f:
            json.dump(state, f)


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
    """Shell command the restart container runs: recreate every service of the compose file that was
    just pulled, except the databases. The list comes from that new file, not from this (old) code:
    an updater from 1.1.x had a fixed list without `console` and never started 1.2.0's new service.
    The fixed list is the fallback when the file cannot be read."""
    skip = '|'.join(_DB_SERVICES)
    return ("S=$(docker-compose config --services 2>/dev/null | grep -vxE '" + skip + "' | tr '\\n' ' '); "
            "[ -n \"$S\" ] || S='" + ' '.join(fallback) + "'; "
            "exec docker-compose up -d --no-deps $S")


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
        r = subprocess.run(['docker', 'run', '-d', '--rm', '--name', 'hcm-self-heal',
                            '-v', '/var/run/docker.sock:/var/run/docker.sock', '-v', f'{host_repo}:/repo',
                            '--env-file', f'{_REPO_PATH}/.env', '-w', '/repo', f'{project}-dashboard',
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
    """Return the project version from <repo>/VERSION, or '' if unreadable."""
    try:
        with open(os.path.join(_REPO_PATH, 'VERSION')) as f:
            return f.read().strip()
    except (FileNotFoundError, OSError):
        return ''


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
    # the running version comes from the local VERSION file and must be published even
    # when the remote check fails, so the footer always shows what is installed
    current_version = _read_version_file()
    _write_update_patch({
        'current_version': current_version or None,
        'current_label':   f'v{current_version}' if current_version else None,
    })
    try:
        r = subprocess.run(
            ['git', '-C', _REPO_PATH, 'fetch', '--tags', '--quiet'],
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

        changelog = _changelog_between(local_tag, remote_tag) if update_available else []

        _write_update_patch({
            'update_available': update_available,
            'current_version':  current_version or None,
            'remote_version':   remote_version  or None,
            # rendered by the existing frontend; keep populated for back-compat
            'current_label':    f'v{current_version}' if current_version else None,
            'remote_label':     f'v{remote_version}'  if remote_version  else None,
            'changelog':        changelog,
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
    if applied:
        app.logger.info(
            'Applied %d pending migration(s) on startup: %s',
            len(applied), ', '.join(applied),
        )


threading.Thread(target=_apply_migrations_on_startup, daemon=True, name='hcm-migrations').start()


def _do_apply_update():
    def log(msg):
        ts = datetime.now(timezone.utc).strftime('%H:%M:%S')
        with _UPDATE_FILE_LOCK:
            state = _read_update_file()
            state.setdefault('apply_log', []).append(f'[{ts}] {msg}')
            with open(_UPDATE_FILE, 'w') as f:
                json.dump(state, f)

    try:
        log('Stashing local changes...')
        stash = subprocess.run(
            ['git', '-C', _REPO_PATH, 'stash'],
            capture_output=True, text=True, timeout=30,
        )
        had_stash = 'No local changes' not in stash.stdout

        log('Pulling latest code...')
        r = subprocess.run(
            ['git', '-C', _REPO_PATH, 'pull'],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode != 0:
            if had_stash:
                subprocess.run(['git', '-C', _REPO_PATH, 'stash', 'pop'],
                               capture_output=True)
            log(f'git pull failed: {(r.stderr or r.stdout).strip()}')
            _write_update_patch({'apply_in_progress': False})
            return
        log(r.stdout.strip() or 'Already up to date.')

        if had_stash:
            log('Restoring local settings...')
            pop = subprocess.run(
                ['git', '-C', _REPO_PATH, 'stash', 'pop'],
                capture_output=True, text=True, timeout=30,
            )
            if pop.returncode != 0:
                log(f'Warning: stash pop had conflicts — manual resolve needed on server.')
                log('Run: git -C /path/to/repo stash pop')

        _restore_ownership(log)

        log('Building new images (no downtime yet)...')
        r = subprocess.run(
            ['docker-compose', 'build'],
            capture_output=True, text=True, timeout=600,
            cwd=_REPO_PATH,
        )
        if r.returncode != 0:
            log(f'docker-compose build failed: {(r.stderr or r.stdout).strip()}')
            _write_update_patch({'apply_in_progress': False})
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
            log('Aborting recreate — fix the migration error and re-run Apply Update.')
            _write_update_patch({'apply_in_progress': False})
            return

        log('Build done. Spawning restart container...')
        _write_update_patch({'update_available': False})
        project = os.getenv('COMPOSE_PROJECT_NAME', 'drsproject')
        # _REPO_PATH is /repo inside the container, but the Docker daemon needs
        # the real host-side path for volume mounts
        host_repo = os.getenv('SC_HOST_REPO_PATH', _REPO_PATH)
        # remove stale updater container if a previous run left one behind
        subprocess.run(['docker', 'rm', '-f', 'hcm-auto-updater'],
                       capture_output=True)
        r = subprocess.run(
            [
                'docker', 'run', '-d', '--rm',
                '--name', 'hcm-auto-updater',
                '-v', '/var/run/docker.sock:/var/run/docker.sock',
                '-v', f'{host_repo}:/repo',
                '--env-file', f'{_REPO_PATH}/.env',
                '-w', '/repo',
                f'{project}-dashboard',
                'sh', '-c', restart_command(),
            ],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            log(f'docker run failed: {(r.stderr or r.stdout).strip()}')
            _write_update_patch({'apply_in_progress': False})
            return
        log(f'Restart container spawned ({r.stdout.strip()[:12]}); the databases keep running. Page will reload shortly.')
        _write_update_patch({'apply_in_progress': False})
    except Exception as exc:
        log(f'Error: {exc}')
        _write_update_patch({'apply_in_progress': False})


@app.route('/api/update/status')
@require_auth
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
        'apply_in_progress': state.get('apply_in_progress', False),
        'apply_log':         state.get('apply_log', []),
    })


@app.route('/api/update/check', methods=['POST'])
@require_auth
def api_update_check():
    threading.Thread(target=_check_for_update, daemon=True).start()
    return jsonify({'ok': True})


@app.route('/api/update/apply', methods=['POST'])
@require_auth
@require_admin
def api_update_apply():
    state = _read_update_file()
    if state.get('apply_in_progress'):
        return jsonify({'error': 'Update already in progress'}), 409
    _write_update_patch({'apply_in_progress': True, 'apply_log': []})
    threading.Thread(target=_do_apply_update, daemon=True).start()
    return jsonify({'ok': True})


# =============================================================================
# serve the frontend
# =============================================================================

@app.route('/')
@require_auth
def index():
    return send_from_directory('static', 'index.html')




if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)