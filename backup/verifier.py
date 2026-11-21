"""
verifier.py — planned verification of restore points, per folder (plans/PLAN-chain-verify.md).

Replaces the Sunday 05:00 re-read of every chain. Once a minute the tick looks at each folder and gives every
idle proxy of the folder's clusters at most one restore point to re-hash against its manifest (the proxy's
`verify` job with `seq`: that one point only). Working point by point keeps the load flat, lets a check stop
at the end of the allowed hours and continue the next night, and lets backups go first.

Which points: every restorable point (active and retired chains) once per interval; points never checked
first (a new point is checked within a day), then the longest unchecked; points with an open failure last.

Which proxies, when:
- a proxy on a **production** cluster: inside the folder's hours (local to that cluster), when no backup
  work of its own cluster is running or pending, and when the point is predicted to finish before both the
  end of the hours and the next backup work that cluster has (from the point's size and the proxy's measured
  read rate);
- a proxy on a cluster marked **DR** (normally passive): around the clock, and it keeps going after the
  due points with anything not checked for a day (continuous verification). While backups of the folder
  write to the share it pauses between points, unless the folder allows it to continue.
A verify takes the proxy's lock, like a backup or restore, so the two never share a proxy, and a backup or
restore that needs the proxy stops the verification on it (make_way): during a disaster the DR proxy restores
at once instead of finishing a multi-TB check first.

A failure is checked a second time before it counts (a NAS hiccup, or retention removing the point while it
was being read, must not raise an alarm). The verdict is recorded by incremental.record_verify_result, which
opens the alert the dashboard mails to the closest admin. Chains not checked for twice the interval raise an
'overdue' alert of their own.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

import db
import incremental
import recurrence
from proxy_client import ProxyError

log = logging.getLogger('backup.verifier')

DEFAULT_RATE = 50 * 1024 * 1024          # bytes/s until a proxy has measured verifications
CONTINUOUS_AGE = timedelta(days=1)       # a DR proxy re-checks anything older than this when nothing is due
OVERDUE_FACTOR = 2                       # a chain with a point unchecked for 2x the interval raises an alert
_SAFETY = 1.25                           # predicted duration margin

_running: dict[int, dict] = {}           # proxy id -> {'chain_id', 'point_id', 'run_id', 'started'}
_running_guard = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------ time helpers
def _zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or 'UTC')
    except Exception:                                   # noqa: BLE001
        return ZoneInfo('UTC')


def hours_left(now: datetime, tz_name: str | None, hours_from: str, hours_until: str) -> float | None:
    """Seconds left in today's allowed hours at `now` (local to the cluster), or None when outside them.
    The hours may run past midnight (22:00-04:00); equal times mean the whole day."""
    tz = _zone(tz_name)
    local = now.astimezone(tz)
    f, u = recurrence.parse_hhmm(hours_from), recurrence.parse_hhmm(hours_until)
    if f == u:
        return 86400.0
    for day in (local.date() - timedelta(days=1), local.date()):
        start = datetime.combine(day, f, tz)
        end = datetime.combine(day if u > f else day + timedelta(days=1), u, tz)
        if start <= local < end:
            return (end - local).total_seconds()
    return None


def predicted_seconds(point: dict, rate: float | None) -> float:
    return (int(point.get('bytes_written') or 0) / (rate or DEFAULT_RATE)) * _SAFETY + 30


# ------------------------------------------------------------------ backup work
def backup_busy(cluster_ids: list[str], folder_id: str | None, now: datetime) -> str | None:
    """Why these clusters' proxies are needed for backups now (None: free): a run going on, an open schedule
    window with members still to run, or a manual run asked for."""
    if db.active_runs_on(cluster_ids):
        return 'a backup is running'
    ids = set(cluster_ids)
    for pol, members in _policies_on(ids, folder_id):
        if pol.get('manual_at'):
            return f"schedule '{pol['name']}' runs now"
        if not pol['enabled']:
            continue
        try:
            w = recurrence.current_window(pol, now)
        except recurrence.RecurrenceError:
            continue
        if w is not None and w.opens <= now and not recurrence.in_blackout(pol, now):
            done = db.plans_run_in_occurrence(pol['id'], w.occurrence)
            if any(m['id'] not in done for m in members):
                return f"schedule '{pol['name']}' has backups to run"
    return None


def seconds_to_backups(cluster_ids: list[str], folder_id: str | None, now: datetime, limit_s: float) -> float:
    """Seconds until these clusters' next backup work starts (a schedule's window opening or a standalone
    plan's daily time), at most limit_s."""
    ids = set(cluster_ids)
    best = limit_s
    for pol, _members in _policies_on(ids, folder_id):
        if not pol['enabled']:
            continue
        try:
            nxt = recurrence.occurrences(pol, now, 1)
        except recurrence.RecurrenceError:
            continue
        if nxt:
            best = min(best, max(0.0, (nxt[0] - now).total_seconds()))
    for plan in db.standalone_plans_on(list(ids)):
        if plan.get('mode') != 'incremental' or not plan.get('run_time'):
            continue
        tz = _zone(plan.get('cluster_timezone'))
        local = now.astimezone(tz)
        try:
            t = recurrence.parse_hhmm(plan['run_time'])
        except recurrence.RecurrenceError:
            continue
        at = datetime.combine(local.date(), t, tz)
        if at < local:
            at += timedelta(days=1)
        best = min(best, (at - local).total_seconds())
    return best


