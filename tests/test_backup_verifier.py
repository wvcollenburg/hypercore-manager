"""backup/verifier.py: planned verification per folder (plans/PLAN-chain-verify.md), with a fake database and
proxies. Which proxy verifies which point, when production proxies may work, DR proxies around the clock,
the second opinion before a failure counts, and the overdue alerts."""
import importlib
import os
import sys
import threading
from datetime import datetime, timedelta, timezone

import pytest

_BACKUP = os.path.join(os.path.dirname(__file__), '..', 'backup')
_CLASH = ('config', 'db', 'log', 'api', 'hypercore', 'jobs', 'scheduler', 'incremental', 'proxy_client', 'manifest_names',
          'recurrence', 'verifier', 'policies', 'planner', 'predict')


def _import():
    saved = {n: sys.modules.pop(n) for n in _CLASH if n in sys.modules}
    sys.path.insert(0, _BACKUP)
    try:
        ver = importlib.import_module('verifier')
        pcm = importlib.import_module('proxy_client')
    finally:
        sys.path.remove(_BACKUP)
        for n in _CLASH:
            sys.modules.pop(n, None)
        sys.modules.update(saved)
    return ver, pcm


ver, pcm = _import()
UTC = timezone.utc
# Saturday 2026-11-21 02:00 UTC = 03:00 in Amsterdam
NIGHT = datetime(2026, 11, 21, 2, 0, tzinfo=UTC)
DAY = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


class FakeDB:
    def __init__(self):
        self.settings = {'interval_days': 7, 'hours_from': '01:00', 'hours_until': '06:00', 'dr_during_backups': False}
        self.clusters = [{'cluster_id': 'prod', 'cluster_name': 'Prod', 'role': 'production', 'timezone': 'Europe/Amsterdam'}]
        self.proxies = {'prod': [{'id': 1, 'name': 'px-prod', 'cluster_id': 'prod', 'vm_uuid': 'v1'}]}
        self.points = []
        self.active_runs = 0
        self.policies = []
        self.members = {}
        self.run_plans = set()
        self.rates = {}
        self.runs = []
        self.alerts = []
        self.resolved = []
        self.overdue = []
        self.open_overdue = set()

    def verify_folders(self): return ['F1']
    def verify_settings(self, f): return dict(self.settings)
    def folder_clusters(self, f): return self.clusters
    def get_proxies(self, cid, enabled_only=False): return self.proxies.get(cid, [])
    def get_proxy(self, pid, with_secret=False):
        return next(dict(p, url='http://px', token='t') for ps in self.proxies.values() for p in ps if p['id'] == pid)
    def verify_candidates(self, f, older_than, limit=50):
        rows = [p for p in self.points if p['verify_checked_at'] is None or p['verify_checked_at'] < older_than]
        return sorted(rows, key=lambda p: (p.get('verify_error') is not None, p['verify_checked_at'] or datetime.min.replace(tzinfo=UTC)))
    def active_runs_on(self, cids): return self.active_runs
    def get_policies(self, f): return self.policies
    def policy_members(self, pid): return self.members.get(pid, [])
    def plans_run_in_occurrence(self, pid, occ): return set()
    def standalone_plans_on(self, cids): return []
    def plan_run_active(self, plan_id): return plan_id in self.run_plans
    def proxy_read_rate(self, pid): return self.rates.get(pid)
    def add_verify_run(self, **f): self.runs.append(dict(f, status='running')); return len(self.runs)
    def finish_verify_run(self, rid, status, b=None, d=None, msg=None): self.runs[rid - 1].update(status=status, message=msg, bytes=b)
    def get_share(self, sid, with_secret=False): return {'uri_base': 'smb://nas/s', 'username': 'u', 'password': 'p'}
    def get_point(self, pid): return next((p for p in self.points if p['point_id'] == pid), None)
    def raise_verify_alert(self, kind, chain, pt, detail): self.alerts.append((kind, chain['id'], detail)); return True
    def resolve_verify_alerts(self, **kw): self.resolved.append(kw); return 1
    def resolve_alerts_of_gone_points(self): return 0
    def overdue_chains(self, f, older_than): return self.overdue
    def open_overdue_chain_ids(self, f): return set(self.open_overdue)
    def interrupted_verify_runs(self): return 0


