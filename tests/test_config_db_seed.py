"""config_db.seed_from_env fills the Defaults template once and never overwrites what is stored.
The earlier rule re-applied the env var whenever the stored value equalled the hardcoded default,
so a node exclusion cleared in the dashboard, dry-run switched back on, or predictive balancing
switched off came back from .env at every restart."""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

psycopg2 = pytest.importorskip('psycopg2')
ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location('config_db_real', ROOT / 'shared' / 'config_db.py')
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


class _Cursor:
    def __init__(self, store): self.store = store
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=()):
        if sql.lstrip().startswith('INSERT'):
            cid, key, value, typ = params
            self.store.setdefault((cid, key), (value, typ))        # ON CONFLICT DO NOTHING
        else:
            raise AssertionError(f'the seed only inserts; it ran: {sql[:60]}')


class _Conn:
    def __init__(self, store): self.store = store; self.committed = 0
    def cursor(self): return _Cursor(self.store)
    def commit(self): self.committed += 1
    def close(self): pass


def test_seed_inserts_missing_keys_and_never_touches_stored_values(monkeypatch):
    cdb = _load(); store = {}
    monkeypatch.setattr(cdb, '_connect', lambda: _Conn(store))
    monkeypatch.setenv('SC_EXCLUDE_NODE_IPS', '10.0.0.5')
    monkeypatch.setenv('SC_DRY_RUN', 'false')
    monkeypatch.setenv('SC_PREDICTIVE_BALANCING_ENABLED', 'true')
    cdb.seed_from_env()
    assert store[(cdb.DEFAULT_CLUSTER_ID, 'EXCLUDE_NODE_IPS')] == ('10.0.0.5', 'list'), 'first start takes the env value'
    assert store[(cdb.DEFAULT_CLUSTER_ID, 'DRY_RUN')] == ('false', 'bool')
    assert store[(cdb.DEFAULT_CLUSTER_ID, 'RAM_LIMIT_PERCENT')] == ('85.0', 'float'), 'unset env falls back to the default'
    assert store[(cdb.DEFAULT_CLUSTER_ID, 'RAM_UPPER_THRESHOLD_PERCENT')] == ('65.0', 'float'), 'the shipped default, formerly only in .env.template'
    # the operator sets them back to the hardcoded defaults in the dashboard, then the stack restarts
    store[(cdb.DEFAULT_CLUSTER_ID, 'EXCLUDE_NODE_IPS')] = ('', 'list')
    store[(cdb.DEFAULT_CLUSTER_ID, 'DRY_RUN')] = ('true', 'bool')
    store[(cdb.DEFAULT_CLUSTER_ID, 'PREDICTIVE_BALANCING_ENABLED')] = ('false', 'bool')
    cdb.seed_from_env()
    assert store[(cdb.DEFAULT_CLUSTER_ID, 'EXCLUDE_NODE_IPS')] == ('', 'list'), 'a cleared exclusion stays cleared'
    assert store[(cdb.DEFAULT_CLUSTER_ID, 'DRY_RUN')] == ('true', 'bool'), 'dry-run switched back on stays on'
    assert store[(cdb.DEFAULT_CLUSTER_ID, 'PREDICTIVE_BALANCING_ENABLED')] == ('false', 'bool')


def test_node_exclusion_is_not_an_env_setting():
    """Node IPs are per cluster by nature; the list lives in the dashboard only."""
    for rel in ('.env.template', 'docker-compose.yaml', 'README.md', 'docs/manual/08-installing-and-updating.md'):
        assert 'SC_EXCLUDE_NODE_IPS' not in (ROOT / rel).read_text(), rel
    assert 'SC_DRY_RUN=false' not in (ROOT / 'install.py').read_text(), 'dry-run is switched per cluster in the dashboard'


def test_tunables_are_not_environment_settings():
    """Dry-run, thresholds, the Oracle, poll intervals and the stale threshold ship with defaults and
    are edited in the dashboard; nothing passes them through .env, compose or the installer."""
    import re
    names = ('SC_DRY_RUN', 'SC_VERBOSITY', 'SC_RAM_UPPER_THRESHOLD_PERCENT', 'SC_PREDICTIVE_BALANCING_ENABLED',
             'SC_PREDICTIVE_MAX_WORKERS', 'SC_POLL_INTERVAL', 'SC_DASHBOARD_STALE_SECONDS', 'SC_EXCLUDE_NODE_IPS')
    for rel in ('.env.template', 'docker-compose.yaml', 'README.md', 'docs/manual/08-installing-and-updating.md'):
        text = (ROOT / rel).read_text()
        for n in names:
            assert not re.search(n + r'(?![A-Z_])', text), f'{rel} still carries {n}'
    core = (ROOT / 'installer' / 'core.py').read_text()
    for n in names:
        assert f'key="{n}"' not in core, f'the installer still has a field for {n}'
