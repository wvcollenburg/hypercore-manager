"""backup/policies.py — the backup-schedule dispatcher: windows, blackouts, capacity per cluster, notices."""
import importlib
import os
import sys
import types
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

_BACKUP = os.path.join(os.path.dirname(__file__), '..', 'backup')
_CLASH = ('config', 'db', 'log', 'api', 'hypercore', 'jobs', 'scheduler', 'incremental', 'proxy_client', 'manifest_names',
          'policies', 'recurrence')


def _import():
    saved = {n: sys.modules.pop(n) for n in _CLASH if n in sys.modules}
    sys.path.insert(0, _BACKUP)
    try:
        pol = importlib.import_module('policies')
        sch = importlib.import_module('scheduler')
        api = importlib.import_module('api')
    finally:
        sys.path.remove(_BACKUP)
        for n in _CLASH:
            sys.modules.pop(n, None)
        sys.modules.update(saved)
    return pol, sch, api


pol, sch, api = _import()
AMS = ZoneInfo('Europe/Amsterdam')


class _DB:
    def __init__(self):
        self.policies = [{'id': 1, 'name': 'Nightly', 'kind': 'incremental', 'enabled': True, 'folder_id': 'f1',
                          'rrule': 'FREQ=DAILY', 'starts_on': date(2026, 10, 1), 'window_open': '22:00',
                          'preferred_finish': '06:00', 'buffer_minutes': 60, 'blackouts': [], 'timezone': 'Europe/Amsterdam'}]
        self.members = [self._m(10, 'c1', 'a'), self._m(11, 'c1', 'b'), self._m(12, 'c1', 'c'), self._m(20, 'c2', 'd')]
        self.ran = set(); self.active = {}; self.proxies = {'c1': 2, 'c2': 0}; self.took = {}; self.released = []
        self.runs = {}; self.updates = []

    @staticmethod
    def _m(i, c, n): return {'id': i, 'cluster_id': c, 'vm_name': n, 'enabled': True, 'cluster_enabled': True}
    def get_policies(self, folder_id=None, all_folders=False, enabled_only=False): return [p for p in self.policies if p['enabled'] or not enabled_only]
    def get_policy(self, pid): return next((p for p in self.policies if p['id'] == pid), None)
    def release_foreign_members(self, p): return self.released
    def policy_members(self, pid): return self.members
    def plans_run_in_occurrence(self, pid, occ): return self.ran
    def last_active_seconds(self, ids): return {i: s for i, s in self.took.items() if i in ids}
    def get_proxies(self, cid, enabled_only=False): return [{'id': n, 'vm_uuid': f'p{n}'} for n in range(self.proxies.get(cid, 0))]
    def count_active_runs(self, cid, kind, exclude_plans=()): return self.active.get(cid, 0)
    def last_run_for(self, pid): return self.runs.get(pid)
    def update_run(self, rid, **f): self.updates.append((rid, f))


@pytest.fixture
def fdb(monkeypatch):
    d = _DB()
    monkeypatch.setattr(pol, 'db', d)
    return d


def _tick(fdb, now, pending=None):
    calls = []
    pending = {} if pending is None else pending
    pol.tick(lambda *a: calls.append(a), pending, now)
    return calls, pending


def test_nothing_starts_before_the_window_opens(fdb):
    calls, _ = _tick(fdb, datetime(2026, 10, 20, 21, 59, tzinfo=AMS))
    # the previous night's window (opened 19 Oct 22:00) is still open until the next opening
    assert {c[0] for c in calls} == {10, 11, 20}
    fdb.ran = {10, 11, 12, 20}
    assert _tick(fdb, datetime(2026, 10, 20, 21, 59, tzinfo=AMS))[0] == []


def test_runs_start_per_cluster_up_to_its_proxies(fdb):
    now = datetime(2026, 10, 20, 22, 0, tzinfo=AMS)
    calls, pending = _tick(fdb, now)
    assert [c[0] for c in calls] == [10, 11, 20], 'two proxies on c1, one slot minimum on c2'
    assert calls[0][1:] == (1, date(2026, 10, 20), 'c1', 'incremental', None)
    # next minute: the dispatched ones are still pending, nothing else fits
    calls, _ = _tick(fdb, now + timedelta(minutes=1), pending)
    assert calls == []
    # 10 finished, 11 still runs (pending), so one slot frees up for 12
    fdb.ran = {10}; pending.pop(10)
    calls, _ = _tick(fdb, now + timedelta(minutes=2), pending)
    assert [c[0] for c in calls] == [12]


