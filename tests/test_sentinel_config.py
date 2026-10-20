"""sentinel/config.py — registry-backed configuration (Config is a frozen dataclass)."""
from types import SimpleNamespace

import config as sentinel_config


def test_load_without_cluster_keeps_template(monkeypatch):
    import cluster_registry
    monkeypatch.setattr(cluster_registry, 'list_enabled', lambda: [])
    cfg = sentinel_config.load()
    assert cfg.cluster_id == '00000000-0000-0000-0000-000000000001'
    assert cfg.hc_host == '' and cfg.hc_username == ''


def test_load_binds_to_registry_cluster(monkeypatch):
    import cluster_registry
    cluster = SimpleNamespace(cluster_id='11111111-2222-3333-4444-555555555555', name='Lab',
                              base_url='https://10.0.0.1/rest/v1', verify_tls=True, username='svc')
    monkeypatch.setattr(cluster_registry, 'credentials', lambda cid: ('svc', 'pw'))
    cfg = sentinel_config.load(cluster)
    assert cfg.cluster_id == cluster.cluster_id
    assert cfg.cluster_name == 'Lab'
    assert cfg.hc_host == 'https://10.0.0.1/rest/v1'
    assert cfg.hc_verify_ssl is True
    assert (cfg.hc_username, cfg.hc_password) == ('svc', 'pw')


def test_load_survives_credential_lookup_failure(monkeypatch):
    import cluster_registry
    cluster = SimpleNamespace(cluster_id='11111111-2222-3333-4444-555555555555', name='Lab',
                              base_url='https://10.0.0.1/rest/v1', verify_tls=False, username='svc')
    def boom(cid): raise RuntimeError('db down')
    monkeypatch.setattr(cluster_registry, 'credentials', boom)
    cfg = sentinel_config.load(cluster)
    assert cfg.hc_username == 'svc' and cfg.hc_password == ''
