"""Sessions can be ended, and the 2FA secret is not handed out freely (1.8.1).

Before, a session cookie stayed valid for 31 days through logout, a password change or reset, a 2FA reset
and an administrator setting the password: _login_problem only checked that the user existed, was enabled
and kept its username. GET /api/users/me/otp/setup returned the stored TOTP secret even with 2FA on, and
without the password. An open console was never checked again after it opened.
"""
from __future__ import annotations

import pytest

import user_db

UID = 'eeeeeeee-0000-0000-0000-000000000001'


@pytest.fixture
def signed_in(dash, monkeypatch):
    state = {'session_gen': 0, 'otp_enabled': False, 'bumped': []}
    monkeypatch.setattr(dash.access_db, 'user_state',
                        lambda uid: {'username': 'anna@x.example', 'enabled': True, 'session_gen': state['session_gen']})
    monkeypatch.setattr(dash.access_db, 'access_for', lambda uid, superuser=False: dash.rbac.Access([], dash.rbac.Tree(), user_id=uid))
    monkeypatch.setattr(user_db, 'get_by_username', lambda n: {'user_id': UID, 'username': n, 'role': 'user',
                                                               'otp_enabled': state['otp_enabled'], 'display_name': None})
    monkeypatch.setattr(user_db, 'verify_password', lambda n, pw: pw == 'right-password')
    monkeypatch.setattr(user_db, 'build_otp_setup', lambda n: {'secret': 'S', 'uri': 'otpauth://x', 'qr_data_url': 'data:'})

    def bump(n):
        state['session_gen'] += 1
        state['bumped'].append(n)
        return state['session_gen']
    monkeypatch.setattr(user_db, 'bump_session_gen', bump)
    client = dash.app.test_client()
    with client.session_transaction() as s:
        s['user_id'], s['username'], s['gen'] = UID, 'anna@x.example', 0
    return client, state


def test_a_session_from_before_the_generation_changed_is_out(signed_in):
    client, state = signed_in
    assert client.get('/api/users/me').status_code == 200
    state['session_gen'] = 1                         # e.g. the password was reset elsewhere
    assert client.get('/api/users/me').status_code == 401
    with client.session_transaction() as s:
        assert 'user_id' not in s


def test_a_cookie_from_before_the_upgrade_keeps_working(signed_in):
    client, _ = signed_in
    with client.session_transaction() as s:
        s.pop('gen')
    assert client.get('/api/users/me').status_code == 200


def test_changing_ones_password_ends_the_other_sessions_not_this_one(signed_in, monkeypatch, dash):
    client, state = signed_in
    other = dash.app.test_client()
    with other.session_transaction() as s:
        s['user_id'], s['username'], s['gen'] = UID, 'anna@x.example', 0
    monkeypatch.setattr(user_db, 'set_password', lambda n, pw: None)
    monkeypatch.setattr(dash.access_db, '_connect', lambda: (_ for _ in ()).throw(RuntimeError('audit')))
    import account_api
    monkeypatch.setattr(account_api, '_audit', lambda *a, **k: None)
    r = client.put('/api/users/me/password', json={'current_password': 'right-password', 'new_password': 'Another-pass-1'})
    assert r.status_code == 200 and state['bumped'] == ['anna@x.example']
    assert client.get('/api/users/me').status_code == 200
    assert other.get('/api/users/me').status_code == 401


def test_the_2fa_secret_needs_the_password_and_never_comes_once_it_is_on(signed_in):
    client, state = signed_in
    r = client.get('/api/users/me/otp/setup')
    assert r.status_code == 403 and r.get_json()['password_required']
    assert client.post('/api/users/me/otp/setup', json={'password': 'wrong'}).status_code == 403
    r = client.post('/api/users/me/otp/setup', json={'password': 'right-password'})
    assert r.status_code == 200 and set(r.get_json()) == {'secret', 'uri', 'qr_data_url'}
    state['otp_enabled'] = True
    assert client.post('/api/users/me/otp/setup', json={'password': 'right-password'}).status_code == 409


def test_turning_2fa_off_ends_the_other_sessions(signed_in, monkeypatch):
    client, state = signed_in
    monkeypatch.setattr(user_db, 'disable_otp', lambda n: None)
    assert client.post('/api/users/me/otp/disable', json={'password': 'right-password'}).status_code == 200
    assert state['bumped'] == ['anna@x.example'] and client.get('/api/users/me').status_code == 200


class _WS:
    def __init__(self): self.closed = None
    def receive(self, timeout=None): return None
    def close(self, reason=None, message=None): self.closed = (reason, message)


class _Up:
    closed = False
    def receive(self): raise RuntimeError('closed')
    def send(self, data): pass
    def close(self): self.closed = True


def test_an_open_console_closes_when_the_right_is_gone(dash, monkeypatch):
    up = _Up()
    monkeypatch.setattr(dash.simple_websocket.Client, 'connect', staticmethod(lambda *a, **k: up))
    monkeypatch.setattr(dash, '_CONSOLE_RECHECK_SECONDS', 0)
    ws = _WS()
    calls = []
    dash._relay_pipe(ws, 'ticket', 'binding', recheck=lambda: calls.append(1) or False)
    assert calls == [1] and ws.closed[0] == 4403 and up.closed
