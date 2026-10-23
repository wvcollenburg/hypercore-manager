"""
Shared fixtures for the HyperCore Manager test suite.

The application modules live in per-container directories (dashboard/, Balancer/,
sentinel/, installer/) rather than one package, so the repo paths are added to
sys.path here. Heavy third-party dependencies that the containers install but a
test environment does not need (InfluxDB client, psycopg2, python-dotenv, Prophet)
are replaced with light stubs *before* the modules under test are imported.
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
for sub in ('dashboard', 'Balancer', 'sentinel', '.'):   # backup/ is loaded in isolation by its own test module
    p = str(REPO / sub)
    if p not in sys.path:
        sys.path.insert(0, p)


def _stub(name: str, **attrs) -> types.ModuleType:
    """Register a stub module under *name* unless the real one imports cleanly."""
    try:
        __import__(name)
        return sys.modules[name]
    except Exception:
        mod = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(mod, k, v)
        sys.modules[name] = mod
        return mod


class _Point:
    def __init__(self, *_a, **_k): pass
    def tag(self, *_a, **_k): return self
    def field(self, *_a, **_k): return self
    def time(self, *_a, **_k): return self


class _InfluxDBClient:
    def __init__(self, *_a, **_k): pass
    def write_api(self, *_a, **_k): return self
    def query_api(self, *_a, **_k): return self
    def close(self): pass


_stub('dotenv', load_dotenv=lambda *a, **k: None)
_stub('influxdb_client', InfluxDBClient=_InfluxDBClient, Point=_Point)
_stub('influxdb_client.client')
_stub('influxdb_client.client.write_api', SYNCHRONOUS='SYNCHRONOUS')
_stub('influxdb_client.client.exceptions', InfluxDBError=type('InfluxDBError', (Exception,), {}))
class _Extensions:
    connection = object


_stub('psycopg2', connect=lambda *a, **k: (_ for _ in ()).throw(RuntimeError('no db in tests')),
      extensions=_Extensions)
_stub('psycopg2.extras', RealDictCursor=object)

# the balancer reads live config from Postgres through config_db; in tests every key falls
# back to its coded default
_TEMPLATE = '00000000-0000-0000-0000-000000000001'
_stub('config_db',
      TEMPLATE_CLUSTER_ID=_TEMPLATE,
      DEFAULT_CLUSTER_ID=_TEMPLATE,
      get=lambda key, cluster_id=_TEMPLATE: None,
      get_all=lambda cluster_id=_TEMPLATE: {},
      copy_template_to=lambda cluster_id: None,
      save=lambda *a, **k: None,
      save_many=lambda *a, **k: None,
      seed_from_env=lambda: None)
_stub('cluster_registry', TEMPLATE_CLUSTER_ID=_TEMPLATE, list_enabled=lambda: [],
      credentials=lambda cid: ('', ''), mark_seen=lambda *a, **k: None, mark_error=lambda *a, **k: None)
_stub('hcm_crypto', key_configured=lambda: True, ENV_VAR='HCM_SECRET_KEY')
_stub('db', connect=lambda *a, **k: (_ for _ in ()).throw(RuntimeError('no db in tests')))


@pytest.fixture(scope='session')
def repo() -> Path:
    return REPO


@pytest.fixture(scope='session')
def env_template(repo) -> dict[str, str]:
    """Key/value pairs from .env.template (comments and blanks skipped)."""
    out = {}
    for line in (repo / '.env.template').read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, _, v = line.partition('=')
        out[k.strip()] = v.strip()
    return out
