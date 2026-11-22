"""config_db: every stored value is one its readers can cast, and one that is not costs only that key.

Before 1.8.1 an emptied number field in the settings arrived as null and was stored as the text 'None';
get_all() then raised on it, logged 'PostgreSQL unavailable' and handed the balancer the env/hardcoded
default of EVERY key of that cluster (dry-run back on, node exclusions gone). The string 'false' was
stored as true, nothing was range-checked, and a failed write still answered ok."""
import importlib.util
import re
from pathlib import Path

import pytest

psycopg2 = pytest.importorskip('psycopg2')
ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location('config_db_values', ROOT / 'shared' / 'config_db.py')
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


class _Cursor:
    def __init__(self, conn): self.conn = conn
    def __enter__(self): return self
    def __exit__(self, *a): return False

    def execute(self, sql, params=()):
        if self.conn.fail:
            raise psycopg2.OperationalError('server closed the connection')
        if sql.lstrip().startswith('INSERT'):
            cid, key, value, typ = params
            self.conn.store[(cid, key)] = (value, typ)

    def fetchall(self):
        return [(k, v, t, None) if self.conn.meta else (k, v, t) for (c, k), (v, t) in self.conn.store.items()]


class _Conn:
    def __init__(self, store, fail=False, meta=False):
        self.store, self.fail, self.meta = store, fail, meta
    def cursor(self): return _Cursor(self)
    def commit(self): pass
    def close(self): pass


CID = 'c1'


def test_one_unreadable_value_costs_only_that_key(monkeypatch):
    cdb = _load()
    store = {(CID, 'DRY_RUN'): ('false', 'bool'), (CID, 'EXCLUDE_NODE_IPS'): ('10.0.0.5', 'list'),
             (CID, 'AVG_WINDOW_MINUTES'): ('None', 'int'), (CID, 'PREDICTIVE_BALANCING_ENABLED'): ('None', 'bool')}
    monkeypatch.setattr(cdb, '_connect', lambda: _Conn(store))
    got = cdb.get_all(CID)
    assert got['DRY_RUN'] is False, 'dry-run stays off'
    assert got['EXCLUDE_NODE_IPS'] == ['10.0.0.5'], 'exclusions stay'
    assert got['AVG_WINDOW_MINUTES'] == 5, 'only the broken key falls back to its default'
    assert got['PREDICTIVE_BALANCING_ENABLED'] is True, "'None' is no boolean: default, not silently false"
    monkeypatch.setattr(cdb, '_connect', lambda: _Conn(store, meta=True))
    meta = {r['key']: r['value'] for r in cdb.get_with_meta(CID)}
    assert meta['DRY_RUN'] is False and meta['AVG_WINDOW_MINUTES'] == 5


@pytest.mark.parametrize('key,value,want', [
    ('DRY_RUN', 'false', False), ('DRY_RUN', False, False), ('DRY_RUN', 'true', True), ('DRY_RUN', 1, True),
    ('AVG_WINDOW_MINUTES', '7', 7), ('AVG_WINDOW_MINUTES', 7.0, 7), ('RAM_LIMIT_PERCENT', 90, 90.0),
    ('EXCLUDE_NODE_IPS', '10.0.0.1, 10.0.0.2', ['10.0.0.1', '10.0.0.2']), ('EXCLUDE_NODE_IPS', '', []),
])
def test_valid_values_are_cast(key, value, want):
    assert _load().validate(key, value) == want


@pytest.mark.parametrize('key,value', [
    ('AVG_WINDOW_MINUTES', None), ('AVG_WINDOW_MINUTES', ''), ('AVG_WINDOW_MINUTES', 'abc'), ('AVG_WINDOW_MINUTES', True),
    ('AVG_WINDOW_MINUTES', 0), ('SAMPLE_INTERVAL_SECONDS', 5), ('RAM_LIMIT_PERCENT', 'nan'), ('RAM_LIMIT_PERCENT', 101),
    ('DRY_RUN', 'maybe'), ('DRY_RUN', None), ('NO_SUCH_KEY', 1), ('EXCLUDE_NODE_IPS', 5),
])
def test_invalid_values_are_refused(key, value):
    with pytest.raises(ValueError):
        _load().validate(key, value)


def test_the_string_false_is_stored_as_false(monkeypatch):
    cdb = _load(); store = {}
    monkeypatch.setattr(cdb, '_connect', lambda: _Conn(store))
    cdb.save_many({'DRY_RUN': 'false'}, CID)
    assert store[(CID, 'DRY_RUN')] == ('false', 'bool')


def test_a_failed_write_is_not_swallowed(monkeypatch):
    cdb = _load()
    monkeypatch.setattr(cdb, '_connect', lambda: _Conn({}, fail=True))
    with pytest.raises(psycopg2.OperationalError):
        cdb.save_many({'DRY_RUN': False}, CID)


def test_the_ranges_follow_the_settings_sliders():
    """RANGES repeats the min/max of every range control in the settings UI; keep the two in step."""
    cdb = _load()
    html = (ROOT / 'dashboard' / 'static' / 'index.html').read_text()
    ui = {k: (float(lo), float(hi)) for k, lo, hi in
          re.findall(r"key: '([A-Z_]+)',\s+label: '[^']*',\s*type: 'range', min:([\d.]+),\s*max:([\d.]+)", html)}
    assert len(ui.keys() & cdb.TUNABLES.keys()) >= 20, 'the slider definitions moved: update this test'
    for key in ui.keys() & cdb.TUNABLES.keys():
        assert tuple(map(float, cdb.RANGES[key])) == ui[key], key


def test_the_config_route_validates_before_storing():
    src = (ROOT / 'dashboard' / 'app.py').read_text()
    body = src[src.index('def post_config'):src.index('# cluster registry API')]
    assert 'silent=True' in body, 'malformed JSON is a 400, not a 500'
    assert body.index('config_db.validate') < body.index('config_db.save_many')
