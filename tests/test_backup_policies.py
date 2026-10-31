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
OCC = datetime(2026, 10, 20, 22, 0, tzinfo=AMS)       # the occurrence that opens on 20 Oct at 22:00


class _DB:
    def __init__(self):
        self.policies = [{'id': 1, 'name': 'Nightly', 'kind': 'incremental', 'enabled': True, 'folder_id': 'f1',
                          'rrule': 'FREQ=DAILY', 'starts_on': date(2026, 10, 1), 'window_open': '22:00',
                          'preferred_finish': '06:00', 'buffer_minutes': 60, 'blackouts': [], 'timezone': 'Europe/Amsterdam'}]
        self.members = [self._m(10, 'c1', 'a'), self._m(11, 'c1', 'b'), self._m(12, 'c1', 'c'), self._m(20, 'c2', 'd')]
        self.ran = set(); self.active = {}; self.proxies = {'c1': 2, 'c2': 0}; self.took = {}; self.released = []
        self.runs = {}; self.updates = []; self.history = {}; self.nights = {}; self.saved = []

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
    def run_history(self, ids, days=90, per_plan=30): return {i: self.history.get(i, []) for i in ids}
    def cluster_run_history(self, cid, limit=60): return [r for i, rs in self.history.items() for r in rs if self._cluster(i) == cid]
    def _cluster(self, pid): return next(m['cluster_id'] for m in self.members if m['id'] == pid)
    def get_night(self, pid, occ): return dict(self.nights.get((pid, occ), {}))
    def save_night(self, pid, occ, items):
        self.saved.append((pid, occ)); self.nights[(pid, occ)] = {i['plan_id']: dict(i) for i in items}
    def update_run(self, rid, **f): self.updates.append((rid, f))
    def set_manual(self, pid, at): self.get_policy(pid)['manual_at'] = at


@pytest.fixture
def fdb(monkeypatch):
    d = _DB()
    monkeypatch.setattr(pol, 'db', d)
    return d


def _tick(fdb, now, pending=None):
    calls = []
    pending = {} if pending is None else pending
    pol.tick(lambda *a, **k: calls.append(a + ((k,) if k else ())), pending, now)
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
    assert calls[0][1:] == (1, OCC, 'c1', 'incremental', None)
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


def _history(n, mib=600, copy=60.0, overhead=30.0, start=datetime(2026, 10, 1, 22, 0, tzinfo=AMS)):
    return [{'status': 'COMPLETE', 'mode': 'incremental', 'point_kind': 'delta', 'blocks_changed': mib, 'copy_seconds': copy,
             'overhead_seconds': overhead, 'work_started_at': start - timedelta(days=d)} for d in range(n)]


def test_learned_members_wait_for_their_planned_start_and_learning_ones_go_first(fdb):
    """10 and 11 are learned (10 MiB/s, 600 MiB deltas: 90 s each); 12 is still learning. The night is
    planned once: 12 starts at the opening, 10 and 11 are placed to end at 06:00 - 60 min = 05:00."""
    fdb.history = {10: _history(6), 11: _history(6)}
    fdb.members = fdb.members[:3]
    opens = datetime(2026, 10, 20, 22, 0, tzinfo=AMS)
    calls, pending = _tick(fdb, opens)
    assert [c[0] for c in calls] == [12] and fdb.saved == [(1, OCC)]
    night = fdb.nights[(1, OCC)]
    assert night[12]['learned'] is False and night[12]['planned_start'] == opens
    ends = sorted(night[i]['predicted_finish'] for i in (10, 11))
    assert ends[-1] == datetime(2026, 10, 21, 5, 0, tzinfo=AMS), ends
    assert _tick(fdb, datetime(2026, 10, 21, 3, 0, tzinfo=AMS), pending)[0] == []
    first = min(night[10]['planned_start'], night[11]['planned_start'])
    calls, _ = _tick(fdb, first, pending)
    assert len(calls) >= 1 and fdb.saved == [(1, OCC)], 'planned once per night'


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


def _run(start, end, occ=OCC, status='COMPLETE'):
    return {'id': 5, 'status': status, 'policy_id': 1, 'occurrence': occ, 'work_started_at': start, 'finished_at': end}