def point(pid, seq=1, chain=10, checked=None, size=1 << 30, error=None, plan=5, created=None):
    return {'point_id': pid, 'seq': seq, 'dir': f'delta-{seq:04d}', 'chain_id': chain, 'plan_id': plan, 'vm_name': f'vm{chain}',
            'cluster_id': 'prod', 'target_id': 9, 'chain_dir': f'F/vm{chain}.chain', 'bytes_written': size, 'status': 'ok',
            'verify_checked_at': checked, 'verify_error': error, 'created_at': created or (NIGHT - timedelta(days=1))}


class FakeInc:
    def __init__(self):
        self.locks = {}
        self.recorded = []

    def proxy_lock(self, pid): return self.locks.setdefault(pid, threading.Lock())
    def proxy_client_for(self, p): return self.pc
    def ensure_reachable(self, p): return p
    def record_verify_result(self, chain_id, res):
        self.recorded.append((chain_id, res))
        failed = {d: '; '.join(r.get('mismatches') or ['x']) for d, r in (res.get('points') or {}).items() if not r.get('ok')}
        return (not failed, failed)


class FakePC:
    def __init__(self, results):
        self.results = list(results)
        self.jobs = []

    def submit(self, kind, body): self.jobs.append((kind, body)); return f'j{len(self.jobs)}'
    def cancel(self, jid): pass
    def wait(self, jid, on_progress=None, timeout=0):
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return {'state': 'done', 'result': r}


@pytest.fixture
def world(monkeypatch):
    db, inc = FakeDB(), FakeInc()
    monkeypatch.setattr(ver, 'db', db)
    monkeypatch.setattr(ver, 'incremental', inc)
    ver._running.clear()
    started = []

    def start(proxy, pt, st, folder, deadline, lock):
        started.append((proxy['id'], pt['point_id'], deadline))
        lock.release()                                  # the test does not run the job
        with ver._running_guard:
            ver._running.pop(proxy['id'], None)
    return db, inc, started, start


# ------------------------------------------------------------------ hours
def test_hours_left_is_local_to_the_cluster_and_may_cross_midnight():
    assert ver.hours_left(NIGHT, 'Europe/Amsterdam', '01:00', '06:00') == 3 * 3600          # 03:00 local
    assert ver.hours_left(NIGHT, 'UTC', '03:00', '06:00') is None                          # 02:00 UTC
    assert ver.hours_left(NIGHT, 'Europe/Amsterdam', '22:00', '04:00') == 3600             # 03:00, until 04:00
    assert ver.hours_left(datetime(2026, 11, 21, 22, 30, tzinfo=UTC), 'UTC', '22:00', '04:00') == 5.5 * 3600
    assert ver.hours_left(DAY, 'UTC', '00:00', '00:00') == 86400                           # equal: all day
    assert ver.hours_left(DAY, 'Europe/Amsterdam', '01:00', '06:00') is None


# ------------------------------------------------------------------ production proxies
def test_a_production_proxy_verifies_inside_its_hours_never_checked_points_first(world):
    db, inc, started, start = world
    db.points = [point(1, checked=NIGHT - timedelta(days=9)), point(2, chain=11, checked=None)]
    assert ver.tick(NIGHT, start) == 1
    assert started == [(1, 2, 3 * 3600)], 'the never-checked point, with the end of the hours as deadline'


def test_outside_the_hours_or_during_backups_a_production_proxy_waits(world):
    db, inc, started, start = world
    db.points = [point(1)]
    assert ver.tick(DAY, start) == 0
    db.active_runs = 1
    assert ver.tick(NIGHT, start) == 0
    db.active_runs = 0
    db.run_plans = {5}                                  # a backup of this VM: retention may prune it
    assert ver.tick(NIGHT, start) == 0
    assert started == []


def test_a_point_that_cannot_finish_before_the_hours_end_is_left_for_a_smaller_one(world):
    db, inc, started, start = world
    db.rates[1] = 100 * 1024 * 1024                     # 100 MiB/s measured
    db.points = [point(1, size=2 << 40), point(2, chain=11, size=10 << 30)]   # 2 TiB: ~7 h; 10 GiB: ~2 min
    ver.tick(NIGHT, start)
    assert started == [(1, 2, 3 * 3600)]


def test_points_checked_within_the_interval_are_not_due(world):
    db, inc, started, start = world
    db.points = [point(1, checked=NIGHT - timedelta(days=3))]
    assert ver.tick(NIGHT, start) == 0
    db.settings['interval_days'] = 0                    # off
    db.points = [point(1)]
    assert ver.tick(NIGHT, start) == 0


