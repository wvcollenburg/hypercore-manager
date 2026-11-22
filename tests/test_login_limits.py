"""Sign-in attempts are limited (1.8.1).

Before, the password step and the two-factor step accepted unlimited guesses: after one correct password,
session['otp_pending'] took every 6-digit code an attacker sent, and a code stayed valid for its whole
window, also a second time. The counts now live in the database (four gunicorn workers, and a session
cookie the client can replay) and a TOTP code is accepted once.
"""
from __future__ import annotations

import time
import types

import pytest

import login_texts
import user_db


class FakeFailures:
    """config.login_failures and the users the login route reads, in memory."""
    def __init__(self):
        self.rows = []                       # (at, login, client, step)
        self.counters = {}
        self.users = {'anna@x.example': {'user_id': 'u1', 'username': 'anna@x.example', 'role': 'user',
                                         'enabled': True, 'otp_enabled': True, 'otp_secret': 'S'}}

    def install(self, monkeypatch, dash, code_ok):
        db = self
        key = user_db._login_key
        monkeypatch.setattr(user_db, 'record_login_failure', lambda login, client, step: db.rows.append((time.time(), key(login), client, step)))
        monkeypatch.setattr(user_db, 'clear_login_failures', lambda login: setattr(db, 'rows', [r for r in db.rows if r[1] != key(login)]))

        def locked(login, client):
            recent = [r for r in db.rows if r[0] > time.time() - 900]
            return (sum(r[1] == key(login) for r in recent) >= user_db.MAX_FAILURES_PER_LOGIN
                    or sum(r[2] == client for r in recent) >= user_db.MAX_FAILURES_PER_CLIENT)
        monkeypatch.setattr(user_db, 'login_locked', locked)
        monkeypatch.setattr(user_db, 'otp_failures_since',
                            lambda login, since: sum(1 for r in db.rows if r[1] == key(login) and r[3] == 'otp' and r[0] > since))
        monkeypatch.setattr(user_db, 'get_by_login', lambda login: db.users.get((login or '').strip().lower()))
        monkeypatch.setattr(user_db, 'get_by_username', lambda name: db.users.get(name))
        monkeypatch.setattr(user_db, 'verify_password', lambda name, pw: name in db.users and pw == 'right-password')
        monkeypatch.setattr(user_db, 'verify_otp_code', lambda name, code: code_ok(code))


@pytest.fixture
def login(dash, monkeypatch):
    db = FakeFailures()
    db.install(monkeypatch, dash, lambda code: code == '123456')
    return dash.app.test_client(), db


def _password(client, user='anna@x.example', pw='right-password', addr='10.0.0.1'):
    return client.post('/login', data={'username': user, 'password': pw}, environ_base={'REMOTE_ADDR': addr})


def _otp(client, code, addr='10.0.0.1'):
    return client.post('/login?step=otp', data={'otp': code}, environ_base={'REMOTE_ADDR': addr})


def test_every_new_text_exists_in_every_language():
    for lang, texts in login_texts.TEXTS.items():
        assert set(texts) == set(login_texts.TEXTS['en']), lang


def test_the_password_step_locks_after_ten_failures_also_for_an_unknown_address(login):
    client, db = login
    for _ in range(10):
        assert b'Invalid email address or password' in _password(client, pw='guess').data
    r = _password(client)                                        # even the right password: locked for now
    assert b'Too many failed sign-in attempts' in r.data and r.status_code == 200
    for _ in range(10):
        _password(client, user='nobody@x.example', pw='guess', addr='10.0.0.2')
    assert b'Too many failed sign-in attempts' in _password(client, user='nobody@x.example', addr='10.0.0.3').data


def test_a_rotating_x_forwarded_for_does_not_reset_the_client_count(login, monkeypatch):
    client, db = login
    monkeypatch.delenv('HCM_TRUSTED_PROXIES', raising=False)
    for i in range(30):
        client.post('/login', data={'username': f'u{i}@x.example', 'password': 'x'},
                    headers={'X-Forwarded-For': f'192.0.2.{i}'}, environ_base={'REMOTE_ADDR': '10.9.9.9'})
    assert {r[2] for r in db.rows} == {'10.9.9.9'}
    r = client.post('/login', data={'username': 'anna@x.example', 'password': 'right-password'},
                    headers={'X-Forwarded-For': '192.0.2.200'}, environ_base={'REMOTE_ADDR': '10.9.9.9'})
    assert b'Too many failed sign-in attempts' in r.data


def test_five_wrong_codes_send_the_person_back_to_the_password(login):
    client, db = login
    assert _password(client).status_code == 302
    for _ in range(4):
        assert b'Invalid code' in _otp(client, '000000').data
    r = _otp(client, '000000')
    assert b'Sign in with your password again' in r.data
    r = _otp(client, '123456')                                   # the right code no longer helps: no pending login
    assert r.status_code == 302 and r.headers['Location'].endswith('/login')
    with client.session_transaction() as s:
        assert 'user_id' not in s


def test_an_older_cookie_does_not_bring_back_the_guesses(login):
    client, db = login
    _password(client)
    with client.session_transaction() as s:
        old = dict(s)
    for _ in range(5):
        _otp(client, '000000')
    with client.session_transaction() as s:                      # the attacker sends the cookie from before
        s.update(old)
    assert b'Sign in with your password again' in _otp(client, '123456').data


def test_a_right_code_signs_in_and_clears_the_typos(login):
    client, db = login
    _password(client, pw='typo')
    _password(client)
    r = _otp(client, '123456')
    assert r.status_code == 302
    with client.session_transaction() as s:
        assert s['user_id'] == 'u1'
    assert not [r for r in db.rows if r[1] == 'anna@x.example']


def test_a_stale_password_step_is_not_accepted(login):
    client, db = login
    _password(client)
    with client.session_transaction() as s:
        s['otp_pending_at'] = time.time() - 600
    r = _otp(client, '123456')
    assert r.status_code == 302 and r.headers['Location'].endswith('/login')


class _FakeTOTP:
    interval = 30

    def __init__(self, secret):
        self.secret = secret

    def generate_otp(self, counter):
        return f'{counter % 1000000:06d}'


def test_a_code_is_accepted_once(monkeypatch):
    monkeypatch.setattr(user_db, 'pyotp', types.SimpleNamespace(TOTP=_FakeTOTP))
    now = 1_800_000_000.0
    step = int(now // 30)
    assert user_db.totp_counter('S', f'{step % 1000000:06d}', now) == step
    assert user_db.totp_counter('S', f'{(step - 1) % 1000000:06d}', now) == step - 1
    assert user_db.totp_counter('S', f'{(step - 2) % 1000000:06d}', now) is None
    assert user_db.totp_counter('S', 'abcdef', now) is None

    last = {}

    def claim(name, counter):
        if last.get(name, -1) >= counter:
            return False
        last[name] = counter
        return True
    monkeypatch.setattr(user_db, '_claim_otp_counter', claim)
    monkeypatch.setattr(user_db, 'get_by_username', lambda n: {'otp_enabled': True, 'otp_secret': 'S'})
    monkeypatch.setattr(time, 'time', lambda: now)
    code = f'{step % 1000000:06d}'
    assert user_db.verify_otp_code('anna', code) is True
    assert user_db.verify_otp_code('anna', code) is False, 'the same code a second time is a replay'
    earlier = f'{(step - 1) % 1000000:06d}'
    assert user_db.verify_otp_code('anna', earlier) is False, 'an older code after a newer one is refused too'
