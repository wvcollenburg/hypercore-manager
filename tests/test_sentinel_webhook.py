"""sentinel/webhook.py — retry schedule and the retry query (F5)."""
import re
from contextlib import contextmanager

import pytest

import config as sentinel_config
import webhook


@pytest.mark.parametrize('attempt,max_attempts,dead', [
    (1, 3, False), (2, 3, False), (3, 3, True), (4, 3, True),
    (1, 1, True), (4, 6, False), (6, 6, True),
])
def test_max_retries_decides_when_an_event_is_dead_lettered(attempt, max_attempts, dead):
    """SENTINEL_WEBHOOK_MAX_RETRIES was never read: the schedule was fixed at three attempts."""
    assert (webhook._next_retry_at(attempt, max_attempts) is None) is dead


class _Cur:
    def __init__(self, log, results):
        self.log, self.results = log, results
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=()):
        self.log.append((sql, params))
    def fetchall(self):
        return self.results.pop(0) if self.results else []


class _Conn:
    def __init__(self, log, results): self.log, self.results = log, results
    def cursor(self, **kw): return _Cur(self.log, self.results)


def test_a_failing_endpoint_gets_its_attempts_and_then_dead_letter(monkeypatch):
    log, results = [], []

    @contextmanager
    def connect(cfg):
        yield _Conn(log, results)
    monkeypatch.setattr(webhook.db, 'connect', connect)
    monkeypatch.setattr(webhook, '_post', lambda *a, **k: (500, 'down'))
    cfg = sentinel_config.Config(cluster_id='11111111-1111-1111-1111-111111111111',
                                 webhook_url='https://hook.example/x', webhook_enabled=True, webhook_max_retries=3)
    event = {'id': 7, 'cluster_id': cfg.cluster_id, 'event_type': 'cluster_health', 'severity': 'CRITICAL',
             'emitted_at': None, 'payload_snapshot': {}}
    results[:] = [[], [dict(event, attempt_count=2)]]         # no new events; one due retry after attempt 2
    webhook.dispatch_pending(cfg)
    inserts = [p for s, p in log if 'INSERT INTO audit.webhook_deliveries' in s]
    assert len(inserts) == 1
    status, attempt, next_retry = inserts[0][3], inserts[0][4], inserts[0][5]
    assert (status, attempt, next_retry) == ('dead_letter', 3, None)


def test_the_retry_query_picks_the_latest_attempt_before_testing_it():
    """status='failed' AND next_retry_at < NOW() were applied before DISTINCT ON, so the stale attempt-1 row
    kept matching after attempt 2 failed: a retry every 60 s for 30 minutes instead of three attempts."""
    src = open(webhook.__file__).read()
    q = src[src.index('retry queue'):src.index('retry_events = ')]
    inner = q[q.index('FROM ('):q.index(') last')]
    assert 'DISTINCT ON' in inner and 'failed' not in inner and 'next_retry_at <' not in inner
    outer = q[q.index(') last'):]
    assert re.search(r"last\.status\s*=\s*'failed'", outer) and re.search(r'last\.next_retry_at\s*<\s*NOW\(\)', outer)
