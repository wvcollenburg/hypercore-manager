"""
testrestore.py — test restores (plans/PLAN-test-restores.md, T1 boot test + T2 network test).

Verification proves a point's bytes are what was written; a test restore proves the VM boots from them.

A **test plan** belongs to a folder. Every interval it queues a round: its fixed VMs plus N picked at random
from the folder's backed-up VMs (longest untested first; a tested VM rests N weeks). The tick (once a minute)
runs the queued tests, one at a time per target cluster:

1. Target: the folder's **DR cluster** when it has one with a proxy, else the VM's own cluster. On a DR cluster
   a test may run at any hour (paused while the folder's backups write to the share); on a production cluster
   only inside the plan's hours, when that cluster has no backup work running or due, and when the test is
   predicted to finish before the next backups.
2. Free memory: some node of the target must take the VM without passing the balancer's RAM ceiling.
3. Restore the newest point as `<vm>-hcmtest-<run>`, tagged `hcm-testrestore`, new MAC addresses, and start it:
   NICs **disconnected** (boot level) or on the plan's **isolated test VLAN** (network level; refused when any
   other VM on that cluster uses that VLAN). The chain is not re-verified first: the planned verification does
   that, and a test is about booting.
4. Sign of life: the guest agent answering (works without network) = pass by agent; else the console screen
   through the console service: a login, lock or sign-in screen = pass by screen; a stop screen, recovery-key
   prompt or nothing to boot = fail; nothing within the timeout = fail. The last screenshot is kept either way.
5. Network level: ping and TCP checks of the VM's addresses, run by a proxy of the target cluster through its
   NIC on the test VLAN (proxy job `netcheck`): HCM never touches the test VLAN.
6. Power off and delete the test VM (a failed one is kept N hours when the plan asks). The sweep deletes any
   leftover; it only ever touches VMs that carry both the tag and the name mark.

A failed test raises an alert (cluster card + mail to the closest admin, like verification); a fixed VM without
a passed test for twice the interval raises an 'overdue' one. A pass closes both.
"""
from __future__ import annotations

import base64
import json
import logging
import random
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import db
import incremental
import jobs
import verifier
from hypercore import HyperCoreError
from incremental import IncrementalError
from proxy_client import ProxyError

log = logging.getLogger('backup.testrestore')

TAG = 'hcm-testrestore'
NAME_MARK = '-hcmtest-'
DEFAULT_RATE = 50 * 1024 * 1024            # bytes/s a restore writes, until measured
POLL_S = 10
SCREEN_EVERY_S = 30
FIRST_SCREEN_S = 45

_running: dict[str, int] = {}              # target cluster id -> test run id
_guard = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------ rounds
def select_vms(plan: dict, now: datetime, rng=random) -> list[dict]:
    """The VMs of a new round: the fixed ones, then `random_count` from the pool (backed-up VMs of the folder,
    minus fixed and excluded ones, minus those tested within `rest_weeks`), longest untested first."""
    members = db.test_plan_vms(plan['id'])
    fixed = [m for m in members if m['mode'] == 'fixed']
    skip = {(m['cluster_id'], m['vm_uuid']) for m in members}
    out = [dict(cluster_id=m['cluster_id'], vm_uuid=m['vm_uuid'], vm_name=m.get('vm_name'), picked='fixed') for m in fixed]
    if plan.get('random_count'):
        rest = now - timedelta(weeks=int(plan.get('rest_weeks') or 4))
        pool = [v for v in db.backed_up_vms(plan['folder_id'])
                if (v['cluster_id'], v['vm_uuid']) not in skip and (v.get('last_tested') is None or v['last_tested'] < rest)]
        rng.shuffle(pool)                                   # ties broken at random
        pool.sort(key=lambda v: v.get('last_tested') or datetime.min.replace(tzinfo=timezone.utc))
        out += [dict(cluster_id=v['cluster_id'], vm_uuid=v['vm_uuid'], vm_name=v.get('vm_name'), picked='random')
                for v in pool[:int(plan['random_count'])]]
    return out


def queue_due(now: datetime) -> int:
    n = 0
    for plan in db.get_test_plans(all_folders=True):
        if not plan['enabled']:
            continue
        last = plan.get('last_run_at')
        if last and last > now - timedelta(days=int(plan['interval_days'])):
            continue
        n += queue_round(plan, now)
    return n


def queue_round(plan: dict, now: datetime) -> int:
    vms = select_vms(plan, now)
    n = db.queue_test_runs(plan, now, vms)
    log.info("test plan '%s': %s test(s) queued", plan['name'], n)
    return n


