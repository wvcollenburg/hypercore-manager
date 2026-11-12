"""
planner.py — when each member of a backup schedule starts on a given night.

Per cluster the night has `capacity` slots (one per usable proxy for backups, one for exports),
and a slot runs one job at a time. The goal is to finish late, at `target = preferred finish -
buffer`, so the data is fresh and the early night quiet, while the buffer absorbs a slow night:

1. Members still learning start at the first allowed minute of the window, largest known first,
   each on the slot that frees up first. Their length is a guess (their own runs so far, or
   DEFAULT_SECONDS).
2. Learned members are placed backwards from the target, longest first, each on the slot whose
   queue starts latest, so every slot's last job ends at the target.
3. A slot whose learned queue would start before its learning block ends, or before the first
   allowed minute, is moved later as a whole; the night then ends past the target and the plan
   says by how much.
4. A start that falls inside a blackout moves to the end of it, pushing the rest of its slot.
An hourly schedule has no preferred finish to aim for: every member starts as early as it can
(step 1 for all of them), and the next occurrence is its target.
Pure functions; datetimes are aware. All arithmetic is done in UTC: Python adds a timedelta to a
datetime in a ZoneInfo zone on the wall clock, which is an hour off on a DST night.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import recurrence

DEFAULT_SECONDS = 30 * 60        # length assumed for a member nothing is known about


def plan_night(policy: dict, window: recurrence.Window, members: list[dict], predictions: dict,
               capacity: dict) -> dict:
    """members: plan rows (id, cluster_id, vm_name). predictions: plan id -> predict.predict() result.
    capacity: cluster id -> slots. Returns {target, earliest, items: [...], end, overrun_seconds}."""
    forward = recurrence.is_hourly(policy)
    buffer = 0 if forward else int(policy.get('buffer_minutes') or 0)
    target = window.finish.astimezone(timezone.utc) - timedelta(minutes=buffer)
    earliest = recurrence.next_allowed(policy, window.opens).astimezone(timezone.utc)
    items = []
    for cid in sorted({m['cluster_id'] for m in members}):
        group = [m for m in members if m['cluster_id'] == cid]
        items += _plan_cluster(policy, group, predictions, max(1, int(capacity.get(cid, 1))), earliest, target, forward)
    end = max((i['predicted_finish'] for i in items), default=None)
    over = max(0.0, (end - target).total_seconds()) if end else 0.0
    return {'target': target, 'earliest': earliest, 'items': items, 'end': end, 'overrun_seconds': over}


def _plan_cluster(policy, members, predictions, slots, earliest, target, forward=False):
    def length(m):
        p = predictions.get(m['id']) or {}
        return float(p['seconds']) if p.get('seconds') else float(DEFAULT_SECONDS)

    known = lambda m: bool((predictions.get(m['id']) or {}).get('learned'))  # noqa: E731
    learning = list(members) if forward else [m for m in members if not known(m)]
    learned = [] if forward else [m for m in members if known(m)]
    # 1. learning members from the first allowed minute, on the slot that frees up first
    front = [earliest] * slots
    queues: list[list[dict]] = [[] for _ in range(slots)]
    for m in sorted(learning, key=lambda m: (-length(m), m.get('vm_name') or '')):
        s = min(range(slots), key=lambda i: front[i])
        queues[s].append({'member': m, 'seconds': length(m), 'learned': known(m) if forward else False, 'front': True})
        front[s] += timedelta(seconds=length(m))
    # 2. learned members backwards from the target, longest first, on the slot starting latest
    back = [target] * slots
    tails: list[list[dict]] = [[] for _ in range(slots)]
    for m in sorted(learned, key=lambda m: (-length(m), m.get('vm_name') or '')):
        need = timedelta(seconds=length(m))
        fits = [i for i in range(slots) if back[i] - need >= front[i]]
        # the latest-starting queue it still fits in; else the one with the most room
        s = max(fits, key=lambda i: back[i]) if fits else max(range(slots), key=lambda i: back[i] - front[i])
        tails[s].insert(0, {'member': m, 'seconds': length(m), 'learned': True})
        back[s] -= timedelta(seconds=length(m))
    # 3. + 4. lay each slot out: learning block from the earliest minute, then the learned queue at
    # its backward start or as soon as the slot is free, whichever is later; blackouts push starts
    out = []
    for s in range(slots):
        t = earliest
        tail_start = back[s]
        for i, job in enumerate(queues[s] + tails[s]):
            if not job.get('front') and i == len(queues[s]):
                t = max(t, tail_start)
            t = recurrence.next_allowed(policy, t).astimezone(timezone.utc)
            finish = t + timedelta(seconds=job['seconds'])
            m = job['member']
            out.append({'plan_id': m['id'], 'cluster_id': m['cluster_id'], 'vm_name': m.get('vm_name'), 'slot': s,
                        'planned_start': t, 'predicted_finish': finish, 'seconds': job['seconds'], 'learned': job['learned']})
            t = finish
    return out
