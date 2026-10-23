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

import hmac
import io
import json
import os
import re
import secrets
import subprocess
import threading
import time
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from functools import wraps

import psycopg2
import psycopg2.extras
from flask import Flask, jsonify, redirect, render_template, request, send_from_directory, Response, session, url_for
from flask_cors import CORS
from influxdb_client import InfluxDBClient
import config_db
import user_db
import cluster_registry
import hypercore_tasks
import hcm_crypto
import storage_summary
from versioning import latest_semver_tag, is_newer

app = Flask(__name__, static_folder='static', template_folder='templates')
CORS(app)

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
    range_hours = max(1, min(int(request.args.get('hours', '1')), 8760))
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
@app.route('/api/vms')
@require_auth
def get_vms():
    range_hours = max(1, min(int(request.args.get('hours', '1')), 8760))
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
        return jsonify(active)
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
    range_hours = max(1, min(int(request.args.get('hours', '1')), 8760))
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
    range_hours = max(1, min(int(request.args.get('hours', '1')), 8760))
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
    range_hours = max(1, min(int(request.args.get('hours', '1')), 8760))
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
        range_hours = max(1, min(int(request.args.get('hours', '1')), 8760))
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
    if sub.startswith(('targets', 'proxies')) and not _is_admin_request():
        return jsonify({'error': 'Admin access required'}), 403
    url, token = _backup_env()
    cid = _cluster_id()
    qs = request.query_string.decode()
    qs = (qs + '&' if qs else '') + f'cluster={cid}'
    body = None
    headers = {'Authorization': f'Bearer {token}'} if token else {}
    if request.method in ('POST', 'PUT'):
        payload = request.get_json(silent=True) or {}
        # the body's cluster is always the one the user has selected and may see
        if 'cluster_id' in payload or sub.endswith(('plans', 'import/start')):
            payload['cluster_id'] = cid
        body = json.dumps(payload).encode()
        headers['Content-Type'] = 'application/json'
    if request.method == 'GET' and sub.startswith('flr/mounts/') and sub.rsplit('/', 1)[-1] in ('file', 'zip'):
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


# H3 — sharing preferences proxy (Phase 4)

@app.route('/api/sentinel/sharing', methods=['GET'])
@require_auth
def sentinel_get_sharing():
    """H3: Forward GET /api/sentinel/sharing → sentinel /api/v1/sharing."""
    import urllib.request as _req
    import urllib.error

    sentinel_api_url = os.getenv('SENTINEL_API_URL', 'http://sentinel:8081')
    sentinel_token   = os.getenv('SENTINEL_API_TOKEN', '')

    try:
        headers = {}
        if sentinel_token:
            headers['Authorization'] = f'Bearer {sentinel_token}'
        rq = _req.Request(f'{sentinel_api_url}/api/v1/sharing?cluster={_cluster_id()}', headers=headers)
        with _req.urlopen(rq, timeout=10) as resp:
            return jsonify(json.loads(resp.read().decode()))
    except urllib.error.HTTPError as e:
        return jsonify({'error': e.read().decode(errors='replace')}), e.code
    except Exception as e:
        return jsonify({'error': f'Sentinel unreachable: {e}'}), 503


@app.route('/api/sentinel/sharing', methods=['PUT'])
@require_auth
@require_admin
def sentinel_put_sharing():
    """H3: Forward PUT /api/sentinel/sharing → sentinel /api/v1/sharing."""
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
            f'{sentinel_api_url}/api/v1/sharing?cluster={_cluster_id()}',
            data=payload, headers=headers, method='PUT',
        )
        with _req.urlopen(rq, timeout=10) as resp:
            return jsonify(json.loads(resp.read().decode()))
    except urllib.error.HTTPError as e:
        body_err = e.read().decode(errors='replace')
        return jsonify({'error': body_err}), e.code
    except Exception as e:
        return jsonify({'error': f'Sentinel unreachable: {e}'}), 503


@app.route('/api/sentinel/export/test-push', methods=['POST'])
@require_auth
def sentinel_export_test_push():
    """H1: Forward POST /api/sentinel/export/test-push → sentinel /api/v1/export/test-push."""
    import urllib.request as _req
    import urllib.error

    sentinel_api_url = os.getenv('SENTINEL_API_URL', 'http://sentinel:8081')
    sentinel_token   = os.getenv('SENTINEL_API_TOKEN', '')

    try:
        headers = {'Content-Type': 'application/json'}
        if sentinel_token:
            headers['Authorization'] = f'Bearer {sentinel_token}'
        rq = _req.Request(
            f'{sentinel_api_url}/api/v1/export/test-push?cluster={_cluster_id()}',
            data=b'{}', headers=headers, method='POST',
        )
        with _req.urlopen(rq, timeout=60) as resp:
            return jsonify(json.loads(resp.read().decode()))
    except urllib.error.HTTPError as e:
        body_err = e.read().decode(errors='replace')
        return jsonify({'error': body_err}), e.code
    except Exception as e:
        return jsonify({'error': f'Sentinel unreachable: {e}'}), 503