def _policies_on(ids: set, folder_id: str | None) -> list[tuple[dict, list[dict]]]:
    """The folder's backup schedules with their active members on these clusters."""
    out = []
    for pol in db.get_policies(folder_id):
        if pol.get('kind') != 'incremental':
            continue
        members = [m for m in db.policy_members(pol['id']) if m['cluster_id'] in ids and m['enabled'] and m.get('cluster_enabled', True)]
        if members:
            out.append((pol, members))
    return out


# ------------------------------------------------------------------ the tick
def tick(now: datetime | None = None, start=None) -> int:
    """Hand idle proxies their next point. `start(proxy, point, settings, folder_id, deadline_s)` runs it
    (a thread in production; tests pass their own). Returns how many verifications were started."""
    now = now or _now()
    start = start or _start_thread
    started = 0
    for folder_id in db.verify_folders():
        try:
            started += _tick_folder(folder_id, now, start)
        except Exception:                               # noqa: BLE001 — one folder must not stop the others
            log.exception('verification tick for folder %s failed', folder_id)
    return started


def _tick_folder(folder_id: str | None, now: datetime, start) -> int:
    st = db.verify_settings(folder_id)
    interval = int(st['interval_days'] or 0)
    if interval <= 0:
        return 0
    clusters = db.folder_clusters(folder_id)
    if not clusters:
        return 0
    by_id = {c['cluster_id']: c for c in clusters}
    proxies = [p for c in clusters for p in db.get_proxies(c['cluster_id'], enabled_only=True) if p.get('vm_uuid')]
    with _running_guard:
        busy_proxies = set(_running)
        busy_chains = {r['chain_id'] for r in _running.values()}
        busy_points = {r['point_id'] for r in _running.values()}
    idle = [p for p in proxies if p['id'] not in busy_proxies and not incremental.proxy_lock(p['id']).locked()]
    if not idle:
        return 0
    # DR proxies first: they compete with nothing; production proxies take what is left
    idle.sort(key=lambda p: by_id[p['cluster_id']]['role'] != 'dr')
    due_before = now - timedelta(days=interval)
    share_busy = None
    candidates = {'due': None, 'continuous': None}
    started = 0
    for p in idle:
        cl = by_id[p['cluster_id']]
        if cl['role'] == 'dr':
            if not st['dr_during_backups']:
                if share_busy is None:
                    share_busy = backup_busy(list(by_id), folder_id, now) or ''
                if share_busy:
                    continue
            point = _pick(candidates, 'due', folder_id, due_before, busy_chains, busy_points) \
                or _pick(candidates, 'continuous', folder_id, now - min(CONTINUOUS_AGE, timedelta(days=interval)), busy_chains, busy_points)
            deadline = None
        else:
            left = hours_left(now, cl.get('timezone'), st['hours_from'], st['hours_until'])
            if left is None or backup_busy([cl['cluster_id']], folder_id, now):
                continue
            free = seconds_to_backups([cl['cluster_id']], folder_id, now, left)
            point = None
            rate = db.proxy_read_rate(p['id'])
            for cand in _list(candidates, 'due', folder_id, due_before):
                if cand['chain_id'] in busy_chains or cand['point_id'] in busy_points:
                    continue
                if predicted_seconds(cand, rate) > free:
                    continue                            # a smaller one may still fit
                point = cand
                break
            deadline = left
        if not point:
            continue
        if db.plan_run_active(point.get('plan_id')):
            busy_chains.add(point['chain_id'])          # retention may be pruning it: later
            continue
        lock = incremental.proxy_lock(p['id'])
        if not lock.acquire(blocking=False):
            continue
        busy_chains.add(point['chain_id'])
        busy_points.add(point['point_id'])
        with _running_guard:
            _running[p['id']] = {'chain_id': point['chain_id'], 'point_id': point['point_id'], 'started': now}
        try:
            start(dict(p, role=cl['role']), point, st, folder_id, deadline, lock)
            started += 1
        except Exception:                               # noqa: BLE001
            log.exception('could not start the verification of point %s on proxy %s', point['point_id'], p.get('name'))
            with _running_guard:
                _running.pop(p['id'], None)
            lock.release()
    return started


