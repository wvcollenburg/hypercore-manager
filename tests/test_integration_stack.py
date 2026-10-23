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

import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'dashboard'))

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


def test_storage_summary_is_per_vm_not_per_disk(session):
    """The Infraviewer overview reads one series per VM plus cluster totals; per-disk history
    only travels through /api/vsds?vm=<uuid>."""
    status, body, _ = session('/api/storage/summary?hours=1')
    assert status == 200 and set(body) >= {'vms', 'cluster', 'hours'}
    for vm in body['vms']:
        assert set(vm) == {'uuid', 'name', 'history', 'vsds'}
        for p in vm['history']:
            assert 'time' in p and not set(p) - {'time', *__import__('storage_summary').FIELDS}
        for d in vm['vsds']:
            assert 'history' not in d and {'uuid', 'name', 'type', 'time'} <= set(d)
    if body['vms']:
        status, disks, _ = session(f"/api/vsds?hours=1&vm={body['vms'][0]['uuid']}")
        assert status == 200 and all(d['vm_uuid'] == body['vms'][0]['uuid'] for d in disks)


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


def _json_keys(obj, acc=None):
    acc = set() if acc is None else acc
    if isinstance(obj, dict):
        for k, v in obj.items():
            acc.add(k); _json_keys(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            _json_keys(v, acc)
    return acc


def test_backup_proxy_hides_paths_from_non_admins(session, creds):
    """A folder member may use the Backup tab but never sees target URIs, paths or accounts."""
    status, data, _ = session('/api/backup/status')
    assert status == 200
    if not data.get('available'):
        pytest.skip('backup container not running')
    assert 'plans' in data  # the count key follows the module's naming
    # temporary non-admin user, created and removed through the admin API
    name = 'hcm-it-viewer'
    session('/api/users', 'POST', json.dumps({'username': name, 'password': 'It-Viewer-1234', 'role': 'user'}).encode())
    try:
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        try:
            opener.open(BASE + '/login', data=urllib.parse.urlencode({'username': name, 'password': 'It-Viewer-1234'}).encode(), timeout=10)
        except urllib.error.HTTPError as e:
            if e.code not in (302, 303):
                raise

        def get(path):
            try:
                with opener.open(BASE + path, timeout=20) as r:
                    raw = r.read()
                    return r.status, (json.loads(raw) if raw else None)
            except urllib.error.HTTPError as e:
                return e.code, None
        assert get('/api/backup/targets')[0] == 403
        for path in ('/api/backup/status', '/api/backup/plans', '/api/backup/runs'):
            code, body = get(path)
            assert code in (200, 403, 404), (path, code)
            if code == 200:
                leaked = _json_keys(body) & {'base', 'user', 'username', 'path_uri_base', 'prune_path', 'smb_user', 'source_uri', 'uri_base', 'uri'}
                assert not leaked, (path, leaked)
    finally:
        session(f'/api/users/{name}', 'DELETE')



def test_a_session_whose_user_is_gone_is_sent_back_to_login(repo, session):
    """A reinstall that keeps SC_DASHBOARD_SECRET_KEY leaves browsers with valid cookies for users
    that no longer exist; the dashboard must treat them as logged out, not as a user without rights
    (seen on the online install after a volume wipe: the onboarding said 'ask an administrator')."""
    from flask.sessions import SecureCookieSessionInterface
    status, me, _ = session('/api/users/me')
    assert status == 200
    if me.get('dev_mode'):
        pytest.skip('auth disabled on this stack')
    key = ''
    for line in (repo / '.env').read_text().splitlines():
        if line.startswith('SC_DASHBOARD_SECRET_KEY='):
            key = line.partition('=')[2].strip()
    if not key:
        pytest.skip('SC_DASHBOARD_SECRET_KEY not in .env')

    cookie = SecureCookieSessionInterface().get_signing_serializer(_flask_app(key)).dumps(
        {'user_id': '00000000-0000-0000-0000-000000000000', 'username': 'ghost', 'role': 'admin'})
    req = urllib.request.Request(BASE + '/api/users/me', headers={'Cookie': f'session={cookie}'})
    try:
        urllib.request.urlopen(req, timeout=10); code = 200
    except urllib.error.HTTPError as e:
        code = e.code
    assert code == 401, 'the API treats the ghost session as logged out'
    req = urllib.request.Request(BASE + '/', headers={'Cookie': f'session={cookie}'})
    try:
        urllib.request.build_opener(_NoRedirect()).open(req, timeout=10); code, loc = 200, ''
    except urllib.error.HTTPError as e:
        code, loc = e.code, e.headers.get('Location', '')
    assert code in (302, 303) and 'login' in loc, 'the page redirects the ghost session to login'


def _flask_app(key):
    from flask import Flask
    app = Flask('probe'); app.secret_key = key
    return app


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)
