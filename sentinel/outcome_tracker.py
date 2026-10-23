"""
outcome_tracker.py — C7: Right-sizing outcome tracking.

Architecture
------------
Runs as a third loop in sentinel.py on a long interval (default 6 hours).

For every right_sizing audit event that has not yet been resolved:
  1. Wait until outcome_check_delay_hours have passed since emitted_at.
  2. Fetch current VM spec (numVCPU, mem) from the HyperCore REST API.
  3. Fetch the VM's spec at recommendation time from InfluxDB (vm_inventory
     measurement, field num_vcpu, closest sample to emitted_at).
  4. Compare before vs. after and write:
       acted_on_at          = NOW()
       action_outcome        = 'resized_up' | 'resized_down' | 'no_action'
       post_action_snapshot  = {num_vcpu, mem_bytes, checked_at, source}

If HyperCore is unreachable the loop logs a warning and retries next cycle.
If the VM no longer exists in HyperCore, outcome is set to 'no_action' with
a note in post_action_snapshot.

InfluxDB "before" data is best-effort: if the vm_inventory measurement is
absent or the VM never appeared in it, the outcome tracker falls back to
comparing sizing_direction against the direction of any vcpu change it can
detect from HyperCore alone.  In that case the before_vcpu field in the
snapshot is null.
"""

import json
import logging
from datetime import datetime, timezone

import psycopg2.extras
import requests

import config as cfg_module
import db

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# HyperCore session helper
# ---------------------------------------------------------------------------

class _HCSession:
    """Thin authenticated wrapper around the HyperCore REST API, on the shared ClusterSession
    (re-login on 401, failover to another node of the cluster)."""

    def __init__(self, cfg: cfg_module.Config) -> None:
        import hypercore_session
        self._cfg = cfg
        self._hc = hypercore_session.ClusterSession(
            cfg.cluster_id, cfg.hc_username, cfg.hc_password, cfg.hc_verify_ssl,
            service='sentinel', name=cfg.cluster_name or cfg.cluster_id, seed=cfg.hc_host,
            logger=lambda msg: log.info('[%s] %s', cfg.cluster_name or cfg.cluster_id, msg))

    def login(self) -> None:
        self._hc.login()

    def logout(self) -> None:
        self._hc.logout()

    def get_vms(self) -> list[dict]:
        """Return list of VM objects from /VirDomain."""
        return self._hc.get_json('/VirDomain', timeout=15)


# ---------------------------------------------------------------------------
# InfluxDB "before" vcpu helper
# ---------------------------------------------------------------------------

def _fetch_before_vcpu(cfg: cfg_module.Config, vm_uuid: str, emitted_at: datetime) -> int | None:
    """
    Query InfluxDB for the num_vcpu value closest to emitted_at.
    Returns None if no data is found (treated as unknown / no comparison).
    """
    try:
        from influxdb_client import InfluxDBClient

        client = InfluxDBClient(
            url=cfg.influx_url,
            token=cfg.influx_token,
            org=cfg.influx_org,
            timeout=20_000,
            enable_gzip=True,
        )
        query_api = client.query_api()

        # look in a ±1-hour window around the recommendation time
        ts = emitted_at.strftime('%Y-%m-%dT%H:%M:%SZ')
        flux = f'''
from(bucket: "{cfg.influx_bucket}")
  |> range(start: time(v: "{ts}") |> duration.subtract(d: 1h),
           stop:  time(v: "{ts}") |> duration.add(d: 1h))
  |> filter(fn: (r) => r._measurement == "vm_inventory"
                    and r._field == "num_vcpu"
                    and r.vm_uuid == "{vm_uuid}")
  |> last()
  |> keep(columns: ["_value"])
'''
        tables = query_api.query(flux, org=cfg.influx_org)
        for table in tables:
            for record in table.records:
                val = record.values.get('_value')
                if val is not None:
                    return int(val)
    except Exception:
        log.debug('InfluxDB before-vcpu lookup failed for vm_uuid=%s', vm_uuid)
    return None


# ---------------------------------------------------------------------------
# core logic
# ---------------------------------------------------------------------------