def test_runs_of_other_plans_on_the_cluster_take_slots(fdb):
    fdb.active = {'c1': 1}                                   # e.g. a standalone plan backing up right now
    calls, _ = _tick(fdb, datetime(2026, 10, 20, 22, 0, tzinfo=AMS))
    assert [c[0] for c in calls] == [10, 20]


def test_longest_previous_run_goes_first_and_unknown_before_known(fdb):
    fdb.took = {10: 60.0, 11: 600.0}
    calls, _ = _tick(fdb, datetime(2026, 10, 20, 22, 0, tzinfo=AMS))
    assert [c[0] for c in calls] == [12, 20, 11], 'unknown ones (12 on c1, 20 on c2) first, then the longest known'


def test_blackout_holds_every_start(fdb):
    fdb.policies[0]['blackouts'] = [{'days': ['tue'], 'from': '21:00', 'to': '23:00'}]
    assert _tick(fdb, datetime(2026, 10, 20, 22, 30, tzinfo=AMS))[0] == []
    assert len(_tick(fdb, datetime(2026, 10, 20, 23, 0, tzinfo=AMS))[0]) == 3


def test_paused_members_and_policies_are_left_alone(fdb):
    fdb.members[0]['enabled'] = False
    fdb.members[3]['cluster_enabled'] = False
    assert [c[0] for c in _tick(fdb, datetime(2026, 10, 20, 22, 0, tzinfo=AMS))[0]] == [11, 12]
    fdb.policies[0]['enabled'] = False
    assert _tick(fdb, datetime(2026, 10, 20, 22, 0, tzinfo=AMS))[0] == []


def test_export_schedules_run_one_export_per_cluster(fdb):
    fdb.policies[0]['kind'] = 'export'
    assert [c[0] for c in _tick(fdb, datetime(2026, 10, 20, 22, 0, tzinfo=AMS))[0]] == [10, 20]


def test_a_broken_policy_does_not_stop_the_others(fdb):
    fdb.policies.insert(0, dict(fdb.policies[0], id=2, name='bad', rrule='nonsense'))
    assert len(_tick(fdb, datetime(2026, 10, 20, 22, 0, tzinfo=AMS))[0]) == 3


def _run(start, end, occ=date(2026, 10, 20), status='COMPLETE'):
    return {'id': 5, 'status': status, 'policy_id': 1, 'occurrence': occ, 'work_started_at': start, 'finished_at': end}


def test_notices_for_blackout_and_late_finish(fdb):
    p = dict(fdb.policies[0], blackouts=[{'days': ['wed'], 'from': '05:00', 'to': '07:00'}])
    ok = _run(datetime(2026, 10, 20, 22, 5, tzinfo=AMS), datetime(2026, 10, 21, 3, 0, tzinfo=AMS))
    assert pol.notices_for(p, ok) == []
    into = _run(datetime(2026, 10, 21, 4, 0, tzinfo=AMS), datetime(2026, 10, 21, 5, 30, tzinfo=AMS))
    assert pol.notices_for(p, into) == ['blackout']
    late = _run(datetime(2026, 10, 21, 4, 0, tzinfo=AMS), datetime(2026, 10, 21, 6, 10, tzinfo=AMS))
    assert pol.notices_for(p, late) == ['blackout', 'late']
    assert pol.notices_for(p, dict(late, status='ERROR')) == [], 'only finished runs get notices'


def test_run_member_records_the_notice(fdb, monkeypatch):
    seen = []
    monkeypatch.setattr(pol.jobs, 'run_plan', lambda pid, policy_id=None, occurrence=None, full=None: seen.append((pid, policy_id, occurrence)))
    fdb.runs[10] = _run(datetime(2026, 10, 21, 5, 0, tzinfo=AMS), datetime(2026, 10, 21, 6, 30, tzinfo=AMS))
    pol.run_member(10, 1, date(2026, 10, 20))
    assert seen == [(10, 1, date(2026, 10, 20))] and fdb.updates == [(5, {'notice': 'late'})]


