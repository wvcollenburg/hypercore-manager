"""backup/proxy_image.py — pushing the installation's proxy image to a registered proxy."""
import importlib
import os
import sys

import pytest

_BACKUP = os.path.join(os.path.dirname(__file__), '..', 'backup')
_CLASH = ('config', 'db', 'log', 'api', 'hypercore', 'jobs', 'scheduler', 'incremental', 'proxy_client', 'manifest_names', 'deploy', 'proxy_image')


def _import():
    saved = {n: sys.modules.pop(n) for n in _CLASH if n in sys.modules}
    sys.path.insert(0, _BACKUP)
    try:
        return importlib.import_module('proxy_image'), importlib.import_module('proxy_client')
    finally:
        sys.path.remove(_BACKUP)
        for n in _CLASH:
            sys.modules.pop(n, None)
        sys.modules.update(saved)


pi, pcm = _import()


class _DB:
    def __init__(self, proxies):
        self.proxies, self.updates = {p['id']: p for p in proxies}, []
    def get_proxy(self, pid, with_secret=False): return self.proxies.get(pid)
    def get_proxies(self, *a, **kw): return list(self.proxies.values())
    def update_proxy(self, pid, **f): self.updates.append((pid, f))


class _Client:
    def __init__(self, versions, active=(), can_update=True):
        self.versions, self.active, self.can_update, self.pushed = list(versions), list(active), can_update, []
    def identity(self):
        v = self.versions.pop(0) if len(self.versions) > 1 else self.versions[0]
        if isinstance(v, Exception):
            raise v
        return {'version': v, 'capabilities': {'update': self.can_update}}
    def active_jobs(self): return self.active
    def push_image(self, tag, chunks, timeout=900):
        self.pushed.append((tag, b''.join(chunks))); return {'accepted': True, 'tag': tag, 'bytes': 6}


STACK = {'available': True, 'version': '0.4.0', 'tag': 'hcm-proxy:0.4.0'}
SOURCE = lambda: ('hcm-proxy:0.4.0', iter([b'abc', b'def']))   # noqa: E731


def _run(monkeypatch, proxy, client, stack=STACK):
    db = _DB([proxy]); monkeypatch.setattr(pi, 'db', db)
    return db, pi.update_proxy(proxy['id'], client_factory=lambda p: client, source=SOURCE, info=lambda: stack, sleep=lambda s: None)


def test_update_pushes_and_waits_for_the_new_version(monkeypatch):
    client = _Client(['0.3.1', pcm.ProxyError('restarting'), '0.4.0'])
    db, r = _run(monkeypatch, {'id': 1, 'kind': 'remote', 'name': 'px'}, client)
    assert r == {'ok': True, 'version': '0.4.0'} and client.pushed == [('hcm-proxy:0.4.0', b'abcdef')]
    assert db.updates[0][1]['status'] == 'updating' and db.updates[-1][1]['status'] == 'ok' and db.updates[-1][1]['version'] == '0.4.0'


def test_update_skips_a_current_proxy_and_the_local_one(monkeypatch):
    client = _Client(['0.4.0'])
    db, r = _run(monkeypatch, {'id': 1, 'kind': 'remote', 'name': 'px'}, client)
    assert r['skipped'] and client.pushed == [] and db.updates == []
    db, r = _run(monkeypatch, {'id': 2, 'kind': 'local', 'name': 'stack'}, client)
    assert 'updated with the stack' in r['skipped']


def test_update_refuses_busy_or_hookless_proxies(monkeypatch):
    with pytest.raises(pcm.ProxyError, match='no update hook'):
        _run(monkeypatch, {'id': 1, 'kind': 'remote', 'name': 'px'}, _Client(['0.3.1'], can_update=False))
    with pytest.raises(pcm.ProxyError, match='job is running'):
        _run(monkeypatch, {'id': 1, 'kind': 'remote', 'name': 'px'}, _Client(['0.3.1'], active=[{'id': 'j', 'state': 'running'}]))
    with pytest.raises(pcm.ProxyError, match='no proxy image'):
        _run(monkeypatch, {'id': 1, 'kind': 'remote', 'name': 'px'}, _Client(['0.3.1']), stack={'available': False, 'detail': 'dashboard down'})


def test_update_reports_a_proxy_that_does_not_come_back(monkeypatch):
    clock = iter(range(0, 100000, 50))
    monkeypatch.setattr(pi.time, 'monotonic', lambda: next(clock))
    client = _Client(['0.3.1', '0.3.1'])
    db = _DB([{'id': 1, 'kind': 'remote', 'name': 'px'}]); monkeypatch.setattr(pi, 'db', db)
    with pytest.raises(pcm.ProxyError, match='did not come back'):
        pi.update_proxy(1, client_factory=lambda p: client, source=SOURCE, info=lambda: STACK, sleep=lambda s: None, wait_seconds=100)
    assert db.updates[-1][1]['status'] == 'error' and 'still reports 0.3.1' in db.updates[-1][1]['status_detail']


def test_update_all_reports_per_proxy(monkeypatch):
    db = _DB([{'id': 1, 'kind': 'remote', 'name': 'a', 'enabled': True}, {'id': 2, 'kind': 'local', 'name': 's', 'enabled': True},
              {'id': 3, 'kind': 'remote', 'name': 'b', 'enabled': True}])
    monkeypatch.setattr(pi, 'db', db)
    clients = {1: _Client(['0.3.1', '0.4.0']), 3: _Client(['0.3.1'], can_update=False)}
    out = pi.update_all(client_factory=lambda p: clients[p['id']], source=SOURCE, info=lambda: STACK, sleep=lambda s: None)
    assert [(r['name'], r['ok']) for r in out] == [('a', True), ('b', False)] and 'no update hook' in out[1]['error']


def test_update_survives_a_cut_off_push_answer(monkeypatch):
    class _Cut(_Client):
        def push_image(self, tag, chunks, timeout=900):
            b''.join(chunks); raise pcm.ProxyError('proxy unreachable: connection refused')
    client = _Cut(['0.3.1', pcm.ProxyError('restarting'), '0.4.0'])
    db, r = _run(monkeypatch, {'id': 1, 'kind': 'remote', 'name': 'px'}, client)
    assert r == {'ok': True, 'version': '0.4.0'} and db.updates[-1][1]['status'] == 'ok'
    client = _Cut(['0.3.1', '0.3.1'])
    clock = iter(range(0, 100000, 50)); monkeypatch.setattr(pi.time, 'monotonic', lambda: next(clock))
    db = _DB([{'id': 1, 'kind': 'remote', 'name': 'px'}]); monkeypatch.setattr(pi, 'db', db)
    with pytest.raises(pcm.ProxyError, match='connection refused'):
        pi.update_proxy(1, client_factory=lambda p: client, source=SOURCE, info=lambda: STACK, sleep=lambda s: None, wait_seconds=100)
    assert 'update failed' in db.updates[-1][1]['status_detail']