def test_notices_for_blackout_and_late_finish(fdb):
    p = dict(fdb.policies[0], blackouts=[{'days': ['wed'], 'from': '05:00', 'to': '07:00'}], buffer_minutes=0)
    ok = _run(datetime(2026, 10, 20, 22, 5, tzinfo=AMS), datetime(2026, 10, 21, 3, 0, tzinfo=AMS))
    assert pol.notices_for(p, ok) == []
    into = _run(datetime(2026, 10, 21, 4, 0, tzinfo=AMS), datetime(2026, 10, 21, 5, 30, tzinfo=AMS))
    assert pol.notices_for(p, into) == ['blackout']
    late = _run(datetime(2026, 10, 21, 4, 0, tzinfo=AMS), datetime(2026, 10, 21, 6, 10, tzinfo=AMS))
    assert pol.notices_for(p, late) == ['blackout', 'late']
    assert pol.notices_for(p, dict(late, status='ERROR')) == [], 'only finished runs get notices'


def test_finishing_inside_the_buffer_is_a_notice_of_its_own(fdb):
    p = dict(fdb.policies[0], buffer_minutes=60)
    assert pol.notices_for(p, _run(datetime(2026, 10, 21, 4, 0, tzinfo=AMS), datetime(2026, 10, 21, 4, 59, tzinfo=AMS))) == []
    assert pol.notices_for(p, _run(datetime(2026, 10, 21, 4, 0, tzinfo=AMS), datetime(2026, 10, 21, 5, 30, tzinfo=AMS))) == ['buffer']
    assert pol.notices_for(p, _run(datetime(2026, 10, 21, 4, 0, tzinfo=AMS), datetime(2026, 10, 21, 6, 30, tzinfo=AMS))) == ['late']


