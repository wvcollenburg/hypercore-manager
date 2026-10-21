"""backup/hypercore.py — one login session per cluster account, reused across requests and clients."""
import importlib
import os
import sys

import pytest

_BACKUP = os.path.join(os.path.dirname(__file__), '..', 'backup')
_SHARED = os.path.join(os.path.dirname(__file__), '..', 'shared')
_CLASH = ('config', 'db', 'log', 'api', 'hypercore', 'jobs', 'scheduler', 'incremental', 'proxy_client', 'manifest_names', 'deploy', 'cluster_registry')


def _import():
    saved = {n: sys.modules.pop(n) for n in _CLASH if n in sys.modules}
    sys.path[:0] = [_BACKUP, _SHARED]
    try:
        return importlib.import_module('hypercore')
    finally:
        sys.path.remove(_BACKUP); sys.path.remove(_SHARED)
        for n in _CLASH:
            sys.modules.pop(n, None)
        sys.modules.update(saved)


hc_mod = _import()


class FakeResp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body if body is not None else {}
        self.ok = status < 400
        self.text = 'x' if body is not None else ''

    def json(self):
        return self._body


class FakeHttp:
    """Stands in for requests.Session: counts logins/logouts and can expire the cookie."""
    def __init__(self):
        self.verify = True
        self.logins = self.logouts = 0
        self.cookie = None
        self.expired = False
        self.calls = []

    class cookies:                                   # noqa: N801
        @staticmethod
        def clear():
            pass

    def post(self, url, **kw):
        if url.endswith('/login'):
            self.logins += 1
            if kw['json']['password'] == 'bad':
                return FakeResp(401)
            self.cookie = f'sess{self.logins}'; self.expired = False
            return FakeResp(200, {'sessionID': self.cookie})
        if url.endswith('/logout'):
            self.logouts += 1; self.cookie = None
            return FakeResp(200)
        return self.request('POST', url, **kw)

    def put(self, url, **kw):
        return self.request('PUT', url, **kw)

    def request(self, method, url, **kw):
        self.calls.append((method, url))
        if not self.cookie or self.expired:
            return FakeResp(401)
        return FakeResp(200, [{'name': 'vm'}])


@pytest.fixture
def fake(monkeypatch):
    f = FakeHttp()
    monkeypatch.setattr(hc_mod.requests, 'Session', lambda: f)
    hc_mod._SESSIONS.clear()
    monkeypatch.setattr(hc_mod.cluster_registry, 'base_url', lambda host: f'https://{host}/rest/v1')
    return f


def test_one_login_for_many_requests_and_clients(fake):
    a = hc_mod.HyperCoreClient('c1', 'svc', 'pw', verify_tls=False)
    for _ in range(5):
        a._request('GET', '/VirDomain')
    b = hc_mod.HyperCoreClient('c1', 'svc', 'pw', verify_tls=False)     # a second client (jobs.client_for makes one per call)
    b._request('GET', '/Node')
    assert fake.logins == 1 and fake.logouts == 0 and len(fake.calls) == 6
    assert a.session is b.session


def test_relogin_once_after_401(fake):
    a = hc_mod.HyperCoreClient('c1', 'svc', 'pw')
    a._request('GET', '/VirDomain')
    fake.expired = True                                   # the cluster dropped the session
    assert a._request('GET', '/VirDomain') == [{'name': 'vm'}]
    assert fake.logins == 2 and fake.logouts == 1


def test_session_renewed_when_old(fake, monkeypatch):
    a = hc_mod.HyperCoreClient('c1', 'svc', 'pw')
    a._request('GET', '/VirDomain')
    a.session.logged_in_at -= hc_mod.SESSION_MAX_AGE + 1
    a._request('GET', '/VirDomain')
    assert fake.logins == 2 and fake.logouts == 1


def test_changed_password_starts_a_new_session(fake):
    hc_mod.HyperCoreClient('c1', 'svc', 'pw')._request('GET', '/VirDomain')
    hc_mod.HyperCoreClient('c1', 'svc', 'pw2')._request('GET', '/VirDomain')
    assert fake.logins == 2 and fake.logouts == 1


def test_bad_credentials_raise(fake):
    with pytest.raises(hc_mod.HyperCoreError, match='401'):
        hc_mod.HyperCoreClient('c1', 'svc', 'bad')._request('GET', '/VirDomain')


def test_logout_all(fake):
    hc_mod.HyperCoreClient('c1', 'svc', 'pw')._request('GET', '/VirDomain')
    hc_mod.HyperCoreClient('c2', 'svc', 'pw')._request('GET', '/VirDomain')
    hc_mod.logout_all()
    assert fake.logouts == 2 and not hc_mod._SESSIONS