# ------------------------------------------------------------------ where and when
def target_for(folder_id: str | None, own_cluster: str) -> tuple[str, str]:
    """(target cluster id, role): a DR cluster of the folder with an enabled proxy (the one with the most free
    memory when there are several), else the VM's own cluster."""
    drs = [c for c in db.folder_clusters(folder_id) if c['role'] == 'dr'
           and any(p.get('vm_uuid') for p in db.get_proxies(c['cluster_id'], enabled_only=True))]
    if not drs:
        return own_cluster, db.cluster_role(own_cluster)
    if len(drs) == 1:
        return drs[0]['cluster_id'], 'dr'
    best, free = drs[0]['cluster_id'], -1
    for c in drs:
        try:
            f = max_free_memory(jobs.client_for(c['cluster_id']).list_nodes())
        except HyperCoreError:
            continue
        if f > free:
            best, free = c['cluster_id'], f
    return best, 'dr'


def max_free_memory(nodes: list[dict]) -> int:
    return max((int(n.get('memSize') or 0) - int(n.get('totalMemUsageBytes') or 0) for n in nodes if n.get('allowRunningVMs', True)),
               default=0)


def memory_fits(nodes: list[dict], mem: int, limit_pct: float) -> bool:
    """Some node that may run VMs takes `mem` more bytes and stays at or under the RAM ceiling."""
    for n in nodes:
        if not n.get('allowRunningVMs', True):
            continue
        size = int(n.get('memSize') or 0)
        if size and (int(n.get('totalMemUsageBytes') or 0) + int(mem)) * 100.0 / size <= limit_pct:
            return True
    return False


def predicted_seconds(restore_bytes: int, timeout_minutes: int) -> float:
    return restore_bytes / DEFAULT_RATE * 1.25 + timeout_minutes * 60 + 300


def may_start(plan: dict, role: str, target: dict, folder_id: str | None, now: datetime, need_s: float) -> str | None:
    """Why the test may not start now (None: it may)."""
    clusters = [c['cluster_id'] for c in db.folder_clusters(folder_id)]
    if role == 'dr':
        busy = verifier.backup_busy(clusters, folder_id, now)
        return f'waiting: {busy}' if busy else None
    left = verifier.hours_left(now, target.get('timezone'), plan['hours_from'], plan['hours_until'])
    if left is None:
        return 'waiting for the plan\'s hours'
    busy = verifier.backup_busy([target['cluster_id']], folder_id, now)
    if busy:
        return f'waiting: {busy}'
    free = verifier.seconds_to_backups([target['cluster_id']], folder_id, now, left)
    if need_s > free:
        return 'waiting: it would not finish before the hours end or the next backups'
    return None


# ------------------------------------------------------------------ the tick
def tick(now: datetime | None = None, start=None) -> int:
    """Queue due rounds, then start what may start: one test per target cluster at a time."""
    now = now or _now()
    start = start or _start_thread
    try:
        queue_due(now)
    except Exception:                                       # noqa: BLE001
        log.exception('queueing test rounds failed')
    started = 0
    with _guard:
        busy = set(_running)
    plans = {p['id']: p for p in db.get_test_plans(all_folders=True)}
    for run in db.queued_test_runs():
        plan = plans.get(run['plan_id'])
        if not plan or not plan['enabled']:
            db.update_test_run(run['id'], status='skipped', finished_at=now, detail='the test plan was disabled or deleted')
            continue
        try:
            target, role = target_for(plan['folder_id'], run['cluster_id'])
        except Exception as e:                              # noqa: BLE001
            log.warning('test %s: no target: %s', run['id'], e)
            continue
        if target in busy:
            continue
        point = db.newest_point_for_vm(run['cluster_id'], run['vm_uuid'])
        if not point:
            db.update_test_run(run['id'], status='skipped', finished_at=now, detail='the VM has no restore point any more')
            continue
        need = predicted_seconds(int(point.get('restore_bytes') or 0), int(plan['timeout_minutes']))
        tinfo = next((c for c in db.folder_clusters(plan['folder_id']) if c['cluster_id'] == target), {'cluster_id': target})
        why = may_start(plan, role, tinfo, plan['folder_id'], now, need)
        if why:
            continue
        if plan.get('byte_budget_gb'):
            used = db.round_bytes(plan['id'], run['round_at'])
            if used + int(point.get('restore_bytes') or 0) > int(plan['byte_budget_gb']) << 30:
                db.update_test_run(run['id'], status='skipped', finished_at=now,
                                   detail=f"byte budget of this round reached ({plan['byte_budget_gb']} GB)")
                continue
        with _guard:
            if target in _running:
                continue
            _running[target] = run['id']
        busy.add(target)
        db.update_test_run(run['id'], status='running', started_at=now, target_cluster_id=target, point_id=point['id'],
                           point_created_at=point['created_at'])
        try:
            start(run, plan, target, point)
            started += 1
        except Exception:                                   # noqa: BLE001
            log.exception('could not start test %s', run['id'])
            with _guard:
                _running.pop(target, None)
            db.update_test_run(run['id'], status='error', finished_at=_now(), detail='could not be started')
    return started


