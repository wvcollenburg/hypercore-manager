"""Cross-site request forgery (1.8.1).

Before, the session cookie had no SameSite, CORS(app) answered every origin, and get_json(force=True) read a
text/plain form body as JSON: a page on any site could make a signed-in admin's browser apply an update, run
a plan, revoke a guest, reset someone's 2FA or register a cluster (Firefox and Safari send the cookie on
such a cross-site POST). /logout was a GET only.
"""
from __future__ import annotations

import re

import pytest


@pytest.fixture
def client(dash, monkeypatch):
    import mailer
    monkeypatch.setattr(mailer, 'settings', lambda with_secret=False: {'public_url': 'https://hcm.example.com', 'guest_url': None})
    monkeypatch.setattr(dash.access_db, 'user_state', lambda uid: None)
    return dash.app.test_client()


def test_a_post_from_another_site_is_refused(client):
    r = client.post('/api/update/apply', headers={'Origin': 'https://evil.example'})
    assert r.status_code == 403 and 'cross-site' in r.get_json()['error']
    r = client.post('/api/update/apply', headers={'Referer': 'https://evil.example/page'})
    assert r.status_code == 403
    assert client.post('/account/forgot', headers={'Origin': 'null'}).status_code == 403


def test_the_sites_own_pages_and_non_browser_clients_pass(client):
    # passes the origin check, then meets the login check (401): not refused as cross-site
    assert client.post('/api/update/apply', headers={'Origin': 'http://localhost'}).status_code == 401
    assert client.post('/api/update/apply', headers={'Origin': 'http://localhost:80'}).status_code == 401
    assert client.post('/api/update/apply').status_code == 401, 'no Origin and no Referer: a script'
    # behind a proxy that rewrites Host: the forwarded host, or the configured address in links
    assert client.post('/api/update/apply', headers={'Origin': 'https://hcm.lan', 'X-Forwarded-Host': 'hcm.lan'}).status_code == 401
    assert client.post('/api/update/apply', headers={'Origin': 'https://hcm.example.com'}).status_code == 401
    assert client.get('/api/update/status', headers={'Origin': 'https://evil.example'}).status_code == 401, 'reads are not changed'


def test_the_session_cookie_is_lax_and_secure_over_https(dash, client):
    assert dash.app.config['SESSION_COOKIE_SAMESITE'] == 'Lax'
    r = client.post('/logout', data={'next': '/invite/' + 'a' * 30})
    cookie = r.headers.get('Set-Cookie', '')
    assert 'SameSite=Lax' in cookie and 'Secure' not in cookie, 'plain HTTP keeps working'
    r = client.post('/logout', data={'next': '/invite/' + 'a' * 30}, headers={'X-Forwarded-Proto': 'https'})
    assert re.search(r';\s*Secure', r.headers.get('Set-Cookie', ''))


def test_no_cors_headers_for_other_origins(client):
    r = client.get('/login', headers={'Origin': 'https://evil.example'})
    assert 'Access-Control-Allow-Origin' not in r.headers


def test_logout_takes_a_post(client):
    r = client.post('/logout')
    assert r.status_code == 302 and r.headers['Location'].endswith('/login')
    assert client.get('/logout').status_code == 302, 'the old link still works'


def test_no_json_body_is_parsed_from_another_content_type(repo):
    src = (repo / 'dashboard' / 'app.py').read_text()
    forced = [m.start() for m in re.finditer(r'get_json\(force=True', src)]
    # one left on purpose: the POST /api/config handler belongs to another change; it is behind the origin check
    assert len(forced) <= 1
    for mod in ('access_api.py', 'account_api.py', 'invite_api.py', 'guest_api.py', 'mail_api.py'):
        assert 'force=True' not in (repo / 'dashboard' / mod).read_text(), mod
