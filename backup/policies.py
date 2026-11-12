"""
policies.py — backup schedules shared by many VMs. One dispatcher tick a minute: for every enabled
policy whose window is open and not in a blackout, start the members that have not run in this
occurrence yet, but never more at once per cluster than it can take (one per usable proxy for
backups, one export per cluster), so no run takes its snapshot and then queues for a proxy.
When the window opens the night is planned once (planner.py, from predict.py): members still
learning start right away, learned ones wait for their planned start so the night ends at the
preferred finish minus the buffer; a member whose planned start has passed starts as soon as a
slot is free. After a member's run the dispatcher records a notice when it ran into a blackout,
into the buffer or past the preferred finish. An hourly schedule starts every member as soon as
a slot is free. "Run now" (policies.manual_at) opens a manual occurrence that ignores the window
and the blackouts: every active member runs once, then it closes; its runs get no window notices.
Members are plans with policy_id set; standalone plans keep their own cron job.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import db
import jobs
import planner
import predict
import recurrence

log = logging.getLogger('backup.policies')


def _now() -> datetime:
    return datetime.now(timezone.utc)


def capacity(cluster_id: str, kind: str) -> int:
    """How many runs of this kind the cluster takes at once."""
    if kind == 'incremental':
        return max(1, len([p for p in db.get_proxies(cluster_id, enabled_only=True) if p.get('vm_uuid')]))
    return 1


def active_members(policy: dict) -> list[dict]:
    return [m for m in db.policy_members(policy['id']) if m['enabled'] and m.get('cluster_enabled', True)]


def predictions_for(policy: dict, members: list[dict], window: recurrence.Window) -> dict:
    """Plan id -> predict.predict() for this occurrence (what kind of point it will be, its weekday)."""
    kind = {'real': 'full', 'synthetic': 'synthetic'}.get(recurrence.full_mode(policy, window.opens), 'delta')
    history = db.run_history([m['id'] for m in members])
    rates = {cid: predict.cluster_rate(db.cluster_run_history(cid)) for cid in {m['cluster_id'] for m in members}} \
        if policy['kind'] == 'incremental' else {}
    weekday = window.opens.weekday()
    return {m['id']: predict.predict(history.get(m['id'], []), kind, policy['kind'], weekday, rates.get(m['cluster_id']),
                                     policy.get('timezone') or 'UTC')
            for m in members}


def night_plan(policy: dict, window: recurrence.Window, members: list[dict] | None = None) -> dict:
    """The planner's view of one night (not stored)."""
    members = active_members(policy) if members is None else members
    caps = {cid: capacity(cid, policy['kind']) for cid in {m['cluster_id'] for m in members}}
    preds = predictions_for(policy, members, window)
    out = planner.plan_night(policy, window, members, preds, caps)
    out['predictions'] = preds
    return out


def stored_night(policy: dict, window: recurrence.Window, members: list[dict]) -> dict:
    """Plan id -> planned row of this occurrence; planned (and stored) the first time it is asked."""
    night = db.get_night(policy['id'], window.occurrence)
    if not night and members:
        db.save_night(policy['id'], window.occurrence, night_plan(policy, window, members)['items'])
        night = db.get_night(policy['id'], window.occurrence)
    return night