def _start_thread(run, plan, target, point) -> None:
    threading.Thread(target=run_test, args=(run, plan, target, point), name=f"test-restore-{run['id']}", daemon=True).start()


# ------------------------------------------------------------------ one test
class Skip(Exception):
    pass


class Fail(Exception):
    def __init__(self, verdict: str, detail: str):
        super().__init__(detail)
        self.verdict, self.detail = verdict, detail


def run_test(run: dict, plan: dict, target: str, point: dict, sleep=time.sleep, clock=time.monotonic) -> str:
    """One test, start to clean-up. Returns the final status."""
    rid = run['id']
    state = {'restored': None, 'png': None}
    status, verdict, detail, checks = 'error', None, None, None
    try:
        src = jobs.client_for(run['cluster_id'])
        source_vm = next((v for v in src.list_vms_full() if v.get('uuid') == run['vm_uuid']), None)
        agent = 'unknown' if not source_vm or source_vm.get('state') != 'RUNNING' else \
            ('available' if source_vm.get('guestAgentState') == 'AVAILABLE' else 'unavailable')
        db.update_test_run(rid, agent=agent)
        hc = jobs.client_for(target)
        pf = jobs.preflight(hc)
        if not pf['ok']:
            raise Skip(f"the target cluster cannot take a restore now: {pf['detail']}")
        mem = int((point.get('definition') or {}).get('mem') or 0)
        if mem and not memory_fits(hc.list_nodes(), mem, db.ram_limit_percent(target)):
            raise Skip(f'not enough free memory on the target cluster for {mem >> 30} GiB without passing the RAM ceiling')
        net = plan['level'] == 'network'
        probe_proxy = None
        if net:
            probe_proxy = network_ready(hc, target, plan)   # raises Fail with what is missing
        restored = restore(run, plan, target, point, net, state)
        verdict, detail = watch_boot(hc, target, restored, int(plan['timeout_minutes']) * 60, state, sleep=sleep, clock=clock)
        db.update_test_run(rid, booted_at=_now())
        if net:
            checks = network_checks(hc, run, plan, restored, source_vm, probe_proxy, point)
            ok, why = judge_checks(checks)
            if not ok:
                raise Fail('network', why)
            detail = f'{detail}; network checks passed'
        status = 'passed'
    except Skip as e:
        status, detail = 'skipped', str(e)
    except Fail as e:
        status, verdict, detail = 'failed', e.verdict, e.detail
    except (IncrementalError, HyperCoreError, ProxyError) as e:
        status, detail = 'error', str(e)[:1000]
    except Exception as e:                                  # noqa: BLE001
        log.exception('test %s failed unexpectedly', rid)
        status, detail = 'error', f'unexpected error: {e}'[:1000]
    finally:
        keep = None
        if status == 'failed' and plan.get('keep_failed_hours') and state['restored']:
            keep = _now() + timedelta(hours=int(plan['keep_failed_hours']))
        db.update_test_run(rid, status=status, verdict=verdict, detail=detail, checks=checks, finished_at=_now(),
                           screenshot=state['png'], keep_until=keep)
        if state['restored'] and not keep:
            cleanup(target, state['restored'], rid)
        _alerts(run, plan, status, verdict, detail)
        with _guard:
            _running.pop(target, None)
    log.info('[test %s] %s %s: %s', rid, run.get('vm_name'), status, detail)
    return status


