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
