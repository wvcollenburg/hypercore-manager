"""sentinel/analysis/whatif.py — What-If on the cluster-wide forecasts (D5)."""
from datetime import date, timedelta

from analysis.whatif import WhatIfRequest, run_whatif

GB = 1024 ** 3


def _fc(dim, usage, capacity, slope, node=None, label='Cluster', node_count=3):
    return {'node_uuid': node, 'entity_label': label, 'resource_dimension': dim, 'forecast_horizon_days': 30,
            'current_usage': usage, 'current_capacity': capacity,
            'model_metadata': {'slope': slope, 'node_count': node_count}}


def _state(exhaustion=(), health=96.0):
    rows = [
        _fc('cpu', 30.0, 200 / 3, 0.01), _fc('ram', 300 * GB, 512 * GB, 0.1 * GB), _fc('storage_capacity', 20e12, None, 1e9),
        # per-node rows of the same dimensions, listed after the cluster rows (the last row used to win)
        _fc('cpu', 90.0, 100.0, 2.0, node='n1', label='Node 1'), _fc('cpu', 5.0, 100.0, -1.0, node='n2', label='Node 2'),
        _fc('ram', 250 * GB, 256 * GB, 5 * GB, node='n1', label='Node 1'),
    ]
    return {'health_row': {'health_score': health, 'severity': 'INFO', 'bottleneck_resource': None},
            'forecast_rows': rows, 'exhaustion_rows': list(exhaustion), 'node_count': 3, 'vm_count': 40}


def test_adding_a_node_never_lowers_health_nor_creates_exhaustion_dates():
    """'add 1 node' lowered health 96.0 -> 91.8 and produced exhaustion dates the baseline did not have:
    capacities were invented (storage current x 1.5, RAM x 2.0) and per-node rows were blended in."""
    r = run_whatif(WhatIfRequest(scenario='add_nodes', count=1), _state())
    assert r.simulated['health_score'] >= r.baseline['health_score'] == 96.0
    assert all(v is None for v in r.simulated['exhaustion_dates'].values()), r.simulated['exhaustion_dates']


def test_adding_nodes_moves_a_cpu_exhaustion_later_on_the_n_plus_1_capacity():
    soon = date.today() + timedelta(days=60)
    state = _state(exhaustion=[{'node_uuid': None, 'resource_dimension': 'cpu', 'exhaustion_date': soon, 'severity': 'WARNING'}])
    state['forecast_rows'][0] = _fc('cpu', 60.0, 200 / 3, 0.1)            # 6.7 points of headroom at 0.1/day
    r = run_whatif(WhatIfRequest(scenario='add_nodes', count=1), state)
    sim = date.fromisoformat(r.simulated['exhaustion_dates']['cpu'])
    # 4 nodes: usage 45 %, growth 0.075/day, capacity 75 % -> 400 days
    assert abs((sim - date.today()).days - 400) <= 1
    assert r.delta['exhaustion_gained_days']['cpu'] > 0
    assert r.simulated['health_score'] >= r.baseline['health_score']


def test_ram_capacity_grows_by_the_added_nodes():
    soon = date.today() + timedelta(days=100)
    state = _state(exhaustion=[{'node_uuid': None, 'resource_dimension': 'ram', 'exhaustion_date': soon, 'severity': 'WARNING'},
                               {'node_uuid': 'n1', 'resource_dimension': 'ram', 'exhaustion_date': date.today(), 'severity': 'EMERGENCY'}])
    r = run_whatif(WhatIfRequest(scenario='add_nodes', count=1), state)
    assert r.baseline['exhaustion_dates']['ram'] == soon.isoformat(), 'the per-node row does not override the cluster row'
    # 3 nodes of 256 GiB, N+1 = 512 GiB; 4 nodes -> 768 GiB: (768 - 300) / 0.1 = 4680 days -> beyond 10 years
    assert r.simulated['exhaustion_dates']['ram'] is None or date.fromisoformat(r.simulated['exhaustion_dates']['ram']) >= soon


def test_adding_vms_without_a_known_capacity_invents_no_date():
    r = run_whatif(WhatIfRequest(scenario='add_vms', count=10), _state())
    assert r.simulated['exhaustion_dates'].get('storage_capacity') is None
    assert r.simulated['health_score'] <= r.baseline['health_score']