def test_a_busy_proxy_gets_nothing_and_one_chain_is_verified_once_at_a_time(world):
    db, inc, started, start = world
    db.proxies['prod'].append({'id': 2, 'name': 'px2', 'cluster_id': 'prod', 'vm_uuid': 'v2'})
    db.points = [point(1, seq=1), point(2, seq=2)]      # same chain
    inc.proxy_lock(2).acquire()                         # px2 runs a backup
    ver.tick(NIGHT, start)
    assert started == [(1, 1, 3 * 3600)]
    inc.proxy_lock(2).release()
    started.clear()

    def keep(proxy, pt, st, folder, deadline, lock):
        started.append((proxy['id'], pt['point_id']))  # stays running
    ver.tick(NIGHT, keep)
    assert started == [(1, 1)], 'the second point of the same chain waits; px2 stays idle'
    assert inc.proxy_lock(1).locked(), 'the verify holds the proxy lock: no backup on it meanwhile'


# ------------------------------------------------------------------ DR proxies
def _with_dr(db):
    db.clusters.append({'cluster_id': 'dr', 'cluster_name': 'DR', 'role': 'dr', 'timezone': 'UTC'})
    db.proxies['dr'] = [{'id': 9, 'name': 'px-dr', 'cluster_id': 'dr', 'vm_uuid': 'v9'}]


def test_a_dr_proxy_verifies_around_the_clock_and_continues_with_anything_older_than_a_day(world):
    db, inc, started, start = world
    _with_dr(db)
    db.points = [point(1, checked=DAY - timedelta(days=2))]   # not due (interval 7) but older than a day
    assert ver.tick(DAY, start) == 1
    assert started == [(9, 1, None)], 'the DR proxy, in the middle of the day, without a deadline'
    started.clear()
    db.points = [point(1, checked=DAY - timedelta(hours=3))]
    assert ver.tick(DAY, start) == 0, 'checked today: nothing to do, not even for DR'


def test_dr_proxies_go_first_and_pause_while_backups_write_the_share(world):
    db, inc, started, start = world
    _with_dr(db)
    db.points = [point(1), point(2, chain=11)]
    ver.tick(NIGHT, start)
    assert started[0][0] == 9 and {s[1] for s in started} == {1, 2}
    started.clear()
    db.active_runs = 1
    assert ver.tick(NIGHT, start) == 0, 'backups running: DR pauses, production waits'
    db.settings['dr_during_backups'] = True
    assert ver.tick(NIGHT, start) == 1 and started[0][0] == 9


def test_backup_work_coming_up_keeps_a_production_proxy_free(world, monkeypatch):
    db, inc, started, start = world
    db.policies = [{'id': 1, 'name': 'Nightly', 'kind': 'incremental', 'enabled': True, 'manual_at': None}]
    db.members[1] = [{'id': 5, 'cluster_id': 'prod', 'enabled': True, 'cluster_enabled': True}]
    monkeypatch.setattr(ver.recurrence, 'current_window', lambda pol, now: None)
    monkeypatch.setattr(ver.recurrence, 'occurrences', lambda pol, now, n: [now + timedelta(minutes=20)])
    db.rates[1] = 10 * 1024 * 1024                      # 10 MiB/s: a 1 GiB point takes ~2 min
    db.points = [point(1, size=1 << 30), point(2, chain=11, size=50 << 30)]   # 50 GiB: ~1.8 h, past the window opening
    ver.tick(NIGHT, start)
    assert started == [(1, 1, 3 * 3600)]
    assert ver.seconds_to_backups(['prod'], 'F1', NIGHT, 3 * 3600) == 20 * 60
    assert ver.backup_busy(['prod'], 'F1', NIGHT) is None, 'not busy yet: only small points fit'


def test_a_backup_or_restore_makes_the_verification_on_its_proxy_step_aside(world):
    db, inc, _, _ = world
    pt = point(1)
    db.points = [pt]
    cancelled = []

    class SlowPC(FakePC):
        def wait(self, jid, on_progress=None, timeout=0):
            ver.make_way(1, 'a restore')                 # a restore arrives while the proxy reads the point
            assert cancelled == [jid], 'the proxy job is cancelled at once'
            raise pcm.ProxyError(f'proxy job {jid} cancelled')

        def cancel(self, jid):
            cancelled.append(jid)
    inc.pc = SlowPC([])
    lock = inc.proxy_lock(1); lock.acquire()
    ver._running[1] = {'chain_id': 10, 'point_id': 1}
    assert ver.run_point({'id': 1, 'name': 'px'}, pt, 'F1', None, lock) == 'stopped'
    assert 'made way for a restore' in db.runs[-1]['message'] and inc.recorded == [] and not lock.locked()
    assert ver.make_way(1) is False, 'nothing runs on the proxy any more'


