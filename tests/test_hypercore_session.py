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
    # the node that just failed (twice: once more on a fresh connection) is tried again only after the others
    assert [h for h, _ in exc.value.attempts] == ['10.0.0.2', '10.0.0.3', '10.0.0.1'], 'the failed node goes last'
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


def test_a_dropped_connection_is_retried_on_the_same_node_first():
    """A keep-alive connection the node closed is no dead node (seen on Lab 241: 'Remote end closed connection
    without response' sent a session onto node IPs its TLS check could never accept)."""
    http = FakeHttp(); s = _session(http)
    s.get_json('/Node')
    real = http.request
    dropped = []

    def once(method, url, **kw):
        if not dropped:
            dropped.append(url)
            raise requests.exceptions.ConnectionError('Remote end closed connection without response')
        return real(method, url, **kw)
    http.request = once
    assert s.get_json('/Node') == {'node': '10.0.0.1'}
    assert http.logins == ['10.0.0.1'] and s.switches == ['10.0.0.1'], 'no failover, no new login'


def test_the_failing_node_is_tried_last_not_dropped():
    """Every other node refuses (e.g. their IPs fail TLS verification): the node that failed is the last resort."""
    http = FakeHttp(); s = _session(http)
    s.get_json('/Node')
    http.down.update({'10.0.0.2', '10.0.0.3'})
    s.logged_in_at = 0.0
    assert s.login(exclude='10.0.0.1') == '10.0.0.1'
    assert http.logins[-1] == '10.0.0.1'


def _refused(host):
    """What requests raises when the TCP connection cannot be made: the request never left."""
    from urllib3.exceptions import MaxRetryError, NewConnectionError
    return requests.exceptions.ConnectionError(MaxRetryError(None, f'https://{host}/', NewConnectionError(None, 'refused')))


def test_a_post_that_timed_out_after_sending_is_not_sent_again():
    """A POST HyperCore accepted but answered late was sent three times (twice on the node, once more on the
    next node after a new login): a migration, clone or new VM could happen three times."""
    http = FakeHttp(); s = _session(http)
    s.get_json('/Node')
    real = http.request

    def slow(method, url, **kw):
        if method == 'POST':
            http.calls.append((method, http._host(url)))
            raise requests.exceptions.ReadTimeout('read timed out')
        return real(method, url, **kw)
    http.request = slow
    with pytest.raises(requests.exceptions.ReadTimeout):
        s.post('/VirDomain/action', json=[{'actionType': 'LIVEMIGRATE'}])
    assert [c for c in http.calls if c[0] == 'POST'] == [('POST', '10.0.0.1')], 'sent exactly once'
    assert http.logins == ['10.0.0.1'], 'no failover login for an ambiguous POST'


def test_a_post_whose_connection_dropped_after_sending_is_not_sent_again():
    http = FakeHttp(); s = _session(http)
    s.get_json('/Node')
    real = http.request

    def dropped(method, url, **kw):
        if method == 'POST':
            http.calls.append((method, http._host(url)))
            raise requests.exceptions.ConnectionError('Remote end closed connection without response')
        return real(method, url, **kw)
    http.request = dropped
    with pytest.raises(requests.exceptions.ConnectionError):
        s.post('/VirDomain/action', json=[])
    assert len([c for c in http.calls if c[0] == 'POST']) == 1


def test_a_post_that_never_left_fails_over():
    """Connection refused: HyperCore never saw it, so the POST may go to the next node."""
    http = FakeHttp(); s = _session(http)
    s.get_json('/Node')
    real = http.request

    def refuse(method, url, **kw):
        h = http._host(url)
        if h == '10.0.0.1':
            http.calls.append((method, h))
            raise _refused(h)
        return real(method, url, **kw)
    http.request = refuse
    assert s.post('/VirDomain/action', json=[]).status_code == 200
    assert [c for c in http.calls if c[0] == 'POST'] == [('POST', '10.0.0.1'), ('POST', '10.0.0.1'), ('POST', '10.0.0.2')]


def test_a_401_another_thread_already_handled_makes_no_second_login():
    """Six threads seeing the same expiry each did logout()+login(); a logout could kill the session the
    previous thread had just made. Only the first thread logs in again."""
    http = FakeHttp(); s = _session(http)
    s.get_json('/Node')
    real = http.request
    real_post = http.post
    logouts = []

    def post(url, **kw):
        if url.endswith('/logout'):
            logouts.append(url)
        return real_post(url, **kw)
    http.post = post
    raced = []

    def racing(method, url, **kw):
        if not raced:                                  # our request goes out with the old session...
            raced.append(1)
            s.login()                                  # ...while another thread logs in again
            return Resp(401)
        return real(method, url, **kw)
    http.request = racing
    assert s.get_json('/Node') == {'node': '10.0.0.1'}
    assert http.logins == ['10.0.0.1', '10.0.0.1'], "the other thread's login is reused"
    assert logouts == [], 'the fresh session is not logged out'