def restore(run: dict, plan: dict, target: str, point: dict, net: bool, state: dict | None = None) -> str:
    """Restore the point as the test VM through the normal restore path; returns the new VM's uuid. The VM is
    put in state['restored'] as soon as it exists, so a restore that fails half-way is cleaned up too."""
    import restore as restore_mod
    chain = db.get_chain(point['chain_id'])
    name = f"{(run.get('vm_name') or 'vm')[:40]}{NAME_MARK}{run['id']}"
    params = {'mac_policy': 'new', 'start': True, 'verify': False, 'nic_connected': net, 'extra_tags': [TAG],
              'description': f"HCM test restore #{run['id']} of {run.get('vm_name')} (point {point['created_at']:%Y-%m-%d %H:%M} UTC); "
                             'deleted automatically'}
    if net:
        params['vlan'] = int(plan['test_vlan'])
    restore_id = db.add_restore(point, chain, name, params, requested_by='test restore', cluster_id=target)
    db.update_test_run(run['id'], restore_id=restore_id)
    restore_mod.run_restore(restore_id)
    r = db.get_restore(restore_id) or {}
    if r.get('created_uuid'):
        db.update_test_run(run['id'], restored_vm_uuid=r['created_uuid'])
        if state is not None:
            state['restored'] = r['created_uuid']
    if r.get('status') != 'COMPLETE':
        raise Fail('restore', f"the restore failed: {r.get('message') or 'no detail'}")
    if 'Not started' in (r.get('message') or ''):
        raise Fail('start', r['message'])
    bytes_ = _restore_bytes(r.get('message') or '')
    db.update_test_run(run['id'], restored_at=_now(), bytes=bytes_ if bytes_ is not None else point.get('restore_bytes'))
    return r['created_uuid']


def _restore_bytes(message: str) -> int | None:
    import re
    m = re.search(r'(\d+) MiB written', message)
    return int(m.group(1)) << 20 if m else None


def watch_boot(hc, target: str, vm_uuid: str, timeout_s: float, state: dict, sleep=time.sleep, clock=time.monotonic) -> tuple[str, str]:
    """('agent' | 'screen', detail) once the VM shows a sign of life; raises Fail otherwise."""
    t0 = clock()
    next_screen = t0 + FIRST_SCREEN_S
    last_screen = None
    while clock() - t0 < timeout_s:
        vm = hc.get_vm(vm_uuid)
        if vm.get('state') == 'CRASHED':
            _grab(target, vm_uuid, state)
            raise Fail('crashed', 'the VM crashed while booting')
        if vm.get('state') in ('SHUTOFF', 'SHUTDOWN') and clock() - t0 > 60:
            _grab(target, vm_uuid, state)
            raise Fail('stopped', 'the VM switched itself off while booting')
        if vm.get('guestAgentState') == 'AVAILABLE':
            _grab(target, vm_uuid, state)                # for the record; the agent decides
            return 'agent', f'guest agent answered after {int(clock() - t0)} s'
        if clock() >= next_screen:
            r = console_bootcheck(target, vm_uuid)
            if r.get('png'):
                state['png'] = base64.b64decode(r['png'])
            last_screen = r.get('detail')
            if r.get('verdict') == 'pass':
                return 'screen', f"{r.get('detail')} on the console after {int(clock() - t0)} s"
            if r.get('verdict') == 'fail':
                raise Fail('screen', f"{r.get('detail')} (console after {int(clock() - t0)} s)")
            next_screen = clock() + SCREEN_EVERY_S
        sleep(POLL_S)
    _grab(target, vm_uuid, state)
    raise Fail('timeout', f'no sign of life within {int(timeout_s // 60)} min (no guest agent, '
                          f"console: {last_screen or 'not read'})")


def _grab(target: str, vm_uuid: str, state: dict) -> None:
    try:
        r = console_bootcheck(target, vm_uuid)
        if r.get('png'):
            state['png'] = base64.b64decode(r['png'])
    except Exception:                                       # noqa: BLE001 — a screenshot is a bonus here
        pass


def console_bootcheck(cluster_id: str, vm_uuid: str) -> dict:
    """The console service's verdict on the VM's screen: {verdict: pass|fail|wait, detail, png}."""
    import os
    import hcm_crypto
    url = os.environ.get('CONSOLE_API_URL', 'http://console:8084').rstrip('/')
    body = json.dumps({'cluster_id': cluster_id, 'vm_uuid': vm_uuid}).encode()
    rq = urllib.request.Request(f'{url}/api/v1/bootcheck', data=body, method='POST',
                                headers={'Content-Type': 'application/json',
                                         'Authorization': f"Bearer {hcm_crypto.derived_token('console')}"})
    try:
        with urllib.request.urlopen(rq, timeout=60) as resp:
            return json.loads(resp.read().decode() or '{}')
    except Exception as e:                                  # noqa: BLE001 — console service down: keep waiting for the agent
        return {'verdict': 'wait', 'detail': f'console service: {e}'}


