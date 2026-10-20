"""shared/ modules: crypto round-trip, host normalisation, and copy drift guard."""
import importlib
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'shared'))


def test_shared_copies_are_identical(repo):
    r = subprocess.run([sys.executable, str(repo / 'tools' / 'sync_shared.py'), '--check'],
                       capture_output=True, text=True)
    assert r.returncode == 0, 'run tools/sync_shared.py:\n' + r.stdout


@pytest.fixture
def crypto(monkeypatch):
    monkeypatch.setenv('HCM_SECRET_KEY', 'unit-test-secret-key')
    import hcm_crypto
    return importlib.reload(hcm_crypto)


def test_crypto_roundtrip(crypto):
    token = crypto.encrypt('Z@bbix / ünïcode')
    assert token != 'Z@bbix / ünïcode'
    assert crypto.decrypt(token) == 'Z@bbix / ünïcode'
    assert crypto.key_configured()


def test_crypto_tokens_are_salted(crypto):
    assert crypto.encrypt('x') != crypto.encrypt('x')


def test_crypto_wrong_key_rejected(crypto, monkeypatch):
    token = crypto.encrypt('secret')
    monkeypatch.setenv('HCM_SECRET_KEY', 'another-key')
    with pytest.raises(crypto.InvalidToken):
        crypto.decrypt(token)


def test_crypto_missing_key_is_a_clear_error(monkeypatch):
    monkeypatch.delenv('HCM_SECRET_KEY', raising=False)
    import hcm_crypto
    importlib.reload(hcm_crypto)
    with pytest.raises(hcm_crypto.SecretKeyMissing):
        hcm_crypto.encrypt('x')
    assert not hcm_crypto.key_configured()


@pytest.mark.parametrize('raw, host', [
    ('172.16.0.241', '172.16.0.241'),
    ('  HTTPS://172.16.0.241/rest/v1  ', '172.16.0.241'),
    ('cluster.example.com:8443/', 'cluster.example.com:8443'),
    ('http://Cluster.Example.COM', 'cluster.example.com'),
])
def test_normalize_host_and_base_url(raw, host):
    import cluster_registry as cr
    assert cr.normalize_host(raw) == host
    assert cr.base_url(raw) == f'https://{host}/rest/v1'


def test_is_uuid():
    import cluster_registry as cr
    assert cr.is_uuid(cr.TEMPLATE_CLUSTER_ID)
    assert not cr.is_uuid('default') and not cr.is_uuid('') and not cr.is_uuid(None)


# --- duplicate detection (same HyperCore cluster registered twice) ----------------

def _cluster(cid, name, hc_uuid, registered):
    import cluster_registry as cr
    from datetime import datetime, timezone
    return cr.Cluster(cluster_id=cid, name=name, host='10.0.0.1', username='u', verify_tls=False, enabled=True,
                      is_template=False, hc_cluster_uuid=hc_uuid, colour=None, last_seen_at=None, last_error=None,
                      registered_at=datetime(2026, 10, registered, tzinfo=timezone.utc), updated_at=None)


def test_duplicate_groups_by_hypercore_uuid():
    import cluster_registry as cr
    a = _cluster('a' * 8 + '-0000-0000-0000-000000000001', 'Lab 241', 'hc-1', 1)
    b = _cluster('b' * 8 + '-0000-0000-0000-000000000002', 'Lab 242', 'hc-1', 2)
    c = _cluster('c' * 8 + '-0000-0000-0000-000000000003', 'Prod', 'hc-2', 3)
    d = _cluster('d' * 8 + '-0000-0000-0000-000000000004', 'Unknown yet', None, 4)
    groups = cr.duplicate_groups([c, b, d, a])
    assert len(groups) == 1
    assert [x.name for x in groups[0]] == ['Lab 241', 'Lab 242'], 'oldest registration first'
    assert cr.duplicate_of(b, [a, b, c, d]).name == 'Lab 241'
    assert cr.duplicate_of(a, [a, b, c, d]) is None, 'the keeper is not a duplicate'
    assert cr.duplicate_of(c, [a, b, c, d]) is None
    assert cr.duplicate_groups([a, c, d]) == []