def night_view(policy: dict, now: datetime, which: str = 'current') -> dict | None:
    """The open night (as stored when it opened, with what ran) or, for 'next' or when no window is
    open, the next one as the planner sees it now. None when the rule has no further occurrence."""
    window = recurrence.current_window(policy, now) if which == 'current' else None
    if window is None:
        nxt = recurrence.occurrences(policy, now, 1)
        if not nxt:
            return None
        window = recurrence.window_for(policy, nxt[0])
    members = active_members(policy)
    fresh = night_plan(policy, window, members)
    stored = db.get_night(policy['id'], window.occurrence) if window.opens <= now else {}
    runs = db.runs_in_occurrence(policy['id'], window.occurrence) if window.opens <= now else {}
    names = {m['id']: m for m in members}
    items = []
    for it in fresh['items']:
        row = stored.get(it['plan_id'])
        if row:
            it = dict(it, slot=row['slot'], planned_start=row['planned_start'], predicted_finish=row['predicted_finish'],
                      seconds=row['predicted_seconds'], learned=row['learned'])
        p = fresh['predictions'].get(it['plan_id']) or {}
        run = runs.get(it['plan_id'])
        items.append(dict(it, cluster_name=names[it['plan_id']].get('cluster_name'), samples=p.get('samples', 0), needed=p.get('needed'),
                          run={k: run.get(k) for k in ('status', 'started_at', 'work_started_at', 'finished_at', 'notice')} if run else None))
    end = max((i['predicted_finish'] for i in items), default=None)
    return {'occurrence': window.occurrence, 'opens': window.opens, 'finish': window.finish, 'target': fresh['target'],
            'earliest': fresh['earliest'], 'end': end, 'overrun_seconds': max(0.0, (end - fresh['target']).total_seconds()) if end else 0.0,
            'open': window.opens <= now < window.closes, 'stored': bool(stored),
            'full': recurrence.full_mode(policy, window.opens) in ('real', 'synthetic'),
            'items': sorted(items, key=lambda i: (i['cluster_id'], i['slot'], i['planned_start']))}


def due_members(policy: dict, window: recurrence.Window, pending: set, now: datetime) -> list[dict]:
    """Enabled members on enabled clusters that have not run in this occurrence and are not queued.
    Learned members wait for their planned start; members still learning (or added after the night
    was planned) are due at once and take a slot as soon as one frees up; in an hourly schedule
    every member is. Earliest planned first."""
    done = db.plans_run_in_occurrence(policy['id'], window.occurrence)
    members = active_members(policy)
    night = stored_night(policy, window, members)
    due = []
    for m in members:
        if m['id'] in done or m['id'] in pending:
            continue
        row = night.get(m['id'])
        if row and row['learned'] and row['planned_start'] > now and not recurrence.is_hourly(policy):
            continue
        due.append((row['planned_start'] if row else window.opens, m['vm_name'] or '', m))
    return [m for _, _, m in sorted(due, key=lambda x: (x[0], x[1]))]


def tick(dispatch, pending: dict, now: datetime | None = None) -> int:
    """Start what is due. dispatch(plan_id, policy_id, occurrence, cluster_id, kind, full) queues a run;
    pending maps the plan ids dispatched and not finished to (cluster id, kind). Returns the number
    of runs dispatched."""
    now = now or _now()
    started = 0
    for policy in db.get_policies(all_folders=True):
        try:
            if policy.get('manual_at'):
                started += _tick_manual(policy, dispatch, pending, now)
            if policy['enabled']:
                started += _tick_policy(policy, dispatch, pending, now)
        except Exception:                               # noqa: BLE001 — one bad policy must not stop the others
            log.exception('policy %s (%s): dispatch failed', policy['id'], policy['name'])
    return started


def _tick_policy(policy: dict, dispatch, pending: dict, now: datetime) -> int:
    for gone in db.release_foreign_members(policy):
        log.warning('policy %s: %s left the schedule (its cluster is no longer in the folder); its plan is paused',
                    policy['name'], gone['vm_name'])
    window = recurrence.current_window(policy, now)
    if window is None or now < window.opens or recurrence.in_blackout(policy, now):
        return 0
    started = _dispatch(policy, window, due_members(policy, window, set(pending), now), dispatch, pending,
                        recurrence.full_mode(policy, window.opens))
    if started:
        log.info('policy %s: %s run(s) started for %s', policy['name'], started, window.occurrence)
    return started