# ------------------------------------------------------------------ network level
def network_ready(hc, target: str, plan: dict) -> dict:
    """The proxy that runs the checks, after the VLAN guardrail. Raises Fail with what is missing."""
    vlan = plan.get('test_vlan')
    if vlan is None or not plan.get('probe_address'):
        raise Fail('setup', 'the plan has no test VLAN or probe address')
    users = [v.get('name') for v in hc.list_vms_full()
             if TAG not in (v.get('tags') or '').split(',') and not _is_proxy(v)
             and any(int(n.get('vlan') or 0) == int(vlan) for n in v.get('netDevs') or [])]
    if users:
        raise Fail('setup', f"VLAN {vlan} is in use on the target cluster by {', '.join(sorted(users)[:5])}: "
                            'a test VLAN must be used by test VMs only (restored VMs keep their addresses and names)')
    for p in db.get_proxies(target, enabled_only=True):
        if not p.get('vm_uuid'):
            continue
        caps = p.get('capabilities') or {}
        if not caps.get('host_network') or 'netcheck' not in (caps.get('jobs') or []):
            continue
        try:
            pvm = hc.get_vm(p['vm_uuid'])
        except HyperCoreError:
            continue
        nic = next((n for n in pvm.get('netDevs') or [] if int(n.get('vlan') or 0) == int(vlan) and n.get('macAddress')), None)
        if nic:
            return dict(p, test_mac=nic['macAddress'])
    raise Fail('setup', f'no proxy on the target cluster can test VLAN {vlan}: deploy one (1.7.0 or later) with a test NIC on that VLAN')


def _is_proxy(vm: dict) -> bool:
    return 'hcm-proxy' in (vm.get('tags') or '').split(',')


def network_checks(hc, run: dict, plan: dict, vm_uuid: str, source_vm: dict | None, proxy: dict, point: dict) -> list[dict]:
    """Ping and port checks of the test VM's addresses through the proxy's test NIC."""
    ips = _ips(hc.get_vm(vm_uuid)) or _ips(source_vm or {}) or _ips(point.get('definition') or {})
    blocked = _infrastructure(proxy, point)
    ips = [ip for ip in ips if ip not in blocked]
    if not ips:
        raise Fail('network', 'no IPv4 address known for the VM (no guest agent report, none recorded): nothing to check')
    member = next((m for m in db.test_plan_vms(plan['id']) if m['cluster_id'] == run['cluster_id'] and m['vm_uuid'] == run['vm_uuid']), None)
    ports = (member or {}).get('ports')
    ports = list(plan.get('default_ports') or []) if ports is None else ports
    full = db.get_proxy(proxy['id'], with_secret=True) or proxy
    pc = incremental.proxy_client_for(full)
    job = pc.submit('netcheck', {'mac': proxy['test_mac'], 'probe': plan['probe_address'], 'targets': [{'ip': ip, 'ports': ports} for ip in ips]})
    res = pc.wait(job, timeout=300)['result'] or {}
    return [{'ip': ip, 'ping': r.get('ping'), 'ports': r.get('ports') or {}} for ip, r in (res.get('results') or {}).items()]


def _ips(vm: dict) -> list[str]:
    out = []
    for n in vm.get('netDevs') or []:
        for ip in n.get('ipv4Addresses') or []:
            if ip and not ip.startswith(('127.', '169.254.')) and ip not in out:
                out.append(ip)
    return out


def _infrastructure(proxy: dict, point: dict) -> set:
    """Addresses never probed through the test NIC (their host route would cut the proxy off for the check)."""
    out = set()
    host = urlparse(proxy.get('url') or '').hostname
    if host:
        out.add(host)
    try:
        chain = db.get_chain(point['chain_id'])
        share = db.get_share(chain['target_id']) if chain and chain.get('target_id') else None
        h = urlparse((share or {}).get('uri_base') or '').hostname
        if h:
            out.add(h)
    except Exception:                                       # noqa: BLE001
        pass
    return out


