"""sentinel/api.py — request handling that needs no database (tunables validation, cluster selection)."""
import importlib.util
from pathlib import Path

import pytest

import config as sentinel_config
import tunables

# loaded by path: other services have an api.py of their own on sys.path
_spec = importlib.util.spec_from_file_location('sentinel_api', Path(__file__).resolve().parents[1] / 'sentinel' / 'api.py')
sentinel_api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sentinel_api)


@pytest.fixture
def client(monkeypatch):
    saved = {}
    monkeypatch.setattr(tunables, 'save_many', lambda updates, cid=None: saved.update(updates))
    monkeypatch.setattr(tunables, 'get_with_meta', lambda cid=None: [])
    cfg = sentinel_config.Config(cluster_id='11111111-1111-1111-1111-111111111111')
    sentinel_api._api_app.config.update(SENTINEL_CFG=cfg, SENTINEL_CFGS={cfg.cluster_id: cfg}, API_TOKEN='')
    c = sentinel_api._api_app.test_client()
    c.saved = saved
    return c


@pytest.mark.parametrize('value', [0, 59, -1])
def test_a_prune_interval_below_a_minute_is_refused(client, value):
    """SENTINEL_VM_PRUNE_INTERVAL_SECONDS 0 made the pruning loop spin without pause."""
    r = client.put('/api/v1/tunables', json={'SENTINEL_VM_PRUNE_INTERVAL_SECONDS': value})
    assert r.status_code == 400 and client.saved == {}


def test_a_prune_interval_of_a_minute_is_accepted(client):
    r = client.put('/api/v1/tunables', json={'SENTINEL_VM_PRUNE_INTERVAL_SECONDS': 60})
    assert r.status_code == 200 and client.saved == {'SENTINEL_VM_PRUNE_INTERVAL_SECONDS': 60}