def _tick_manual(policy: dict, dispatch, pending: dict, now: datetime) -> int:
    """A manual occurrence: every active member that has not run in it yet, regardless of the
    window and the blackouts. It closes once every member has a run in it (or after a day)."""
    window = recurrence.manual_window(policy['manual_at'])
    members = active_members(policy)
    done = db.plans_run_in_occurrence(policy['id'], window.occurrence)
    if now >= window.closes or all(m['id'] in done for m in members):
        db.set_manual(policy['id'], None)
        return 0
    due = sorted((m for m in members if m['id'] not in done and m['id'] not in pending), key=lambda m: m['vm_name'] or '')
    # a manual run is an incremental (or what the chain needs), never a full the rule asks for
    started = _dispatch(policy, window, due, dispatch, pending, 'none' if policy.get('full_rrule') else None, manual=True)
    if started:
        log.info('policy %s: %s manual run(s) started', policy['name'], started)
    return started


def _dispatch(policy: dict, window: recurrence.Window, due: list[dict], dispatch, pending: dict, full,
              manual: bool = False) -> int:
    """Start due members while their cluster has a free slot."""
    kind = policy['kind']
    free: dict[str, int] = {}
    started = 0
    for m in due:
        cid = m['cluster_id']
        if cid not in free:
            mine = [pid for pid, c in pending.items() if c == (cid, kind)]   # dispatched, running or not yet
            free[cid] = capacity(cid, kind) - db.count_active_runs(cid, kind, exclude_plans=mine) - len(mine)
        if free[cid] <= 0:
            continue
        if manual:
            dispatch(m['id'], policy['id'], window.occurrence, cid, kind, full, manual=True)
        else:
            dispatch(m['id'], policy['id'], window.occurrence, cid, kind, full)
        pending[m['id']] = (cid, kind)
        free[cid] -= 1
        started += 1
    return started


def run_member(plan_id: int, policy_id: int, occurrence, full: str | None = None, manual: bool = False) -> None:
    """Scheduler entry point for a policy run: the plan's normal run (with what the full rule says
    for this occurrence), then the notices. A manual run has no window, so no window notices."""
    jobs.run_plan(plan_id, policy_id=policy_id, occurrence=occurrence, full=full)
    if manual:
        return
    try:
        run = db.last_run_for(plan_id)
        policy = db.get_policy(policy_id)
        if run and policy and run.get('policy_id') == policy_id:
            have = [c for c in (run.get('notice') or '').split(',') if c]      # the run may have set its own
            codes = have + [c for c in notices_for(policy, run) if c not in have]
            planned = db.get_night(policy_id, occurrence).get(plan_id)
            fields = {'notice': ','.join(codes)} if codes != have else {}
            if planned:
                fields.update(planned_start=planned['planned_start'], predicted_finish=planned['predicted_finish'])
            if fields:
                db.update_run(run['id'], **fields)
    except Exception:                                   # noqa: BLE001 — a notice is never worth failing a run over
        log.exception('notice check for plan %s failed', plan_id)


def notices_for(policy: dict, run: dict) -> list[str]:
    """'blackout': the run was still busy when a blackout began; 'late': it finished after the
    preferred finish of its occurrence (in an hourly schedule: after the next occurrence opened);
    'buffer': it finished inside the buffer before it. Only finished runs get notices."""
    if run.get('status') != 'COMPLETE' or not run.get('finished_at'):
        return []
    start = run.get('work_started_at') or run.get('started_at')
    end = run['finished_at']
    codes = []
    if start and recurrence.overlaps_blackout(policy, start, end):
        codes.append('blackout')
    occ = run.get('occurrence')
    if occ:
        finish = recurrence.window_for(policy, occ).finish.astimezone(timezone.utc)   # UTC: see planner.py
        buffer = 0 if recurrence.is_hourly(policy) else int(policy.get('buffer_minutes') or 0)
        if end > finish:
            codes.append('late')
        elif buffer and end > finish - timedelta(minutes=buffer):
            codes.append('buffer')
    return codes
