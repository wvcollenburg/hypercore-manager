"""sentinel/writer.py — audit events only for what changed, and audit retention (Foresight).

Needs numpy (writer imports the analysis modules): runs with the sentinel image, skipped elsewhere."""
from contextlib import contextmanager
from datetime import date

import pytest

pytest.importorskip('numpy')
pytest.importorskip('scipy')

import config as sentinel_config                          # noqa: E402
import writer as writer_mod                               # noqa: E402
from analysis.cluster_health import ClusterHealthResult   # noqa: E402
from analysis.forecaster import ForecastResult            # noqa: E402
from analysis.rightsizing import RightSizingResult        # noqa: E402

CID = '11111111-1111-1111-1111-111111111111'


class _DB:
    """Remembers what each recommendations table holds between cycles, like the real one."""
    def __init__(self):
        self.tables, self.audit, self.next_id, self.rowcounts = {}, [], 0, []

    @contextmanager
    def connect(self, cfg):
        yield self

    def cursor(self, **kw):
        return _Cur(self)


class _Cur:
    def __init__(self, db): self.db, self.result, self.rowcount = db, [], 0
    def __enter__(self): return self
    def __exit__(self, *a): return False

    def execute(self, sql, params=()):
        s = ' '.join(sql.split())
        db = self.db
        if s.startswith('SELECT') and 'FROM recommendations.' in s:
            table = s.split('FROM ')[1].split()[0]
            cols = [c.strip() for c in s[len('SELECT '):s.index(' FROM')].split(',')]
            self.result = [tuple(r.get(c) for c in cols) for r in db.tables.get(table, [])]
        elif s.startswith('DELETE FROM recommendations.'):
            db.tables[s.split()[2]] = []
        elif s.startswith('INSERT INTO recommendations.'):
            table = s.split()[2]
            cols = [c.strip() for c in s[s.index('(') + 1:s.index(')')].split(',')]
            db.tables.setdefault(table, []).append(dict(zip(cols, params)))
            db.next_id += 1
            self.result = [(db.next_id,)]
        elif s.startswith('INSERT INTO audit.recommendation_events'):
            db.audit.append(params)
        elif s.startswith('WITH old AS'):
            db.prune_params = params
            self.rowcount = db.rowcounts.pop(0) if db.rowcounts else 0
        elif 'cleanup_expired_recommendations' in s:
            self.result = []

    def fetchone(self): return self.result[0]
    def fetchall(self): return self.result


@pytest.fixture
def w(monkeypatch):
    db = _DB()
    monkeypatch.setattr(writer_mod.db, 'connect', db.connect)
    wr = writer_mod.Writer(sentinel_config.Config(cluster_id=CID))
    wr.fake = db
    return wr


def _rs(severity='WARNING', direction='over_provisioned', vm='aaaaaaaa-0000-0000-0000-000000000001'):
    return RightSizingResult(cluster_id=CID, vm_uuid=vm, vm_name='web', node_uuid='bbbbbbbb-0000-0000-0000-000000000001',
                             sizing_direction=direction, severity=severity, analysis_window_days=14,
                             cpu_signal_score=0.6, cpu_avg_usage_pct=5.0, cpu_p95_usage_pct=9.0,
                             latency_signal_score=None, spike_signal_score=None, composite_score=0.6,
                             signal_detail={}, current_vcpu=8)


def _fc(severity, horizon=30, node=None):
    return ForecastResult(cluster_id=CID, node_uuid=node, entity_label='Cluster', resource_dimension='storage',
                          forecast_horizon_days=horizon, current_usage=50.0, current_capacity=100.0,
                          forecast_value=60.0, confidence_score=0.9, driving_metric=None,
                          exhaustion_date=date(2027, 6, 1) if severity != 'INFO' else None,
                          severity=severity, model_metadata={'slope': 0.1})


def test_an_unchanged_recommendation_is_audited_once(w):
    """Every hourly cycle re-emitted an audit row for every row: ~10 M rows a year for 100 VMs, the outcome
    tracker checked each flagged VM 24x a day, and the webhook repeated the same CRITICAL hourly."""
    for _ in range(3):
        w.write_rightsizing([_rs()])
    assert len(w.fake.audit) == 1
    w.write_rightsizing([_rs(severity='CRITICAL')])
    assert len(w.fake.audit) == 2, 'a severity change is news'
    w.write_rightsizing([_rs(severity='CRITICAL', direction='under_provisioned')])
    assert len(w.fake.audit) == 3, 'a direction change is news'
    w.write_rightsizing([_rs(severity='CRITICAL', direction='under_provisioned'),
                         _rs(vm='aaaaaaaa-0000-0000-0000-000000000002')])
    assert len(w.fake.audit) == 4, 'a new VM is news'


def test_info_forecasts_are_not_audited_and_others_only_on_change(w):
    for _ in range(2):
        w.write_forecasts([_fc('INFO', 30), _fc('WARNING', 90)])
    types = [p[1] for p in w.fake.audit]
    assert types.count('forecast_snapshot') == 1, 'the INFO snapshot never, the WARNING once'
    assert types.count('resource_exhaustion') == 1


def test_exhaustion_rows_store_a_percentage_not_bytes(w):
    """current_usage_pct got the RAM forecast's current_usage in bytes: the report printed '320000000000.0%'."""
    ram = _fc('WARNING')
    ram.resource_dimension, ram.current_usage, ram.current_capacity = 'ram', 320e9, 400e9
    cpu = _fc('CRITICAL')
    cpu.resource_dimension, cpu.current_usage, cpu.current_capacity = 'cpu', 61.5, 200 / 3
    storage = _fc('WARNING')
    storage.current_usage, storage.current_capacity = 20e12, None   # storage: capacity unknown
    w.write_forecasts([ram, cpu, storage])
    pct = {r['resource_dimension']: r['current_usage_pct'] for r in w.fake.tables['recommendations.resource_exhaustion']}
    assert pct == {'ram': 80.0, 'cpu': 61.5, 'storage': None}


def test_cluster_health_is_audited_when_its_severity_changes(w):
    def health(sev):
        return ClusterHealthResult(cluster_id=CID, health_score=90.0, bottleneck_resource=None, severity=sev)
    for sev in ('INFO', 'INFO', 'WARNING', 'WARNING', 'INFO'):
        w.write_cluster_health(health(sev))
    assert [p[4] for p in w.fake.audit] == ['INFO', 'WARNING', 'INFO']


def test_audit_rows_past_the_retention_are_pruned_in_batches(w):
    w.fake.rowcounts = [writer_mod._AUDIT_PRUNE_BATCH, writer_mod._AUDIT_PRUNE_BATCH, 17]
    assert w.prune_audit(6) == 2 * writer_mod._AUDIT_PRUNE_BATCH + 17
    assert w.fake.prune_params == [CID, 6, writer_mod._AUDIT_PRUNE_BATCH]
    assert w.fake.rowcounts == []
