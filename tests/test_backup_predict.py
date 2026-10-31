"""backup/predict.py and backup/planner.py — learned backup durations and the plan of a night."""
import importlib
import os
import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

_BACKUP = os.path.join(os.path.dirname(__file__), '..', 'backup')
sys.path.insert(0, _BACKUP)
try:
    pr = importlib.import_module('predict')
    pl = importlib.import_module('planner')
    rec = importlib.import_module('recurrence')
finally:
    sys.path.remove(_BACKUP)

AMS = ZoneInfo('Europe/Amsterdam')
TUE = datetime(2026, 10, 20, 22, 0, tzinfo=AMS)


def _run(mib, copy, overhead=30.0, kind='delta', when=TUE, synth=None, mode='incremental'):
    return {'status': 'COMPLETE', 'mode': mode, 'point_kind': kind, 'blocks_changed': mib, 'copy_seconds': copy,
            'overhead_seconds': overhead, 'synth_seconds': synth, 'work_started_at': when}


def test_percentile_and_median():
    assert pr.percentile([1, 2, 3, 4, 5], 0.8) == pytest.approx(4.2)
    assert pr.median([3, 1, 2]) == 2 and pr.percentile([], 0.5) is None


def test_cluster_rate_ignores_tiny_copies_and_untimed_runs():
    runs = [_run(1000, 10), _run(2000, 20), _run(5, 0.2), dict(_run(500, 5), copy_seconds=None), _run(0, 3)]
    assert pr.cluster_rate(runs) == pytest.approx(100.0)


def test_a_vm_is_learned_after_enough_deltas():
    runs = [_run(600, 6, when=TUE - timedelta(days=d)) for d in range(4)]
    p = pr.predict(runs, 'delta', 'incremental', TUE.weekday(), 100.0, 'Europe/Amsterdam')
    assert p['learned'] is False and p['samples'] == 4 and p['seconds'] == pytest.approx(30 + 6)
    p = pr.predict(runs + [_run(600, 6, when=TUE - timedelta(days=4))], 'delta', 'incremental', TUE.weekday(), 100.0, 'Europe/Amsterdam')
    assert p['learned'] is True
    assert pr.predict(runs, 'delta', 'incremental', TUE.weekday(), None)['seconds'] is None, 'no read rate yet: nothing to go on'


def test_same_weekday_pattern_wins_once_there_are_enough_of_them():
    """Mondays carry the weekend's changes: 6000 MiB, other days 600 MiB."""
    mon = TUE - timedelta(days=1)
    runs = [_run(6000, 60, when=mon - timedelta(weeks=w)) for w in range(3)] + [_run(600, 6, when=TUE - timedelta(weeks=w)) for w in range(4)]
    assert pr.predict(runs, 'delta', 'incremental', mon.weekday(), 100.0, 'Europe/Amsterdam')['seconds'] == pytest.approx(30 + 60)
    assert pr.predict(runs, 'delta', 'incremental', TUE.weekday(), 100.0, 'Europe/Amsterdam')['seconds'] == pytest.approx(30 + 6)
    # Wednesday has no history of its own: all deltas, 80th percentile
    wed = pr.predict(runs, 'delta', 'incremental', (TUE + timedelta(days=1)).weekday(), 100.0, 'Europe/Amsterdam')
    assert wed['seconds'] == pytest.approx(30 + pr.percentile([6000] * 3 + [600] * 4, 0.8) / 100)


def test_full_and_synthetic_nights():
    deltas = [_run(600, 6, when=TUE - timedelta(days=d)) for d in range(5)]
    full = _run(40000, 400, kind='full', when=TUE - timedelta(days=20))
    assert pr.predict(deltas, 'full', 'incremental', 1, 100.0)['learned'] is False, 'no full seen yet'
    p = pr.predict(deltas + [full], 'full', 'incremental', 1, 100.0)
    assert p['learned'] and p['seconds'] == pytest.approx(30 + 400)
    # synthetic: the delta plus a full's worth at the read rate, until synthetic fulls ran
    s = pr.predict(deltas + [full], 'synthetic', 'incremental', TUE.weekday(), 100.0, 'Europe/Amsterdam')
    assert s['learned'] and s['seconds'] == pytest.approx(30 + 6 + 400)
    s = pr.predict([dict(deltas[0], synth_seconds=120.0)] + deltas[1:] + [full], 'synthetic', 'incremental', TUE.weekday(), 100.0, 'Europe/Amsterdam')
    assert s['seconds'] == pytest.approx(30 + 6 + 120)
    assert pr.predict(deltas, 'synthetic', 'incremental', TUE.weekday(), 100.0)['learned'] is False


def test_exports_learn_their_active_time():
    runs = [_run(0, 300, overhead=20, mode='export', kind=None) for _ in range(5)]
    p = pr.predict(runs, 'delta', 'export', 1, None)
    assert p['learned'] and p['seconds'] == pytest.approx(320)


# ------------------------------------------------------------------- planner
def _policy(**kw):
    p = {'rrule': 'FREQ=DAILY', 'starts_on': date(2026, 10, 1), 'window_open': '22:00', 'preferred_finish': '06:00',
         'buffer_minutes': 60, 'blackouts': [], 'timezone': 'Europe/Amsterdam'}
    p.update(kw)
    return p