def _determine_outcome(
    sizing_direction: str,
    before_vcpu: int | None,
    after_vcpu: int | None,
) -> str:
    """
    Derive action_outcome from sizing direction and vcpu change.

    If before_vcpu is unknown we cannot confirm a resize, so we return
    'no_action' as a conservative default.
    """
    if before_vcpu is None or after_vcpu is None:
        return 'no_action'

    if after_vcpu > before_vcpu:
        return 'resized_up'
    if after_vcpu < before_vcpu:
        return 'resized_down'
    return 'no_action'


def run_once(cfg: cfg_module.Config) -> None:
    """
    One pass of the outcome tracker:
      - Load pending right_sizing audit events past the check delay.
      - Fetch current VM specs from HyperCore.
      - Compare with InfluxDB "before" data and write outcomes.
    """
    if not cfg.hc_host:
        log.debug('Outcome tracker skipped — no cluster registered yet')
        return

    # ── 1. load pending events ──────────────────────────────────────────────
    try:
        with db.connect(cfg) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    '''
                    SELECT id, payload_snapshot, emitted_at
                    FROM audit.recommendation_events
                    WHERE cluster_id    = %s
                      AND event_type    = 'right_sizing'
                      AND acted_on_at   IS NULL
                      AND emitted_at    < NOW() - INTERVAL '1 hour' * %s
                    ORDER BY emitted_at ASC
                    LIMIT 200
                    ''',
                    [cfg.cluster_id, cfg.outcome_check_delay_hours],
                )
                pending = cur.fetchall()
    except Exception:
        log.exception('Outcome tracker: failed to load pending events')
        return

    if not pending:
        log.debug('Outcome tracker: no pending events')
        return

    log.info('Outcome tracker: checking %d pending events', len(pending))

    # ── 2. fetch current VM specs from HyperCore ────────────────────────────
    hc = _HCSession(cfg)
    try:
        hc.login()
        vms_raw = hc.get_vms()
    except Exception:
        log.warning('Outcome tracker: HyperCore unreachable — will retry next cycle')
        return
    finally:
        hc.logout()

    # build lookup: vm_uuid → {num_vcpu, mem_bytes}
    hc_by_uuid: dict[str, dict] = {}
    for vm in vms_raw:
        uid = vm.get('uuid') or vm.get('domainUUID') or ''
        if uid:
            hc_by_uuid[uid] = {
                'num_vcpu':  int(vm.get('numVCPU', 0)),
                'mem_bytes': int(vm.get('mem', 0)),
            }

    checked_at = datetime.now(tz=timezone.utc).isoformat()

    # ── 3. resolve each event ───────────────────────────────────────────────
    for row in pending:
        event_id   = row['id']
        emitted_at = row['emitted_at']
        payload    = row['payload_snapshot'] or {}

        vm_uuid          = payload.get('vm_uuid') or ''
        sizing_direction = payload.get('sizing_direction') or ''

        # current spec from HyperCore
        hc_spec = hc_by_uuid.get(vm_uuid)
        if hc_spec is None:
            # VM no longer exists — mark as no_action, note it
            post_snapshot = {
                'checked_at': checked_at,
                'source':     'hc_not_found',
                'before_vcpu': None,
                'after_vcpu':  None,
                'mem_bytes':   None,
            }
            outcome = 'no_action'
        else:
            after_vcpu  = hc_spec['num_vcpu']
            before_vcpu = _fetch_before_vcpu(cfg, vm_uuid, emitted_at)

            outcome = _determine_outcome(sizing_direction, before_vcpu, after_vcpu)

            post_snapshot = {
                'checked_at':  checked_at,
                'source':      'hypercore',
                'before_vcpu': before_vcpu,
                'after_vcpu':  after_vcpu,
                'mem_bytes':   hc_spec['mem_bytes'],
            }

        # ── 4. write outcome back to audit row ──────────────────────────────
        try:
            with db.connect(cfg) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        '''
                        UPDATE audit.recommendation_events
                           SET acted_on_at         = NOW(),
                               action_outcome        = %s,
                               post_action_snapshot  = %s
                         WHERE id = %s
                        ''',
                        [outcome, json.dumps(post_snapshot), event_id],
                    )
            log.info(
                'Outcome recorded',
                extra={
                    'event_id':  event_id,
                    'vm_uuid':   vm_uuid,
                    'direction': sizing_direction,
                    'outcome':   outcome,
                },
            )
        except Exception:
            log.exception('Outcome tracker: failed to write outcome for event_id=%s', event_id)
