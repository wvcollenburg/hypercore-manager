"""sentinel/hcb.py and sentinel/anonymiser.py — pure functions, no database."""
import json

import pytest
from cryptography.exceptions import InvalidTag

import hcb
import anonymiser
from anonymiser import AnonConfig, anonymise_snapshot, _pseudonym


# --- .hcb encrypted export ----------------------------------------------------

def test_hcb_roundtrip():
    payload = json.dumps({'metadata': {'cluster_name': 'prod'}, 'forecasts': [1, 2, 3]})
    blob = hcb.encrypt(payload, 'secret-token')
    assert blob[:4] == hcb.MAGIC
    assert len(blob) == 4 + 32 + 12 + len(payload.encode()) + 16
    assert hcb.decrypt(blob, 'secret-token') == payload


def test_hcb_random_salt_and_iv():
    a = hcb.encrypt('{}', 't')
    b = hcb.encrypt('{}', 't')
    assert a != b
    assert a[4:36] != b[4:36] and a[36:48] != b[36:48]


def test_hcb_wrong_token_fails():
    blob = hcb.encrypt('{"x": 1}', 'right')
    with pytest.raises(InvalidTag):
        hcb.decrypt(blob, 'wrong')


def test_hcb_tamper_detected():
    blob = bytearray(hcb.encrypt('{"x": 1}', 't'))
    blob[50] ^= 0xFF
    with pytest.raises(InvalidTag):
        hcb.decrypt(bytes(blob), 't')


def test_hcb_bad_magic():
    with pytest.raises(ValueError):
        hcb.decrypt(b'NOPE' + b'\0' * 80, 't')


def test_hcb_unicode_payload():
    payload = json.dumps({'cluster_name': 'Zürich — prod'}, ensure_ascii=False)
    assert hcb.decrypt(hcb.encrypt(payload, 'k'), 'k') == payload


# --- anonymiser -----------------------------------------------------------------

KEY = 'ab' * 32


def cfg(**kw):
    base = dict(enabled=True, anon_vm_names=False, anon_node_uuids=False, anon_cluster_name=False, hmac_key=KEY)
    base.update(kw)
    return AnonConfig(**base)


def test_pseudonym_is_deterministic_and_prefixed():
    a = _pseudonym(KEY, 'vm', 'DC02')
    assert a == _pseudonym(KEY, 'vm', 'DC02')
    assert a.startswith('vm-') and len(a) == 3 + 8
    assert a != _pseudonym(KEY, 'vm', 'DC03')
    assert a != _pseudonym('cd' * 32, 'vm', 'DC02'), 'different key, different pseudonym'
    assert _pseudonym(KEY, 'node', 'DC02') != _pseudonym(KEY, 'vm', 'DC02'), 'prefix is part of the hash'


SNAPSHOT = {
    'metadata': {'cluster_name': 'prod-cluster', 'cluster_id': 'c-1'},
    'forecasts': [
        {'entity_label': 'Node 3', 'node_uuid': 'nnn-3', 'metric': 'cpu'},
        {'entity_label': 'DC02', 'vm_name': 'DC02', 'metric': 'cpu'},
        {'entity_label': 'DC02 / disk0', 'vm_name': 'DC02', 'metric': 'latency'},
    ],
    'rightsizing': [{'vm_name': 'web01', 'node_uuid': 'nnn-1', 'flag': 'over'}],
    'resize_recommendations': [],
    'cluster_health': [{'score': 91}],
    'exhaustion': [{'entity_label': None, 'vm_name': None}],
}


def test_disabled_config_leaves_snapshot_untouched():
    out = anonymise_snapshot(SNAPSHOT, cfg())
    assert out == SNAPSHOT
    assert out is not SNAPSHOT, 'returns a copy'


def test_vm_names_anonymised_consistently_across_sections():
    out = anonymise_snapshot(SNAPSHOT, cfg(anon_vm_names=True))
    pseud = _pseudonym(KEY, 'vm', 'DC02')
    assert out['forecasts'][1]['vm_name'] == pseud
    assert out['forecasts'][1]['entity_label'] == pseud
    assert out['forecasts'][2]['entity_label'] == f'{pseud} / disk0', 'VSD name kept, VM part hashed'
    assert out['forecasts'][0]['entity_label'] == 'Node 3', 'node labels untouched by vm toggle'
    assert out['rightsizing'][0]['vm_name'] == _pseudonym(KEY, 'vm', 'web01')
    assert out['metadata']['cluster_name'] == 'prod-cluster'
    assert SNAPSHOT['forecasts'][1]['vm_name'] == 'DC02', 'input not mutated'


def test_node_uuids_anonymised():
    out = anonymise_snapshot(SNAPSHOT, cfg(anon_node_uuids=True))
    assert out['forecasts'][0]['node_uuid'] == _pseudonym(KEY, 'node', 'nnn-3')
    # peer id is hashed, the "Node" prefix survives (format shared with Foresight-Central)
    assert out['forecasts'][0]['entity_label'] == f"Node-{_pseudonym(KEY, 'node', '3')}"
    assert out['rightsizing'][0]['node_uuid'] == _pseudonym(KEY, 'node', 'nnn-1')
    assert out['forecasts'][1]['vm_name'] == 'DC02'


def test_cluster_name_anonymised():
    out = anonymise_snapshot(SNAPSHOT, cfg(anon_cluster_name=True))
    assert out['metadata']['cluster_name'] == _pseudonym(KEY, 'cluster', 'prod-cluster')
    assert out['metadata']['cluster_id'] == 'c-1'


def test_none_values_are_skipped():
    out = anonymise_snapshot(SNAPSHOT, cfg(anon_vm_names=True, anon_node_uuids=True))
    assert out['exhaustion'][0] == {'entity_label': None, 'vm_name': None}