def _list(cache: dict, key: str, folder_id, older_than) -> list[dict]:
    if cache[key] is None:
        cache[key] = db.verify_candidates(folder_id, older_than)
    return cache[key]


def _pick(cache: dict, key: str, folder_id, older_than, busy_chains: set, busy_points: set) -> dict | None:
    for c in _list(cache, key, folder_id, older_than):
        if c['chain_id'] not in busy_chains and c['point_id'] not in busy_points:
            return c
    return None


# ------------------------------------------------------------------ one verification
def _start_thread(proxy, point, settings, folder_id, deadline_s, lock) -> None:
    threading.Thread(target=run_point, args=(proxy, point, folder_id, deadline_s, lock),
                     name=f"verify-{point['point_id']}", daemon=True).start()


def run_point(proxy: dict, point: dict, folder_id: str | None, deadline_s: float | None, lock) -> str:
    """Verify one point on one proxy (the caller holds the proxy's lock; it is released here). A failure
    is verified a second time before it is recorded. Returns the verify_runs status."""
    run_id = None
    status, msg, nbytes, dur = 'error', None, None, None
    try:
        run_id = db.add_verify_run(folder_id=folder_id, cluster_id=point['cluster_id'], proxy_id=proxy['id'],
                                   proxy_name=proxy.get('name'), proxy_role=proxy.get('role'), chain_id=point['chain_id'],
                                   point_id=point['point_id'], vm_name=point.get('vm_name'), seq=point['seq'])
        with _running_guard:
            if proxy['id'] in _running:
                _running[proxy['id']]['run_id'] = run_id
        share = db.get_share(point['target_id'], with_secret=True)
        if not share:
            status, msg = 'error', 'the chain has no destination share any more'
            return status
        full = db.get_proxy(proxy['id'], with_secret=True) or proxy
        pc = incremental.proxy_client_for(incremental.ensure_reachable(full))
        body = {'target': {'uri': share['uri_base'], 'username': share.get('username'), 'password': share.get('password')},
                'chain_dir': point['chain_dir'], 'seq': int(point['seq'])}
        timeout = deadline_s if deadline_s else 24 * 3600
        t0 = time.monotonic()
        res = _verify(pc, body, timeout, proxy['id'])
        r = (res.get('points') or {}).get(point['dir'])
        if r is not None and not r.get('ok'):
            # a second opinion before an alarm: unless a backup of this VM started meanwhile (retention may
            # have removed files of the point while it was read), then it is simply checked again later
            if db.plan_run_active(point.get('plan_id')) or not _still_ok(point):
                status, msg = 'stopped', 'a backup of this VM started during the check; it is verified again later'
                return status
            res = _verify(pc, body, timeout, proxy['id'])
        nbytes, dur = res.get('bytes'), round(time.monotonic() - t0, 1)
        if not _still_ok(point):
            status, msg = 'stopped', 'the point was removed by retention during the check'
            return status
        ok, failed = incremental.record_verify_result(point['chain_id'], res)
        if (res.get('points') or {}).get(point['dir']) is None:
            status, msg = 'error', 'the proxy did not report this point (renamed by retention?); verified again later'
        elif ok:
            status = 'ok'
        else:
            status, msg = 'failed', '; '.join(failed.values())[:1000] or 'checksum differs'
        return status
    except MadeWay as e:
        status, msg = 'stopped', f'made way for {e}; verified again later'
        return status
    except ProxyError as e:
        text = str(e)
        if 'did not finish within' in text:
            status, msg = 'stopped', 'stopped at the end of the allowed hours; continued in the next window'
        else:
            status, msg = 'error', text[:1000]
        return status
    except Exception as e:                              # noqa: BLE001
        log.exception('verification of point %s failed', point.get('point_id'))
        status, msg = 'error', f'unexpected error: {e}'[:1000]
        return status
    finally:
        if run_id:
            try:
                db.finish_verify_run(run_id, status, nbytes, dur, msg)
            except Exception:                           # noqa: BLE001
                log.warning('could not record verify run %s', run_id, exc_info=True)
        with _running_guard:
            _running.pop(proxy['id'], None)
        try:
            lock.release()
        except RuntimeError:
            pass