def test_run_member_records_the_notice(fdb, monkeypatch):
    seen = []
    monkeypatch.setattr(pol.jobs, 'run_plan', lambda pid, policy_id=None, occurrence=None, full=None: seen.append((pid, policy_id, occurrence)))
    fdb.runs[10] = _run(datetime(2026, 10, 21, 5, 0, tzinfo=AMS), datetime(2026, 10, 21, 6, 30, tzinfo=AMS))
    pol.run_member(10, 1, OCC)
    assert seen == [(10, 1, OCC)] and fdb.updates == [(5, {'notice': 'late'})]


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
    for bad in ({'kind': 'incremental'}, {'name': 'x', 'kind': 'both'}, {'name': 'x', 'kind': 'export', 'rrule': 'FREQ=MINUTELY'},
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
    pol.run_member(10, 1, OCC, 'synthetic')
    assert fdb.updates == [(5, {'notice': 'synthetic_failed,late'})]


def test_full_rule_days_are_passed_to_the_runs(fdb):
    fdb.policies[0].update(full_rrule='FREQ=MONTHLY;BYDAY=1SU', full_kind='synthetic', rrule='FREQ=DAILY')
    sunday = _tick(fdb, datetime(2026, 11, 1, 22, 0, tzinfo=AMS))[0]          # first Sunday of November
    assert {c[5] for c in sunday} == {'synthetic'}
    monday = _tick(fdb, datetime(2026, 11, 2, 22, 0, tzinfo=AMS))[0]
    assert {c[5] for c in monday} == {'none'}, 'with a full rule, other days never count up to a full'
    fdb.policies[0]['full_rrule'] = None
    assert {c[5] for c in _tick(fdb, datetime(2026, 11, 2, 22, 0, tzinfo=AMS))[0]} == {None}


def test_scheduler_start_survives_a_database_without_the_new_tables(monkeypatch):
    """On an upgrade the dashboard applies the migrations while the backup service starts: a
    failing first sync must not take the service down; the periodic sync catches up."""
    added = []
    monkeypatch.setattr(sch._scheduler, 'start', lambda: None)
    monkeypatch.setattr(sch._scheduler, 'add_job', lambda *a, **kw: added.append(kw.get('id')))
    def boom():
        raise RuntimeError('relation "backup.policies" does not exist')
    monkeypatch.setattr(sch, 'sync_jobs', boom)
    sch.start(60)
    assert 'sync' in added and 'reconcile-start' in added


# ------------------------------------------------------- hourly and run now
def test_an_hourly_schedule_starts_every_member_at_once_in_each_occurrence(fdb):
    fdb.policies[0].update(rrule='FREQ=HOURLY;INTERVAL=3')
    fdb.history = {i: [{'status': 'COMPLETE', 'mode': 'incremental', 'point_kind': 'delta', 'blocks_changed': 600, 'copy_seconds': 6,
                        'overhead_seconds': 30, 'work_started_at': OCC} for _ in range(6)] for i in (10, 11, 12)}
    calls, _ = _tick(fdb, datetime(2026, 10, 20, 15, 1, tzinfo=AMS))
    assert sorted(c[0] for c in calls) == [10, 11, 20], 'learned or not, members start as soon as a proxy is free'
    assert {c[2] for c in calls} == {datetime(2026, 10, 20, 15, 0, tzinfo=AMS)}, 'the occurrence is the hour it opened'
    late = _run(datetime(2026, 10, 20, 15, 5, tzinfo=AMS), datetime(2026, 10, 20, 18, 2, tzinfo=AMS), occ=datetime(2026, 10, 20, 15, 0, tzinfo=AMS))
    assert pol.notices_for(fdb.policies[0], late) == ['late'], 'ran into the next occurrence'
    ok = dict(late, finished_at=datetime(2026, 10, 20, 17, 55, tzinfo=AMS))
    assert pol.notices_for(fdb.policies[0], ok) == [], 'no buffer in an hourly schedule'


def test_run_now_ignores_window_and_blackouts_and_closes_when_everyone_ran(fdb):
    at = datetime(2026, 10, 20, 12, 0, tzinfo=AMS)                      # far outside the 22:00 window
    fdb.policies[0].update(manual_at=at, enabled=False, full_rrule='FREQ=WEEKLY;BYDAY=TU',
                           blackouts=[{'days': ['tue'], 'from': '08:00', 'to': '18:00'}])
    calls, pending = _tick(fdb, at + timedelta(minutes=1))
    assert [c[:6] for c in calls] == [(10, 1, at, 'c1', 'incremental', 'none'), (11, 1, at, 'c1', 'incremental', 'none'),
                                     (20, 1, at, 'c2', 'incremental', 'none')]
    assert all(c[6] == {'manual': True} for c in calls), 'a paused schedule still runs on request; a full day still gives no full'
    fdb.ran = {10, 11, 20}
    calls, _ = _tick(fdb, at + timedelta(minutes=30))
    assert [c[0] for c in calls] == [12], 'the next member once a proxy is free'
    fdb.ran = {10, 11, 12, 20}
    assert _tick(fdb, at + timedelta(minutes=60))[0] == [] and fdb.policies[0]['manual_at'] is None


def test_a_manual_run_gets_no_window_notices(fdb, monkeypatch):
    monkeypatch.setattr(pol.jobs, 'run_plan', lambda *a, **k: None)
    fdb.runs[10] = _run(datetime(2026, 10, 21, 5, 0, tzinfo=AMS), datetime(2026, 10, 21, 6, 30, tzinfo=AMS))
    pol.run_member(10, 1, OCC, None, manual=True)
    assert fdb.updates == []


def test_proxy_logs_route_fetches_the_bundle_with_the_decrypted_token(monkeypatch):
    """The stored token is encrypted: the route must ask for it decrypted, or the proxy answers 401."""
    asked = {}
    def get_proxy(pid, with_secret=False):
        asked['with_secret'] = with_secret
        return {'id': pid, 'url': 'http://px:8083', 'token': 'plain' if with_secret else None, 'token_enc': 'x'}
    monkeypatch.setattr(api, 'db', types.SimpleNamespace(get_proxy=get_proxy))
    used = {}
    class FakeClient:
        def __init__(self, proxy): used['token'] = proxy.get('token')
        def support_bundle(self): return b'PK\x03\x04zip', 'hcm-proxy-px-1.zip'
    monkeypatch.setattr(api.incremental, 'proxy_client_for', FakeClient)
    monkeypatch.setitem(api.app.config, 'API_TOKEN', '')
    r = api.app.test_client().get('/api/v1/proxies/3/logs')
    assert r.status_code == 200 and r.data.startswith(b'PK') and r.headers['Content-Disposition'] == 'attachment; filename="hcm-proxy-px-1.zip"'
    assert asked == {'with_secret': True} and used == {'token': 'plain'}


def test_flr_mount_passes_keys_and_answers_locked_volumes_with_422(monkeypatch):
    seen = {}
    def mount_point(pid, by, keys=None):
        seen['keys'] = keys
        raise api.ProxyError('proxy returned 422: locked', {'error': 'encrypted volume(s) need a key: disk1-p2 (BitLocker)',
                                                            'locked': [{'name': 'disk1-p2', 'kind': 'BitLocker', 'reason': 'key needed'}]})
    monkeypatch.setattr(api.flr, 'mount_point', mount_point)
    monkeypatch.setitem(api.app.config, 'API_TOKEN', '')
    c = api.app.test_client()
    r = c.post('/api/v1/flr/mounts', json={'point_id': 4, 'keys': ['k1']})
    assert r.status_code == 422 and r.get_json()['locked'][0]['kind'] == 'BitLocker' and seen['keys'] == ['k1']
    assert c.post('/api/v1/flr/mounts', json={'point_id': 4, 'keys': 'k1'}).status_code == 400
