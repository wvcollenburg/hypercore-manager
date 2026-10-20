"""Integration tests against a running local stack (http://localhost:5000).

Run with:  pytest -m integration
Skipped automatically when the dashboard is not reachable. The dashboard password is read
from the repo's .env (SC_DASHBOARD_USER / SC_DASHBOARD_PASSWORD).
"""
import http.cookiejar
import json
import urllib.error
import urllib.parse
import urllib.request

import pytest

BASE = 'http://localhost:5000'
pytestmark = pytest.mark.integration


def _reachable():
    try:
        urllib.request.urlopen(BASE + '/login', timeout=3)
        return True
    except Exception:
        return False


@pytest.fixture(scope='module')
def creds(repo):
    env = {}
    p = repo / '.env'
    if not p.exists():
        pytest.skip('.env not present')
    for line in p.read_text().splitlines():
        if '=' in line and not line.startswith('#'):
            k, _, v = line.partition('=')
            env[k.strip()] = v.strip()
    return env.get('SC_DASHBOARD_USER', 'admin'), env.get('SC_DASHBOARD_PASSWORD', '')


@pytest.fixture(scope='module')
def session(creds):
    if not _reachable():
        pytest.skip('dashboard not reachable on ' + BASE)
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    data = urllib.parse.urlencode({'username': creds[0], 'password': creds[1]}).encode()
    try:
        opener.open(BASE + '/login', data=data, timeout=10)
    except urllib.error.HTTPError as e:
        if e.code not in (302, 303):
            raise

    def get(path, method='GET', body=None):
        req = urllib.request.Request(BASE + path, method=method, data=body)
        if body is not None:
            req.add_header('Content-Type', 'application/json')
        with opener.open(req, timeout=20) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None), r.headers
    return get


def test_root_requires_login():
    req = urllib.request.Request(BASE + '/')
    opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler)
    with urllib.request.build_opener().open(req) as r:
        assert r.geturl().endswith('/login')


def test_login_page_is_rebranded():
    html = urllib.request.urlopen(BASE + '/login', timeout=5).read().decode()
    assert 'HyperCore Manager' in html
    assert '/static/img/' not in html and 'Scale Computing' not in html


def test_dashboard_page_is_rebranded(session):
    req = urllib.request.Request(BASE + '/')
    status, _, _ = session('/api/users/me')
    assert status == 200
    html = urllib.request.urlopen(urllib.request.Request(BASE + '/static/index.html'), timeout=5).read().decode()
    assert '<title>HyperCore Manager</title>' in html
    assert 'SC//' not in html and 'LogoBall' not in html
    assert 'communityNoticeBackdrop' in html


def test_me_exposes_community_ack(session):
    status, me, _ = session('/api/users/me')
    assert status == 200
    assert 'community_ack' in me


def test_community_ack_round_trip(session):
    status, body, _ = session('/api/users/me/community-ack', method='POST', body=b'{}')
    assert status == 200 and body['ok'] is True
    _, me, _ = session('/api/users/me')
    if not me.get('dev_mode'):
        assert me['community_ack'] is True


def test_update_status_always_has_current_version(session):
    status, d, _ = session('/api/update/status')
    assert status == 200
    assert d['current_version'], 'VERSION must be published even if the remote check fails'


@pytest.mark.parametrize('path', ['/api/nodes', '/api/vms', '/api/drives', '/api/migrations', '/api/config'])
def test_core_data_endpoints_respond(session, path):
    status, body, _ = session(path)
    assert status == 200
    assert body is not None


def test_sentinel_proxy_status(session):
    status, body, _ = session('/api/sentinel/status')
    assert status in (200, 502, 503)
    if status == 200:
        assert body.get('service') == 'sentinel'


def test_static_lang_files_served(session):
    for code in ('en', 'nl', 'fr', 'de'):
        with urllib.request.urlopen(BASE + f'/static/lang/{code}.js', timeout=5) as r:
            text = r.read().decode()
            assert 'community.ack' in text and 'SC//' not in text