def judge_checks(checks: list[dict]) -> tuple[bool, str]:
    """Pass when one of the VM's addresses answers on every port asked for (with no ports asked for: answers
    ping). Windows answers no ping by default, so ports are the better check."""
    if not checks:
        return False, 'no network result'
    for c in checks:
        ports = c.get('ports') or {}
        if ports and all(ports.values()):
            return True, f"{c['ip']} answers on {', '.join(str(p) for p in ports)}"
        if not ports and c.get('ping'):
            return True, f"{c['ip']} answers ping"
    parts = []
    for c in checks:
        closed = [str(p) for p, ok in (c.get('ports') or {}).items() if not ok]
        parts.append(f"{c['ip']}: " + (f"port(s) {', '.join(closed)} closed" if closed else ('no ping answer' if not c.get('ping') else 'ok')))
    return False, '; '.join(parts)


# ------------------------------------------------------------------ clean-up and alerts
def cleanup(cluster_id: str, vm_uuid: str, run_id: int | None = None) -> bool:
    """Power off and delete a test VM; only one that carries the tag and the name mark."""
    try:
        hc = jobs.client_for(cluster_id)
        try:
            vm = hc.get_vm(vm_uuid)
        except HyperCoreError:
            vm = None                                       # already gone
        if vm:
            if TAG not in (vm.get('tags') or '').split(',') or NAME_MARK not in (vm.get('name') or ''):
                log.error('refusing to delete %s (%s): not a test restore VM', vm.get('name'), vm_uuid)
                return False
            if vm.get('state') not in ('SHUTOFF', 'SHUTDOWN'):
                t = hc.vm_action(vm_uuid, 'STOP')
                if t:
                    incremental.wait_task(hc, t, 'power off test VM', 300)
            t = hc.delete_vm(vm_uuid)
            if t:
                incremental.wait_task(hc, t, 'delete test VM', 600)
        if run_id:
            db.update_test_run(run_id, cleaned_at=_now())
        return True
    except Exception as e:                                  # noqa: BLE001 — the sweep tries again
        log.warning('test VM %s on %s not deleted yet: %s', vm_uuid, cluster_id, e)
        return False


def sweep(now: datetime | None = None) -> int:
    """Delete finished test VMs (kept ones once their time is up), then any VM on any cluster that carries the
    test tag and name mark but belongs to no running or kept test (left behind by a restart)."""
    import cluster_registry
    now = now or _now()
    n = 0
    for r in db.test_vms_to_clean(now):
        if r.get('target_cluster_id') and cleanup(r['target_cluster_id'], r['restored_vm_uuid'], r['id']):
            n += 1
    keep = db.test_vms_in_use(now)
    # a test that is still restoring has created its VM before the VM's uuid is recorded: spare it by name
    running_names = {f'{NAME_MARK}{i}' for i in db.running_test_ids()}
    for c in cluster_registry.list_enabled():
        try:
            vms = jobs.client_for(c.cluster_id).list_vms_full()
        except HyperCoreError:
            continue
        for v in vms:
            name = v.get('name') or ''
            if any(name.endswith(m) for m in running_names):
                continue
            if TAG in (v.get('tags') or '').split(',') and NAME_MARK in name and v.get('uuid') not in keep:
                if cleanup(c.cluster_id, v['uuid']):
                    n += 1
    return n


def _alerts(run: dict, plan: dict, status: str, verdict: str | None, detail: str | None) -> None:
    try:
        if status == 'failed':
            db.raise_test_alert('test_failed', plan.get('folder_id'), run['cluster_id'], run['vm_uuid'], run.get('vm_name'),
                                f'{verdict}: {detail}')
        elif status == 'passed':
            db.resolve_test_alerts(run['cluster_id'], run['vm_uuid'])
    except Exception:                                       # noqa: BLE001
        log.warning('test alert bookkeeping failed', exc_info=True)


def check_overdue(now: datetime | None = None) -> dict:
    """A fixed VM of an enabled plan without a passed test for twice the plan's interval: an 'overdue' alert."""
    now = now or _now()
    out = {'raised': 0}
    for plan in db.get_test_plans(all_folders=True):
        if not plan['enabled']:
            continue
        limit = now - timedelta(days=2 * int(plan['interval_days']))
        if plan.get('created_at') and plan['created_at'] > limit:
            continue                                        # too young to be overdue
        for (cid, vm), r in db.last_test_passes(plan['id']).items():
            if r.get('passed') is None or r['passed'] < limit:
                since = r['passed'].strftime('%Y-%m-%d') if r.get('passed') else 'never'
                if db.raise_test_alert('test_overdue', plan.get('folder_id'), cid, vm, r.get('vm_name'),
                                       f"no passed test restore since {since} (plan '{plan['name']}' tests every {plan['interval_days']} day(s))"):
                    out['raised'] += 1
    return out


def running() -> dict:
    with _guard:
        return dict(_running)
