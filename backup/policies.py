"""
policies.py — backup schedules shared by many VMs. One dispatcher tick a minute: for every enabled
policy whose window is open and not in a blackout, start the members that have not run in this
occurrence yet, but never more at once per cluster than it can take (one per usable proxy for
backups, one export per cluster), so no run takes its snapshot and then queues for a proxy.
After a member's run the dispatcher records a notice when it ran into a blackout or past the
preferred finish. Members are plans with policy_id set; standalone plans keep their own cron job.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import db
import jobs
import recurrence

log = logging.getLogger('backup.policies')


def _now() -> datetime:
    return datetime.now(timezone.utc)


def capacity(cluster_id: str, kind: str) -> int:
    """How many runs of this kind the cluster takes at once."""
    if kind == 'incremental':
        return max(1, len([p for p in db.get_proxies(cluster_id, enabled_only=True) if p.get('vm_uuid')]))
    return 1


def due_members(policy: dict, window: recurrence.Window, pending: set) -> list[dict]:
    """Enabled members on enabled clusters that have not run in this occurrence and are not queued,
    the longest previous run first (unknown first: their size is the biggest unknown)."""
    done = db.plans_run_in_occurrence(policy['id'], window.occurrence)
    members = [m for m in db.policy_members(policy['id'])
               if m['enabled'] and m.get('cluster_enabled', True) and m['id'] not in done and m['id'] not in pending]
    took = db.last_active_seconds([m['id'] for m in members])
    return sorted(members, key=lambda m: (m['id'] in took, -took.get(m['id'], 0.0), m['vm_name'] or ''))


def tick(dispatch, pending: dict, now: datetime | None = None) -> int:
    """Start what is due. dispatch(plan_id, policy_id, occurrence, cluster_id, kind) queues a run;
    pending maps the plan ids dispatched and not finished to (cluster id, kind). Returns the number
    of runs dispatched."""
    now = now or _now()
    started = 0
    for policy in db.get_policies(all_folders=True, enabled_only=True):
        try:
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
    kind = policy['kind']
    free: dict[str, int] = {}
    started = 0
    for m in due_members(policy, window, set(pending)):
        cid = m['cluster_id']
        if cid not in free:
            mine = [pid for pid, c in pending.items() if c == (cid, kind)]   # dispatched, running or not yet
            free[cid] = capacity(cid, kind) - db.count_active_runs(cid, kind, exclude_plans=mine) - len(mine)
        if free[cid] <= 0:
            continue
        dispatch(m['id'], policy['id'], window.occurrence, cid, kind)
        pending[m['id']] = (cid, kind)
        free[cid] -= 1
        started += 1
    if started:
        log.info('policy %s: %s run(s) started for %s', policy['name'], started, window.occurrence)
    return started


def run_member(plan_id: int, policy_id: int, occurrence) -> None:
    """Scheduler entry point for a policy run: the plan's normal run, then the notices."""
    jobs.run_plan(plan_id, policy_id=policy_id, occurrence=occurrence)
    try:
        run = db.last_run_for(plan_id)
        policy = db.get_policy(policy_id)
        if run and policy and run.get('policy_id') == policy_id:
            codes = notices_for(policy, run)
            if codes:
                db.update_run(run['id'], notice=','.join(codes))
    except Exception:                                   # noqa: BLE001 — a notice is never worth failing a run over
        log.exception('notice check for plan %s failed', plan_id)


def notices_for(policy: dict, run: dict) -> list[str]:
    """'blackout': the run was still busy when a blackout began; 'late': it finished after the
    preferred finish of its occurrence. Only finished runs get notices."""
    if run.get('status') != 'COMPLETE' or not run.get('finished_at'):
        return []
    start = run.get('work_started_at') or run.get('started_at')
    end = run['finished_at']
    codes = []
    if start and recurrence.overlaps_blackout(policy, start, end):
        codes.append('blackout')
    occ = run.get('occurrence')
    if occ:
        opens = recurrence.occurrences(policy, _day_start(policy, occ), 1)
        if opens and opens[0].date() == occ and end > recurrence.window_for(policy, opens[0]).finish:
            codes.append('late')
    return codes


def _day_start(policy: dict, occ) -> datetime:
    """Just before the occurrence's day begins, in the policy zone (occurrences() is strictly after)."""
    tz = recurrence.zone(policy.get('timezone'))
    return datetime(occ.year, occ.month, occ.day, tzinfo=tz) - timedelta(seconds=1)
