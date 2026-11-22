"""The alert history's Flux queries push their filters down (1.8.1).

The first filter after the measurement was contains(value: r.severity, set: [...]), which InfluxDB does not
push down: the _field filter, unique() and last() then ran in the Flux engine over every field of every
condition series (16 s on one real cluster, a 503 after 28 s on another, with a 10 s client timeout).
"""
from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

import rbac

CID = 'cccccccc-0000-0000-0000-0000000000cc'


def _rec(field, value, name='Disk', node='n1', sev='WARNING'):
    return SimpleNamespace(values={'condition_name': name, 'node_uuid': node, 'severity': sev, 'node_lan_ip': '10.0.0.1'},
                           get_value=lambda: value, get_field=lambda: field)


@pytest.fixture
def history(dash, monkeypatch):
    queries, clients = [], []
    answers = {'unique': [SimpleNamespace(records=[_rec('last_set_time', 1_000), _rec('last_set_time', 5_000)]),
                          SimpleNamespace(records=[_rec('last_clear_time', 2_000)])],
               'last': [SimpleNamespace(records=[_rec('value', True), _rec('is_ok', True), _rec('last_set_time', 5_000),
                                                 _rec('description', 'Disk is failing')])]}

    class Client:
        def __init__(self, timeout=None): clients.append(timeout)
        def query_api(self): return self
        def close(self): pass

        def query(self, org=None, query=''):
            queries.append(query)
            return answers['unique' if 'unique(' in query else 'last']
    monkeypatch.setattr(dash, 'get_influx_client', lambda timeout=None: Client(timeout))
    grants = [rbac.Grant(rbac.expand(['cluster.view']), 'cluster', cluster_id=CID)]
    monkeypatch.setattr(dash.access_db, 'access_for', lambda uid, superuser=False: rbac.Access(grants, rbac.Tree(), user_id=uid))
    monkeypatch.setattr(dash.access_db, 'user_state', lambda uid: {'username': 'u@x', 'enabled': True})
    monkeypatch.setattr(dash, '_enabled_clusters', lambda: [SimpleNamespace(cluster_id=CID)])
    monkeypatch.setattr(dash, '_alert_mutes', lambda cids: {})
    monkeypatch.setattr(dash.time, 'time', lambda: 10.0)
    c = dash.app.test_client()
    with c.session_transaction() as s:
        s['user_id'], s['username'] = 'u1', 'u@x'
    return c, queries, clients


def test_the_filters_are_pushed_down_and_the_timeout_is_longer(history):
    c, queries, clients = history
    r = c.get(f'/api/cluster-alerts/history?cluster={CID}&days=30&severity=critical,warning')
    assert r.status_code == 200
    assert len(queries) == 2 and clients == [60_000]
    for q in queries:
        assert 'contains(' not in q
        lines = [l.strip() for l in q.strip().splitlines()]
        m = lines.index('|> filter(fn: (r) => r["_measurement"] == "condition_metrics")')
        assert lines[m + 1].startswith('|> filter(fn: (r) => r["_field"] == ')
        assert re.match(r'\|> filter\(fn: \(r\) => r\["severity"\] == "[A-Z]+"( or r\["severity"\] == "[A-Z]+")*\)$', lines[m + 2])
    unique, last = queries
    assert 'range(start: -30d)' in unique and '"last_set_time"' in unique and '"last_clear_time"' in unique
    assert 'range(start: -1d)' in last and '|> last()' in last


def test_the_episodes_are_what_they_were(history):
    c, _, _ = history
    eps = c.get(f'/api/cluster-alerts/history?cluster={CID}&days=30').get_json()['episodes']
    assert [(e['started'] is not None, e['ended'] is not None, e['active'], e['description']) for e in eps] == [
        (True, False, True, 'Disk is failing'), (True, True, False, 'Disk is failing')]