@app.route('/api/sentinel/export/snapshot')
@require_auth
def sentinel_export_snapshot():
    """H1: Forward GET /api/sentinel/export/snapshot → sentinel /api/v1/export/snapshot."""
    import urllib.request as _req
    import urllib.error

    sentinel_api_url = os.getenv('SENTINEL_API_URL', 'http://sentinel:8081')
    sentinel_token   = os.getenv('SENTINEL_API_TOKEN', '')

    try:
        headers = {}
        if sentinel_token:
            headers['Authorization'] = f'Bearer {sentinel_token}'
        rq = _req.Request(f'{sentinel_api_url}/api/v1/export/snapshot?cluster={_cluster_id()}', headers=headers)
        with _req.urlopen(rq, timeout=30) as resp:
            return jsonify(json.loads(resp.read().decode()))
    except urllib.error.HTTPError as e:
        return jsonify({'error': e.read().decode(errors='replace')}), e.code
    except Exception as e:
        return jsonify({'error': f'Sentinel unreachable: {e}'}), 503


@app.route('/api/sentinel/export/hcb')
@require_auth
def sentinel_export_hcb():
    """H4: Forward GET /api/sentinel/export/hcb → sentinel /api/v1/export/hcb (binary stream).
    Forwards the ?passphrase= query parameter supplied by the browser."""
    import urllib.request as _req
    import urllib.error
    from urllib.parse import urlencode

    sentinel_api_url = os.getenv('SENTINEL_API_URL', 'http://sentinel:8081')
    sentinel_token   = os.getenv('SENTINEL_API_TOKEN', '')

    passphrase = request.args.get('passphrase', '')
    qs = urlencode({'passphrase': passphrase}) if passphrase else ''
    url = f'{sentinel_api_url}/api/v1/export/hcb?cluster={_cluster_id()}'
    if qs:
        url = f'{url}?{qs}'

    try:
        headers = {}
        if sentinel_token:
            headers['Authorization'] = f'Bearer {sentinel_token}'
        rq = _req.Request(url, headers=headers)
        with _req.urlopen(rq, timeout=60) as resp:
            file_bytes          = resp.read()
            content_disposition = resp.headers.get('Content-Disposition', '')
            content_type        = resp.headers.get('Content-Type', 'application/octet-stream')
        response = Response(file_bytes, mimetype=content_type)
        if content_disposition:
            response.headers['Content-Disposition'] = content_disposition
        return response
    except urllib.error.HTTPError as e:
        body_err = e.read().decode(errors='replace')
        try:
            err_json = json.loads(body_err)
        except Exception:
            err_json = {'error': body_err}
        return jsonify(err_json), e.code
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

    Runs in a background thread with retry/back-off so that:
      - the dashboard's HTTP server isn't blocked waiting for Postgres to be
        reachable (the two containers start in parallel under compose);
      - a deployment that ships a new migration but only restarts services
        (not the user-triggered Apply Update flow) still picks it up.
    Errors are logged but never raised — the auto-updater path
    (_do_apply_update) is the authoritative migration application moment;
    this is a belt-and-braces backstop.
    """
    import time

    init_dir = os.path.join(_REPO_PATH, 'postgres', 'init')
    if not os.path.isdir(init_dir):
        return

    delay = 2
    for attempt in range(8):
        try:
            import migrations as _migrations
            with _pg() as conn:
                applied = _migrations.apply_pending(conn, init_dir)
            if applied:
                app.logger.info(
                    'Applied %d pending migration(s) on startup: %s',
                    len(applied), ', '.join(applied),
                )
            return
        except Exception as exc:
            app.logger.warning(
                'Startup migration check attempt %d/%d failed: %s',
                attempt + 1, 8, exc,
            )
            time.sleep(delay)
            delay = min(delay * 2, 30)

    app.logger.error('Startup migration check gave up after retries')


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
                'docker-compose', 'up', '-d', '--remove-orphans',
            ],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            log(f'docker run failed: {(r.stderr or r.stdout).strip()}')
            _write_update_patch({'apply_in_progress': False})
            return
        log(f'Restart container spawned ({r.stdout.strip()[:12]}). Page will reload shortly.')
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