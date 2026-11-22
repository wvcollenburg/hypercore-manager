"""sentinel/outcome_tracker.py — the 'before' vCPU lookup (C7)."""
import logging
from datetime import datetime, timedelta, timezone

import influxdb_client

import config as sentinel_config
import outcome_tracker

CID = '11111111-1111-1111-1111-111111111111'
VM = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'


class _Record:
    def __init__(self, v): self.values = {'_value': v}


class _Table:
    def __init__(self, v): self.records = [_Record(v)]


def _fake_client(queries, answer=4, fail=False):
    class Client:
        def __init__(self, **kw): pass
        def query_api(self): return self
        def query(self, flux, org=None):
            queries.append(flux)
            if fail:
                raise RuntimeError('error calling function "subtract"')
            return [_Table(answer)]
    return Client


def test_the_before_vcpu_query_is_valid_flux_for_this_cluster(monkeypatch):
    """The query used duration.subtract/add, which Flux does not have: every lookup failed, before_vcpu was
    None and every outcome became 'no_action', never to be checked again."""
    queries = []
    monkeypatch.setattr(influxdb_client, 'InfluxDBClient', _fake_client(queries))
    cfg = sentinel_config.Config(cluster_id=CID)
    at = datetime(2026, 11, 20, 13, 30, tzinfo=timezone(timedelta(hours=1)))     # 12:30 UTC
    assert outcome_tracker._fetch_before_vcpu(cfg, VM, at) == 4
    (flux,) = queries
    assert 'duration.' not in flux
    assert 'range(start: 2026-11-20T11:30:00Z, stop: 2026-11-20T13:30:00Z)' in flux
    assert f'r.cluster_id == "{CID}"' in flux and f'r.vm_uuid == "{VM}"' in flux


def test_a_failed_lookup_is_logged_as_a_warning(monkeypatch, caplog):
    monkeypatch.setattr(influxdb_client, 'InfluxDBClient', _fake_client([], fail=True))
    with caplog.at_level(logging.WARNING, logger=outcome_tracker.log.name):
        assert outcome_tracker._fetch_before_vcpu(sentinel_config.Config(cluster_id=CID), VM,
                                                  datetime.now(timezone.utc)) is None
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_a_malformed_vm_uuid_never_reaches_the_query(monkeypatch):
    queries = []
    monkeypatch.setattr(influxdb_client, 'InfluxDBClient', _fake_client(queries))
    assert outcome_tracker._fetch_before_vcpu(sentinel_config.Config(cluster_id=CID), 'x") or true //',
                                              datetime.now(timezone.utc)) is None
    assert queries == []
