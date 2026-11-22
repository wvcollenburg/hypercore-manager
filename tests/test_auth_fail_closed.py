"""
Authentication must not switch off because the database could not be asked.

Before 1.8.1 `user_db.has_any_users()` answered False on any database error and the dashboard cached
that as "no accounts, no login needed" for the life of the worker: a dashboard that started before
Postgres accepted connections served every anonymous request as root admin.
"""
from __future__ import annotations

import logging
import re
import sys
import threading
import time
import types

for _name in ('pyotp', 'qrcode', 'qrcode.image', 'qrcode.image.pil'):   # user_db imports them; the gate does not use them
    sys.modules.setdefault(_name, types.ModuleType(_name))

import user_db                                          # noqa: E402


def test_an_unreachable_database_is_not_no_users(monkeypatch):
    def boom():
        raise RuntimeError('connection refused')
    monkeypatch.setattr(user_db, '_connect', boom)
    assert user_db.has_any_users() is None


class _Cur:
    def __init__(self, row): self.row = row
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, *a): pass
    def fetchone(self): return self.row


class _Conn:
    def __init__(self, row): self.row = row
    def cursor(self): return _Cur(self.row)
    def close(self): pass


def test_an_answering_database_is_believed(monkeypatch):
    monkeypatch.setattr(user_db, '_connect', lambda: _Conn({'?column?': 1}))
    assert user_db.has_any_users() is True
    monkeypatch.setattr(user_db, '_connect', lambda: _Conn(None))
    assert user_db.has_any_users() is False


def _gate(repo, answers):
    """The start-up block of app.py that decides whether logins are required, run against a fake user store
    that gives `answers` in order (None = database unreachable)."""
    src = (repo / 'dashboard' / 'app.py').read_text()
    m = re.search(r'^_AUTH_ENABLED = True\n.*?^_refresh_auth_enabled\(\)\n', src, re.S | re.M)
    assert m, 'the auth gate block moved: update this test'
    calls = {'bootstrap': 0}
    seq = iter(answers)
    store = types.SimpleNamespace(
        has_any_users=lambda: next(seq),
        ensure_table=lambda: calls.__setitem__('bootstrap', calls['bootstrap'] + 1),
        seed_admin_from_env=lambda: None)
    ns = {'threading': threading, 'time': time, 'user_db': store,
          'app': types.SimpleNamespace(logger=logging.getLogger('test'))}
    exec(m.group(0), ns)                                   # noqa: S102 — our own source
    return ns, calls


def test_logins_stay_required_while_the_database_is_down(repo):
    ns, calls = _gate(repo, [None, None, True])
    assert ns['_AUTH_ENABLED'] is True and ns['_auth_known'] is False
    ns['_refresh_auth_enabled'](min_interval=0)
    assert ns['_AUTH_ENABLED'] is True and ns['_auth_known'] is False
    ns['_refresh_auth_enabled'](min_interval=0)
    assert ns['_AUTH_ENABLED'] is True and ns['_auth_known'] is True
    assert calls['bootstrap'] == 2, 'the skipped start-up bootstrap is repeated before the question'


def test_a_database_that_comes_back_empty_is_bootstrapped_before_auth_goes_off(repo):
    # a first start that raced Postgres: the admin from .env was never seeded, so "no rows" would be wrong
    ns, calls = _gate(repo, [None, False])
    ns['_refresh_auth_enabled'](min_interval=0)
    assert calls['bootstrap'] == 1
    assert ns['_AUTH_ENABLED'] is False and ns['_auth_known'] is True   # truly no accounts: first-run mode


def test_the_question_is_not_repeated_on_every_request(repo):
    ns, _ = _gate(repo, [None, True])
    ns['_refresh_auth_enabled']()                           # default interval: too soon, not asked again
    assert ns['_auth_known'] is False


def test_a_known_answer_is_final(repo):
    ns, _ = _gate(repo, [True])
    assert ns['_auth_known'] is True
    ns['_refresh_auth_enabled'](min_interval=0)             # would raise StopIteration if it asked again
    assert ns['_AUTH_ENABLED'] is True
