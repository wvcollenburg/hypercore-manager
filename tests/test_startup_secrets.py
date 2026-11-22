"""The dashboard does not start with the .env.template placeholders, and its workers share one session key
(1.8.1). A blank SC_DASHBOARD_SECRET_KEY made each of the four workers generate its own key: a login worked
only on the worker that issued the cookie."""
from __future__ import annotations

import re


def test_placeholders_are_found(dash):
    env = {'HCM_SECRET_KEY': 'CHANGE_ME', 'SC_DASHBOARD_SECRET_KEY': ' CHANGE_ME ', 'SC_DASHBOARD_PASSWORD': 'x'}
    assert dash._placeholder_secrets(env) == ['HCM_SECRET_KEY', 'SC_DASHBOARD_SECRET_KEY']
    assert dash._placeholder_secrets({'SC_DASHBOARD_PASSWORD': 'CHANGE_ME'}) == ['SC_DASHBOARD_PASSWORD']
    assert dash._placeholder_secrets({}) == []


def test_the_start_is_refused_with_a_clear_line(repo):
    src = (repo / 'dashboard' / 'app.py').read_text()
    block = src[src.index('_placeholders = _placeholder_secrets()'):src.index('_secret_key = _session_secret()')]
    assert 'raise SystemExit' in block and 'refusing to start' in block


def test_a_blank_session_key_is_derived_the_same_in_every_worker(dash):
    a = dash._session_secret({'HCM_SECRET_KEY': 'k' * 64})
    b = dash._session_secret({'HCM_SECRET_KEY': 'k' * 64, 'SC_DASHBOARD_SECRET_KEY': ''})
    assert a == b and re.fullmatch(r'[0-9a-f]{64}', a) and 'k' * 8 not in a
    assert a != dash._session_secret({'HCM_SECRET_KEY': 'other'})
    assert dash._session_secret({'SC_DASHBOARD_SECRET_KEY': 'mine', 'HCM_SECRET_KEY': 'k'}) == 'mine'
    assert dash.hcm_crypto.derived_token is not None
