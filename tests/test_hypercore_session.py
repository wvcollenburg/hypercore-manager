"""shared/hypercore_session.py — one session per cluster with node failover."""
import os
import sys

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'shared'))
import hypercore_session as hs   # noqa: E402


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body if body is not None else {}
        self.ok = status < 400
        self.text = ''

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f'{self.status_code}')


class FakeHttp:
    """Three nodes; `down` holds the ones that refuse connections, `expired` drops the cookie."""
    def __init__(self):
        self.verify = True
        self.down: set[str] = set()
        self.cookie = None
        self.expired = False
        self.logins: list[str] = []
        self.calls: list[tuple[str, str]] = []

    class cookies:                                   # noqa: N801
        @staticmethod
        def clear():
            pass

    @staticmethod
    def _host(url):
        return url.split('://', 1)[1].split('/', 1)[0]

    def post(self, url, **kw):
        h = self._host(url)
        if h in self.down:
            raise requests.exceptions.ConnectionError(f'{h} refused')
        if url.endswith('/login'):
            self.logins.append(h)
            if kw['json']['password'] == 'bad':
                return Resp(401)
            self.cookie, self.expired = h, False
            return Resp(200)
        if url.endswith('/logout'):
            self.cookie = None
            return Resp(200)
        return self.request('POST', url, **kw)

    def request(self, method, url, **kw):
        h = self._host(url)
        self.calls.append((method, h))
        if h in self.down:
            raise requests.exceptions.ConnectionError(f'{h} refused')
        if self.cookie != h or self.expired:
            return Resp(401)
        return Resp(200, {'node': h})

    def close(self):
        pass


NODES = ['10.0.0.1', '10.0.0.2', '10.0.0.3']


def _session(http, current_first=True, on_switch=None, **kw):
    def candidates(current):
        if current and current in NODES:
            i = NODES.index(current)
            return NODES[i:] + NODES[:i]
        return list(NODES)
    switches = []
    s = hs.ClusterSession('cid', 'svc', kw.pop('password', 'pw'), False, service='test', name='Lab',
                          candidates=candidates, on_switch=on_switch or switches.append,
                          http_factory=lambda: http, base_url_of=lambda h: f'https://{h}/rest/v1', logger=lambda m: None, **kw)
    s.switches = switches
    return s


def test_one_login_many_requests():
    http = FakeHttp(); s = _session(http)
    for _ in range(4):
        assert s.get_json('/Node') == {'node': '10.0.0.1'}
    assert http.logins == ['10.0.0.1'] and s.host == '10.0.0.1'
    assert s.switches == ['10.0.0.1'], 'the first contact is recorded as the active host'


def test_relogin_on_same_node_after_401():
    http = FakeHttp(); s = _session(http)
    s.get_json('/Node')
    http.expired = True
    assert s.get_json('/Node') == {'node': '10.0.0.1'}
    assert http.logins == ['10.0.0.1', '10.0.0.1'], 'a 401 is not a node failure'
    assert s.switches == ['10.0.0.1']


def test_failover_after_one_transport_failure_and_no_move_back():
    http = FakeHttp(); s = _session(http)
    s.get_json('/Node')
    http.down.add('10.0.0.1')
    assert s.get_json('/Node') == {'node': '10.0.0.2'}
    assert http.logins == ['10.0.0.1', '10.0.0.2'] and s.host == '10.0.0.2'
    assert s.switches == ['10.0.0.1', '10.0.0.2']
    http.down.clear()                                  # node 1 is back: nothing moves
    for _ in range(3):
        assert s.get_json('/Node') == {'node': '10.0.0.2'}
    assert http.logins == ['10.0.0.1', '10.0.0.2']


def test_failover_skips_every_dead_node():
    http = FakeHttp(); s = _session(http)
    s.get_json('/Node')
    http.down.update({'10.0.0.1', '10.0.0.2'})
    assert s.get_json('/Node') == {'node': '10.0.0.3'}
    assert http.logins == ['10.0.0.1', '10.0.0.3']


def test_all_nodes_down_raises_cluster_unreachable():
    http = FakeHttp(); s = _session(http)
    s.get_json('/Node')
    http.down.update(NODES)
    with pytest.raises(hs.ClusterUnreachable) as exc:
        s.get_json('/Node')
    assert [h for h, _ in exc.value.attempts] == ['10.0.0.2', '10.0.0.3'], 'the node that just failed is not retried'
    assert not s.logged_in
    http.down.clear()                                  # the next call recovers on its own
    assert s.get_json('/Node')['node'] in NODES


def test_bad_credentials_stop_at_the_first_node():
    http = FakeHttp(); s = _session(http, password='bad')
    with pytest.raises(hs.AuthFailed):
        s.get_json('/Node')
    assert http.logins == ['10.0.0.1'], 'refused credentials are not retried on other nodes'


def test_pinned_host_never_fails_over():
    http = FakeHttp()
    s = hs.ClusterSession(None, 'svc', 'pw', False, host='10.0.0.2', http_factory=lambda: http,
                          base_url_of=lambda h: f'https://{h}/rest/v1', logger=lambda m: None)
    assert s.get_json('/Node') == {'node': '10.0.0.2'}
    http.down.add('10.0.0.2')
    with pytest.raises(hs.ClusterUnreachable):
        s.get_json('/Node')
    assert http.logins == ['10.0.0.2']


def test_seed_is_the_fallback_when_nothing_is_known():
    http = FakeHttp()
    s = hs.ClusterSession('cid', 'svc', 'pw', False, seed='10.0.0.3', candidates=lambda cur: [],
                          on_switch=lambda h: None, http_factory=lambda: http,
                          base_url_of=lambda h: f'https://{h}/rest/v1', logger=lambda m: None)
    assert s.get_json('/Node') == {'node': '10.0.0.3'}
