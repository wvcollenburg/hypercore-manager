"""
hypercore_session.py — one HyperCore login session per (cluster, account) with node failover.
Shared by the collector, balancer, backup service and Sentinel (copies kept in step by
tools/sync_shared.py).

HyperCore has no primary node: every node serves the full REST API and can issue cluster-wide
commands. But a login session is only valid on the node that issued it, and each node has its
own TLS certificate. So a ClusterSession keeps one requests.Session logged in on one node,
logs in again on that node after a 401, and moves to another known node (a new login there)
as soon as the current one stops answering. The candidates are the node it uses now, then
every other usable node the registry knows (discovered through GET /Node), then the address
the cluster was registered with as a last resort. Nothing moves back when a node returns: the
registered address is only the seed for discovery. Every switch is written to
config.clusters.active_host so the other services follow at their next login and the
dashboard can show the node in use.

Logins count against HyperCore's hourly limit, so a session is reused until it fails; a
switch costs one login per candidate tried, and when every candidate fails the caller waits
its own interval before the next attempt.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

import requests

import cluster_registry

log = logging.getLogger('hypercore_session')

TRANSPORT_ERRORS = (requests.exceptions.ConnectionError, requests.exceptions.Timeout)   # SSLError is a ConnectionError


class ClusterUnreachable(requests.exceptions.ConnectionError):
    """No candidate node answered. `attempts` lists (host, error) in the order tried."""
    def __init__(self, cluster: str, attempts: list[tuple[str, str]]):
        self.attempts = attempts
        detail = '; '.join(f'{h}: {e}' for h, e in attempts) or 'no address known'
        super().__init__(f'cluster {cluster} unreachable on every known node ({detail})')


class AuthFailed(requests.exceptions.HTTPError):
    """The cluster refused the credentials (401/403). Not a node problem: no failover."""
    def __init__(self, host: str, status: int):
        self.host, self.status = host, status
        super().__init__(f'HyperCore login on {host} failed ({status}): check username and password')


class ClusterSession:
    def __init__(self, cluster_id: Optional[str], username: str, password: str, verify_tls: bool, *,
                 service: str = 'service', name: Optional[str] = None, host: Optional[str] = None,
                 seed: Optional[str] = None,
                 candidates: Optional[Callable[[Optional[str]], list[str]]] = None,
                 on_switch: Optional[Callable[[str], None]] = None,
                 http_factory: Optional[Callable[[], requests.Session]] = None,
                 base_url_of: Optional[Callable[[str], str]] = None,
                 timeout: float = 15, login_timeout: float = 10, logger: Optional[Callable[[str], None]] = None):
        """`host` pins the session to one address (no failover, no registry); otherwise the
        candidates come from the registry for `cluster_id` (or from `candidates(current)`), with
        `seed` (the registered address) as the fallback when the registry cannot be read."""
        self.cluster_id = cluster_id
        self.name = name or cluster_id or host or '?'
        self.username, self.password, self.verify = username, password, bool(verify_tls)
        self.service = service
        self.timeout, self.login_timeout = timeout, login_timeout
        self._pinned = cluster_registry.normalize_host(host) if host else None
        self._seed = cluster_registry.normalize_host(seed) if seed else None
        self._candidates = candidates
        self._on_switch = on_switch
        self._base_url_of = base_url_of or cluster_registry.base_url
        self._log = logger or (lambda msg: log.info('[%s] %s', self.name, msg))
        self.session: requests.Session = (http_factory or requests.Session)()
        self.session.verify = self.verify
        self.host: Optional[str] = self._pinned
        self.logged_in_at = 0.0
        self.lock = threading.RLock()
        if not self.verify:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    # ----------------------------------------------------------------- state
    @property
    def base_url(self) -> str:
        return self._base_url_of(self.host or self._pinned or '')

    @property
    def logged_in(self) -> bool:
        return bool(self.logged_in_at)

    def candidates(self, current: Optional[str] = None) -> list[str]:
        if self._pinned:
            return [self._pinned]
        if self._candidates:
            hosts = list(self._candidates(current))
        else:
            try:
                hosts = cluster_registry.candidate_hosts(self.cluster_id, current=current)
            except Exception as e:  # registry down: keep talking to what we know
                log.warning('[%s] registry unavailable for candidates: %s', self.name, e)
                hosts = []
        if not hosts:
            hosts = [h for h in (current, self._seed) if h]
        return hosts

    # ----------------------------------------------------------------- login
    def login(self, exclude: Optional[str] = None) -> str:
        """Log in on the first candidate that answers; returns the host used. Raises AuthFailed
        at once when the credentials are refused, ClusterUnreachable when nobody answers."""
        with self.lock:
            previous = self.host
            hosts = [h for h in self.candidates(self.host) if h != exclude] or self.candidates(self.host)
            attempts: list[tuple[str, str]] = []
            for h in hosts:
                self.session.cookies.clear()
                url = f'{self._base_url_of(h)}/login'
                try:
                    r = self.session.post(url, json={'username': self.username, 'password': self.password},
                                          timeout=self.login_timeout)
                except TRANSPORT_ERRORS as e:
                    attempts.append((h, _short(e)))
                    continue
                except requests.exceptions.RequestException as e:
                    attempts.append((h, _short(e)))
                    continue
                if r.status_code in (401, 403):
                    raise AuthFailed(h, r.status_code)
                if r.status_code >= 400:
                    attempts.append((h, f'HTTP {r.status_code}'))
                    continue
                self.host, self.logged_in_at = h, time.monotonic()
                if attempts:
                    self._log(f'{self.service}: node(s) not answering: ' + '; '.join(f'{a}: {e}' for a, e in attempts))
                if previous and previous != h:
                    self._log(f'{self.service}: switched from {previous} to {h}')
                if h != previous:
                    self._switched(h)
                return h
            self.logged_in_at = 0.0
            raise ClusterUnreachable(self.name, attempts)

    def _switched(self, host: str) -> None:
        try:
            if self._on_switch:
                self._on_switch(host)
            elif self.cluster_id and not self._pinned:
                cluster_registry.set_active_host(self.cluster_id, host)
        except Exception as e:  # bookkeeping never breaks the session
            log.warning('[%s] could not record active host %s: %s', self.name, host, e)

    def ensure(self) -> None:
        with self.lock:
            if not self.logged_in_at:
                self.login()

    def logout(self) -> None:
        with self.lock:
            if not self.logged_in_at:
                return
            try:
                self.session.post(f'{self.base_url}/logout', timeout=5)
            except requests.exceptions.RequestException:
                pass
            self.session.cookies.clear()
            self.logged_in_at = 0.0

    def close(self) -> None:
        self.logout()
        self.session.close()

    # -------------------------------------------------------------- requests
    def request(self, method: str, path: str, **kw) -> requests.Response:
        """One REST call on the node in use. A 401 means the session expired: log in again on
        the same node and retry once. A transport failure means the node is gone: log in on
        another candidate and retry once there. Raises ClusterUnreachable / AuthFailed."""
        kw.setdefault('timeout', self.timeout)
        self.ensure()
        host = self.host
        try:
            resp = self.session.request(method, f'{self.base_url}{path}', **kw)
        except TRANSPORT_ERRORS as e:
            self._log(f'{self.service}: {host} not answering ({_short(e)}); trying another node')
            with self.lock:
                self.logged_in_at = 0.0
                self.login(exclude=host)
            resp = self.session.request(method, f'{self.base_url}{path}', **kw)
        if resp.status_code == 401:
            with self.lock:
                self.logout()
                self.login()
            resp = self.session.request(method, f'{self.base_url}{path}', **kw)
        return resp

    def get(self, path: str, **kw) -> requests.Response:
        return self.request('GET', path, **kw)

    def post(self, path: str, **kw) -> requests.Response:
        return self.request('POST', path, **kw)

    def get_json(self, path: str, **kw):
        resp = self.request('GET', path, **kw)
        resp.raise_for_status()
        return resp.json()


def _short(e: BaseException) -> str:
    s = str(e)
    if isinstance(e, requests.exceptions.SSLError):
        return 'TLS: ' + s[:120]
    if isinstance(e, requests.exceptions.Timeout):
        return 'timeout'
    return (s.split('(Caused by', 1)[-1].strip(' )') if 'Caused by' in s else s)[:120] or type(e).__name__
