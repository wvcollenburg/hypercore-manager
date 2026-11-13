"""dashboard/alerts.py — which HyperCore conditions light the badges on a cluster card, and how
the history is rebuilt from the Collector's lastSetTime / lastClearTime values."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'dashboard'))
import alerts  # noqa: E402

H = 3600_000


def _row(value=True, is_ok=True, severity='WARNING', set_ms=1_700_000_000_000):
    return {'fields': {'value': value, 'is_ok': is_ok, 'description': 'd', 'last_set_time': set_ms},
            'severity': severity, 'node_ip': '10.0.0.1'}


def test_only_set_and_ok_conditions_are_active():
    latest = {('c1', 'A', 'n1'): _row(), ('c1', 'B', 'n1'): _row(value=False),
              ('c1', 'C', 'n1'): _row(is_ok=False),            # the spec: ignore value when isOK is false
              ('c1', 'D', 'n1'): _row(severity='DEBUG')}
    items = alerts.active_items(latest)['c1']
    assert [i['name'] for i in items] == ['A']
    assert items[0]['key'] == 'condition:A:n1' and items[0]['since'].startswith('2023-11-14')
    assert items[0]['node_uuid'] == 'n1', 'the node is a field of its own, for the alerts on each node card'


def test_severity_mapping_onto_the_four_badges():
    m = alerts.ALERT_SEVERITY
    assert {m[k] for k in ('EMERGENCY', 'ALERT', 'CRITICAL', 'ERROR')} == {'critical'}
    assert (m['WARNING'], m['NOTICE'], m['INFO']) == ('warning', 'notice', 'info')
    assert 'DEBUG' not in m


def test_same_condition_on_two_nodes_is_two_alerts():
    latest = {('c1', 'A', 'n1'): _row(), ('c1', 'A', 'n2'): _row()}
    assert {i['key'] for i in alerts.active_items(latest)['c1']} == {'condition:A:n1', 'condition:A:n2'}


def test_episodes_pair_set_and_clear_times():
    k = ('A', 'n1')
    t0 = 1_700_000_000_000
    eps = alerts.condition_episodes({k: {t0, t0 + 10 * H}}, {k: {t0 + 2 * H}},
                                    {k: {'value': True, 'last_set_time': t0 + 10 * H}}, since_ms=0)
    assert [(e['start'], e['end'], e['active']) for e in eps] == [(t0, t0 + 2 * H, False), (t0 + 10 * H, None, True)]


def test_episode_without_a_clear_and_not_set_any_more_is_not_active():
    k = ('A', 'n1')
    t0 = 1_700_000_000_000
    eps = alerts.condition_episodes({k: {t0}}, {}, {k: {'value': False, 'last_set_time': t0}}, since_ms=0)
    assert eps == [{'key': k, 'start': t0, 'end': None, 'active': False}]


def test_a_set_condition_that_is_not_ok_is_not_still_active():
    k = ('A', 'n1')
    t0 = 1_700_000_000_000
    eps = alerts.condition_episodes({k: {t0}}, {}, {k: {'value': True, 'is_ok': False, 'last_set_time': t0}}, since_ms=0)
    assert eps[0]['active'] is False


def test_episodes_that_ended_before_the_period_are_left_out_and_zero_is_never_a_time():
    k = ('A', 'n1')
    t0 = 1_700_000_000_000
    eps = alerts.condition_episodes({k: {0, t0}}, {k: {0, t0 + H}}, {k: {'value': False}}, since_ms=t0 + 2 * H)
    assert eps == []


def test_ms_iso():
    assert alerts.ms_iso(0) is None and alerts.ms_iso(None) is None and alerts.ms_iso('x') is None
    assert alerts.ms_iso(1_700_000_000_000).startswith('2023-11-14T22:13:20')