def test_standalone_plans_keep_their_cron_job_and_members_do_not(monkeypatch):
    plans = [{'id': 1, 'enabled': True, 'run_time': '03:00', 'policy_id': None, 'cluster_timezone': 'UTC'},
             {'id': 2, 'enabled': True, 'run_time': '03:00', 'policy_id': 7, 'cluster_timezone': 'UTC'}]
    monkeypatch.setattr(sch, 'db', types.SimpleNamespace(get_schedules=lambda: plans))
    added = []
    monkeypatch.setattr(sch, '_scheduler', types.SimpleNamespace(get_jobs=lambda: [], add_job=lambda *a, **k: added.append(k['id'])))
    assert sch.sync_jobs() == 1 and added == ['export-1']


def test_policy_fields_validation():
    f = api._policy_fields({'name': ' Nightly ', 'kind': 'incremental', 'rrule': 'freq=weekly;byday=mo',
                            'blackouts': [{'days': ['Mon'], 'from': '7:00', 'to': '19:00'}], 'buffer_minutes': 9999}, True)
    assert f['name'] == 'Nightly' and f['rrule'] == 'FREQ=WEEKLY;BYDAY=MO' and f['buffer_minutes'] == 720
    assert f['window_open'] == '22:00' and f['blackouts'] == [{'days': ['mon'], 'from': '07:00', 'to': '19:00'}]
    for bad in ({'kind': 'incremental'}, {'name': 'x', 'kind': 'both'}, {'name': 'x', 'kind': 'export', 'rrule': 'FREQ=HOURLY'},
                {'name': 'x', 'kind': 'export', 'timezone': 'Mars/Base'}, {'name': 'x', 'kind': 'export', 'window_open': '25:00'}):
        with pytest.raises(ValueError):
            api._policy_fields(bad, True)
    assert 'kind' not in api._policy_fields({'kind': 'export', 'enabled': False}, False), 'kind is fixed after creation'
    f = api._policy_fields({'full_rrule': 'freq=monthly;byday=1su', 'full_kind': 'synthetic'}, False, 'incremental')
    assert f == {'full_rrule': 'FREQ=MONTHLY;BYDAY=1SU', 'full_kind': 'synthetic'}
    assert api._policy_fields({'full_rrule': ''}, False, 'incremental') == {'full_rrule': None}
    for bad, kind in (({'full_rrule': 'FREQ=MONTHLY;BYDAY=1SU'}, 'export'), ({'full_kind': 'virtual'}, 'incremental')):
        with pytest.raises(ValueError):
            api._policy_fields(bad, False, kind)


def test_run_member_keeps_a_notice_the_run_set_itself(fdb, monkeypatch):
    monkeypatch.setattr(pol.jobs, 'run_plan', lambda *a, **k: None)
    fdb.runs[10] = dict(_run(datetime(2026, 10, 21, 5, 0, tzinfo=AMS), datetime(2026, 10, 21, 6, 30, tzinfo=AMS)), notice='synthetic_failed')
    pol.run_member(10, 1, date(2026, 10, 20), 'synthetic')
    assert fdb.updates == [(5, {'notice': 'synthetic_failed,late'})]


def test_full_rule_days_are_passed_to_the_runs(fdb):
    fdb.policies[0].update(full_rrule='FREQ=MONTHLY;BYDAY=1SU', full_kind='synthetic', rrule='FREQ=DAILY')
    sunday = _tick(fdb, datetime(2026, 11, 1, 22, 0, tzinfo=AMS))[0]          # first Sunday of November
    assert {c[5] for c in sunday} == {'synthetic'}
    monday = _tick(fdb, datetime(2026, 11, 2, 22, 0, tzinfo=AMS))[0]
    assert {c[5] for c in monday} == {'none'}, 'with a full rule, other days never count up to a full'
    fdb.policies[0]['full_rrule'] = None
    assert {c[5] for c in _tick(fdb, datetime(2026, 11, 2, 22, 0, tzinfo=AMS))[0]} == {None}