def _members(*ids, cluster='c1'):
    return [{'id': i, 'cluster_id': cluster, 'vm_name': f'vm{i}'} for i in ids]


def _plan(policy, members, preds, caps):
    w = rec.current_window(policy, TUE)
    return pl.plan_night(policy, w, members, preds, caps)


def _by(plan):
    return {i['plan_id']: i for i in plan['items']}


def test_learned_members_end_at_the_target_and_learning_ones_start_at_the_opening():
    preds = {1: {'learned': True, 'seconds': 3600}, 2: {'learned': True, 'seconds': 1800}, 3: {'learned': False, 'seconds': None}}
    plan = _plan(_policy(), _members(1, 2, 3), preds, {'c1': 2})
    it = _by(plan)
    target = datetime(2026, 10, 21, 5, 0, tzinfo=AMS)
    assert plan['target'] == target and plan['overrun_seconds'] == 0
    assert it[3]['planned_start'] == TUE and it[3]['seconds'] == pl.DEFAULT_SECONDS and not it[3]['learned']
    assert it[1]['predicted_finish'] == target and it[1]['planned_start'] == target - timedelta(hours=1)
    assert it[2]['predicted_finish'] == target, 'the two learned ones go to different slots and both end at the target'
    assert it[1]['slot'] != it[2]['slot']


def test_one_slot_queues_learned_members_back_to_back():
    preds = {1: {'learned': True, 'seconds': 3600}, 2: {'learned': True, 'seconds': 1800}}
    it = _by(_plan(_policy(), _members(1, 2), preds, {'c1': 1}))
    assert it[2]['planned_start'] == datetime(2026, 10, 21, 3, 30, tzinfo=AMS)
    assert it[2]['predicted_finish'] == it[1]['planned_start'] and it[1]['predicted_finish'] == datetime(2026, 10, 21, 5, 0, tzinfo=AMS)


def test_a_night_that_does_not_fit_starts_at_the_opening_and_reports_the_overrun():
    preds = {i: {'learned': True, 'seconds': 3 * 3600} for i in (1, 2, 3)}
    plan = _plan(_policy(), _members(1, 2, 3), preds, {'c1': 1})
    assert min(i['planned_start'] for i in plan['items']) == TUE
    assert plan['end'] == TUE + timedelta(hours=9) and plan['overrun_seconds'] == 2 * 3600


def test_learned_members_avoid_a_slot_busy_with_learning_ones():
    preds = {1: {'learned': False, 'seconds': 6 * 3600}, 2: {'learned': True, 'seconds': 3 * 3600}}
    it = _by(_plan(_policy(), _members(1, 2), preds, {'c1': 2}))
    assert it[1]['slot'] != it[2]['slot'] and it[2]['predicted_finish'] == datetime(2026, 10, 21, 5, 0, tzinfo=AMS)


def test_starts_never_fall_in_a_blackout_and_clusters_are_planned_apart():
    pol = _policy(blackouts=[{'days': ['tue'], 'from': '22:00', 'to': '23:00'}])
    preds = {1: {'learned': False}, 2: {'learned': False}}
    plan = _plan(pol, _members(1) + _members(2, cluster='c2'), preds, {'c1': 1, 'c2': 1})
    assert plan['earliest'] == datetime(2026, 10, 20, 23, 0, tzinfo=AMS)
    assert {i['planned_start'] for i in plan['items']} == {datetime(2026, 10, 20, 23, 0, tzinfo=AMS)}


def test_a_dst_night_is_planned_in_real_time():
    """Europe/Amsterdam falls back at 03:00 on 25 Oct 2026: 02:00-03:00 happens twice. A 3-hour
    backup that must end at 05:00 starts 3 real hours earlier, not 3 wall-clock hours."""
    pol = _policy()
    w = rec.current_window(pol, datetime(2026, 10, 24, 23, 0, tzinfo=AMS))
    it = pl.plan_night(pol, w, _members(1), {1: {'learned': True, 'seconds': 3 * 3600}}, {'c1': 1})['items'][0]
    assert it['predicted_finish'] == datetime(2026, 10, 25, 5, 0, tzinfo=AMS)
    assert (it['predicted_finish'] - it['planned_start']).total_seconds() == 3 * 3600
    assert it['planned_start'] == datetime(2026, 10, 25, 1, 0, tzinfo=timezone.utc)


def test_an_hourly_occurrence_is_planned_forward_from_its_opening():
    pol = _policy(rrule='FREQ=HOURLY;INTERVAL=3')
    w = rec.current_window(pol, datetime(2026, 10, 20, 13, 0, tzinfo=AMS))
    preds = {1: {'learned': True, 'seconds': 600}, 2: {'learned': True, 'seconds': 300}, 3: {'learned': False}}
    plan = pl.plan_night(pol, w, _members(1, 2, 3), preds, {'c1': 1})
    it = _by(plan)
    assert plan['target'] == datetime(2026, 10, 20, 15, 0, tzinfo=AMS), 'the next occurrence, no buffer'
    assert min(i['planned_start'] for i in plan['items']) == datetime(2026, 10, 20, 12, 0, tzinfo=AMS)
    assert it[1]['learned'] and not it[3]['learned'] and plan['overrun_seconds'] == 0
