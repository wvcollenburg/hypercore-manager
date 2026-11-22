"""Collector/collector.py — condition_metrics are written when a condition changes, plus a heartbeat."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location('hcm_collector', ROOT / 'Collector' / 'collector.py')
collector = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(collector)


def _cond(name='DriveFailed', node='n1', value=True, set_ms=1_700_000_000_000, clear_ms=0, visible=True):
    return {'name': name, 'nodeUUID': node, 'nodeLANIP': '10.0.0.1', 'severity': 'WARNING', 'value': value,
            'isOK': True, 'isExpired': False, 'description': 'd', 'lastSetTime': set_ms, 'lastClearTime': clear_ms,
            'tags': [], 'userVisible': visible}


def test_an_unchanged_condition_is_written_again_only_after_the_heartbeat():
    """Every condition, set or not, was written every slow poll (300 s) with seven fields, which made the
    dashboard's alert-history queries slow on real clusters."""
    state, t = {}, 1000.0
    conds = [_cond('A'), _cond('B', value=False), _cond('Hidden', visible=False)]
    assert len(collector.condition_points('c1', conds, state, now=t)) == 2
    assert collector.condition_points('c1', conds, state, now=t + 300) == []
    changed = [_cond('A', value=False, clear_ms=1_700_000_100_000), _cond('B', value=False)]
    lines = [p.to_line_protocol() for p in collector.condition_points('c1', changed, state, now=t + 600)]
    assert len(lines) == 1 and 'condition_name=A' in lines[0] and 'value=false' in lines[0], 'a clear is written at once'
    beat = t + 600 + collector.CONDITION_HEARTBEAT_SECONDS
    assert len(collector.condition_points('c1', changed, state, now=beat)) == 2, 'the heartbeat keeps the last hour filled'


def test_the_heartbeat_fits_the_dashboards_one_hour_window():
    """_active_conditions reads the last point of the past hour: an unchanged condition needs a point in it."""
    assert collector.CONDITION_HEARTBEAT_SECONDS + 300 < 3600


def test_without_state_every_condition_is_written():
    conds = [_cond('A'), _cond('B')]
    assert len(collector.condition_points('c1', conds)) == 2
    assert len(collector.condition_points('c1', conds)) == 2
