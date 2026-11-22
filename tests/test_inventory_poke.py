"""A VM change made in HCM shows in the dashboard within seconds (William, 2026-11-22).

The backup service notifies 'hcm_inventory' with the cluster id when a VM change set or a restore finishes; the
collector's listener wakes that cluster's worker for an immediate poll; the dashboard drops a VM that the newest
poll no longer reports; the VM list follows a running change every few seconds.
"""
import importlib.util
import socket
import sys
import threading
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location('hcm_collector_poke', ROOT / 'Collector' / 'collector.py')
collector = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(collector)
sys.path.insert(0, str(ROOT / 'dashboard'))
import vm_rows  # noqa: E402


class _Conn:
    """A database connection with one pending notification, readable through a socket pair."""
    def __init__(self, payloads, stop):
        self.a, self.b = socket.socketpair()
        self.b.send(b'x')
        self.pending, self.notifies, self.stop, self.autocommit, self.sql = list(payloads), [], stop, False, []
    def fileno(self): return self.a.fileno()
    def cursor(self):
        conn = self
        class _C:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, sql): conn.sql.append(sql)
        return _C()
    def poll(self):
        self.a.recv(16)
        self.notifies.extend(types.SimpleNamespace(payload=p) for p in self.pending)
        self.pending = []
        self.stop.set()                        # one round is enough for the test
    def close(self): self.a.close(); self.b.close()


def test_a_notification_wakes_that_clusters_worker_only():
    stop = threading.Event()
    w1, w2 = types.SimpleNamespace(wake=threading.Event()), types.SimpleNamespace(wake=threading.Event())
    conn = _Conn(['cluster-1'], stop)
    collector.inventory_listener({'cluster-1': (w1, None), 'cluster-2': (w2, None)}, stop, connect=lambda: conn)
    assert w1.wake.is_set() and not w2.wake.is_set()
    assert conn.sql == ['LISTEN hcm_inventory'] and conn.autocommit


def test_the_listener_survives_a_database_that_is_not_there():
    stop, calls = threading.Event(), []
    def down():
        calls.append(1)
        stop.set()
        raise RuntimeError('could not connect')
    collector.inventory_listener({}, stop, connect=down)
    assert calls == [1]


def test_a_vm_the_newest_poll_did_not_report_is_dropped():
    now = datetime(2026, 11, 22, 21, 0, tzinfo=timezone.utc)
    seen = {'a': now, 'b': now - timedelta(seconds=1), 'gone': now - timedelta(seconds=5)}   # 'gone': the poll 5 s before
    vms = {'a': {}, 'b': {}, 'gone': {}, 'never-inventoried': {}}
    assert sorted(vm_rows.missing_from_last_poll(vms, seen)) == ['gone', 'never-inventoried']
    assert vm_rows.missing_from_last_poll(vms, {}) == [], 'no inventory at all: drop nothing'


def test_the_backup_service_pokes_the_collector_when_a_change_set_ends():
    src = (ROOT / 'backup' / 'vmedit.py').read_text()
    fin = src[src.index('def _finish('):]
    fin = fin[:fin.index('\ndef ')]
    assert 'poke_inventory' in fin
    assert 'db.poke_inventory(cid)' in (ROOT / 'backup' / 'restore.py').read_text()
    assert "pg_notify(%s, %s)" in (ROOT / 'backup' / 'db.py').read_text()


def test_the_vm_list_follows_a_running_change(repo):
    src = (repo / 'dashboard' / 'static' / 'index.html').read_text()
    assert src.count('if (r.ok) _vmFollow();') == 3, 'edit, power/delete/clone/snapshot, and New VM'
    load = src[src.index('async function loadVMs()'):][:1500]
    assert '_vmFollowTick();' in load