# ------------------------------------------------------------------ one verification
def _run(db, inc, results, pt=None):
    pt = pt or point(1)
    db.points = [pt]
    inc.pc = FakePC(results)
    lock = inc.proxy_lock(1)
    lock.acquire()
    ver._running[1] = {'chain_id': pt['chain_id'], 'point_id': pt['point_id']}
    st = ver.run_point({'id': 1, 'name': 'px', 'role': 'production'}, pt, 'F1', 3600, lock)
    assert not lock.locked() and 1 not in ver._running
    return st


OK = {'ok': True, 'points': {'delta-0001': {'ok': True}}, 'bytes': 1 << 30}
BAD = {'ok': False, 'points': {'delta-0001': {'ok': False, 'mismatches': ['disk.qcow2: checksum differs']}}, 'bytes': 1 << 30}


def test_one_point_is_verified_with_seq_and_recorded(world):
    db, inc, _, _ = world
    assert _run(db, inc, [OK]) == 'ok'
    assert inc.pc.jobs == [('verify', {'target': {'uri': 'smb://nas/s', 'username': 'u', 'password': 'p'},
                                       'chain_dir': 'F/vm10.chain', 'seq': 1})]
    assert inc.recorded == [(10, OK)] and db.runs[0]['status'] == 'ok'


def test_a_failure_is_verified_a_second_time_before_it_counts(world):
    db, inc, _, _ = world
    assert _run(db, inc, [BAD, OK]) == 'ok', 'a hiccup: the second read passes, nothing is recorded as corrupt'
    assert len(inc.pc.jobs) == 2 and inc.recorded == [(10, OK)]
    inc.recorded.clear()
    assert _run(db, inc, [BAD, BAD]) == 'failed'
    assert inc.recorded == [(10, BAD)] and 'checksum differs' in db.runs[-1]['message']


def test_a_failure_while_a_backup_of_the_vm_runs_is_not_recorded(world):
    db, inc, _, _ = world
    db.run_plans = {5}
    assert _run(db, inc, [BAD]) == 'stopped'
    assert inc.recorded == [] and len(inc.pc.jobs) == 1


def test_a_point_pruned_meanwhile_is_not_recorded(world):
    db, inc, _, _ = world
    pt = point(1)
    db.points = [pt]
    pc = FakePC([OK])
    orig = pc.wait
    pc.wait = lambda jid, **kw: (pt.update(status='pruned'), orig(jid, **kw))[1]
    inc.proxy_client_for = lambda p: pc
    lock = inc.proxy_lock(1); lock.acquire()
    assert ver.run_point({'id': 1, 'name': 'px'}, pt, 'F1', 3600, lock) == 'stopped'
    assert inc.recorded == []


def test_the_end_of_the_hours_stops_the_check_without_a_verdict(world):
    db, inc, _, _ = world
    assert _run(db, inc, [pcm.ProxyError('proxy job j1 did not finish within 1.0h')]) == 'stopped'
    assert inc.recorded == [] and 'next window' in db.runs[-1]['message']
    assert _run(db, inc, [pcm.ProxyError('cannot mount the share')]) == 'error'


# ------------------------------------------------------------------ overdue
def test_overdue_chains_raise_one_alert_and_close_it_when_caught_up(world):
    db, inc, _, _ = world
    db.overdue = [{'id': 10, 'vm_name': 'vm10', 'cluster_id': 'prod', 'points': 3, 'since': NIGHT - timedelta(days=20)}]
    assert ver.check_overdue(NIGHT) == {'raised': 1, 'resolved': 0}
    assert db.alerts[0][0] == 'overdue' and '3 restore point(s)' in db.alerts[0][2]
    db.open_overdue = {10}
    assert ver.check_overdue(NIGHT)['raised'] == 0, 'one open alert per chain'
    db.overdue = []
    assert ver.check_overdue(NIGHT)['resolved'] == 1 and db.resolved[-1] == {'chain_id': 10, 'kind': 'overdue'}