class MadeWay(Exception):
    pass


def _verify(pc, body: dict, timeout: float, proxy_id: int | None = None) -> dict:
    entry = _running.get(proxy_id) if proxy_id is not None else None
    if entry is not None and entry.get('preempted'):
        raise MadeWay(entry['preempted'])
    job = pc.submit('verify', body)
    if entry is not None:
        with _running_guard:
            entry['job'] = (pc, job)
            preempted = entry.get('preempted')
        if preempted:                                   # asked while the job was being submitted
            _cancel(pc, job)
            raise MadeWay(preempted)
    try:
        return pc.wait(job, timeout=timeout)['result'] or {}
    except ProxyError:
        if entry is not None and entry.get('preempted'):
            raise MadeWay(entry['preempted']) from None
        raise


def _cancel(pc, job) -> None:
    try:
        pc.cancel(job)
    except Exception:                                   # noqa: BLE001 — the job may have just finished
        pass


def make_way(proxy_id: int, what: str = 'a backup or restore') -> bool:
    """A backup or restore needs this proxy: stop the verification running on it (the point is verified again
    later). Returns True when one was stopped. The caller then waits for the proxy lock as usual; the
    verification releases it within one poll of the proxy job."""
    with _running_guard:
        entry = _running.get(proxy_id)
        if entry is None:
            return False
        entry['preempted'] = what
        job = entry.get('job')
    if job:
        _cancel(*job)
    log.info('verification on proxy %s makes way for %s', proxy_id, what)
    return True


def _still_ok(point: dict) -> bool:
    cur = db.get_point(point['point_id'])
    return bool(cur and cur.get('status') == 'ok' and cur.get('dir') == point['dir'])


# ------------------------------------------------------------------ overdue alerts and housekeeping
def check_overdue(now: datetime | None = None) -> dict:
    """Open an 'overdue' alert for every chain with a point unchecked for twice its folder's interval, and
    close the ones that caught up (or whose folder switched verification off). Also closes alerts of
    points that no longer exist."""
    now = now or _now()
    out = {'raised': 0, 'resolved': 0}
    try:
        out['resolved'] += db.resolve_alerts_of_gone_points()
    except Exception:                                   # noqa: BLE001
        log.warning('closing alerts of removed points failed', exc_info=True)
    for folder_id in db.verify_folders():
        try:
            st = db.verify_settings(folder_id)
            interval = int(st['interval_days'] or 0)
            open_ids = db.open_overdue_chain_ids(folder_id)
            late = {} if interval <= 0 else {c['id']: c for c in db.overdue_chains(folder_id, now - timedelta(days=interval * OVERDUE_FACTOR))}
            for cid, c in late.items():
                if cid in open_ids:
                    continue
                detail = (f"{c['points']} restore point(s) not verified since {c['since'].astimezone(timezone.utc):%Y-%m-%d %H:%M} UTC "
                          f"(the folder verifies every {interval} day(s))")
                if db.raise_verify_alert('overdue', c, None, detail):
                    out['raised'] += 1
            for cid in open_ids - set(late):
                out['resolved'] += db.resolve_verify_alerts(chain_id=cid, kind='overdue')
        except Exception:                               # noqa: BLE001
            log.exception('overdue check for folder %s failed', folder_id)
    return out


def running() -> list[dict]:
    """What the proxies are verifying right now (for the status view)."""
    with _running_guard:
        return [dict(v, proxy_id=k) for k, v in _running.items()]


def status(folder_id: str | None) -> dict:
    """Settings, counts and the proxies that take part, for the Schedules tab."""
    st = db.verify_settings(folder_id)
    clusters = db.folder_clusters(folder_id)
    now = _now()
    summary = db.verify_summary(folder_id, int(st['interval_days'] or 0) or 7)
    procs = []
    run_by = {r['proxy_id']: r for r in running()}
    for c in clusters:
        for p in db.get_proxies(c['cluster_id'], enabled_only=True):
            if not p.get('vm_uuid'):
                continue
            if c['role'] == 'dr':
                state = 'continuous'
            else:
                left = hours_left(now, c.get('timezone'), st['hours_from'], st['hours_until'])
                state = 'in_hours' if left is not None else 'waiting'
            procs.append({'id': p['id'], 'name': p.get('name'), 'cluster_id': c['cluster_id'], 'cluster_name': c['cluster_name'],
                          'role': c['role'], 'state': state, 'busy': p['id'] in run_by,
                          'rate': db.proxy_read_rate(p['id'])})
    return {'settings': st, 'summary': summary, 'clusters': clusters, 'proxies': procs}
