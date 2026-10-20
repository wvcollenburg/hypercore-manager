"""Repository guards for the multi-cluster model: every InfluxDB write is tagged with the
cluster, every dashboard and Sentinel Flux query filters on it, and no service reads
cluster credentials from the environment any more."""
import re

import pytest


def test_every_collector_point_carries_cluster_tag(repo):
    src = (repo / 'Collector' / 'collector.py').read_text()
    points = re.findall(r'Point\("([a-z_]+)"\)(.{0,40})', src)
    assert points, 'no Point( calls found'
    untagged = [name for name, tail in points if '.tag("cluster_id", cid)' not in tail]
    assert untagged == [], f'points without cluster tag: {untagged}'


def test_balancer_influx_writes_carry_cluster_tag(repo):
    for rel in ('Balancer/HyperCore_balancer.py', 'Balancer/predictive_engine.py'):
        src = (repo / rel).read_text()
        for m in re.finditer(r'Point\("([a-z_]+)"\)', src):
            window = src[m.end(): m.end() + 120]
            assert 'cluster_id' in window, f'{rel}: Point("{m.group(1)}") not tagged with cluster_id'


def _ranges_followed_by_cluster_filter(src: str, filter_marker: str) -> tuple[int, int]:
    ranges = list(re.finditer(r'\|> range\(start: [^\n]*\)\n', src))
    ok = sum(1 for m in ranges if filter_marker in src[m.end(): m.end() + 120])
    return len(ranges), ok


def test_dashboard_flux_queries_filter_on_cluster(repo):
    src = (repo / 'dashboard' / 'app.py').read_text()
    total, ok = _ranges_followed_by_cluster_filter(src, '{_flux_cluster()}')
    assert total >= 13 and ok == total, f'{ok}/{total} dashboard queries carry the cluster filter'


def test_sentinel_flux_queries_filter_on_cluster(repo):
    src = (repo / 'sentinel' / 'influx_client.py').read_text()
    total, ok = _ranges_followed_by_cluster_filter(src, 'r.cluster_id == "{self._cluster_id}"')
    assert total >= 13 and ok == total, f'{ok}/{total} sentinel queries carry the cluster filter'


def test_predictive_engine_filters_on_cluster(repo):
    src = (repo / 'Balancer' / 'predictive_engine.py').read_text()
    assert 'r["cluster_id"] == "{_sanitize_uuid(cluster_id)}"' in src


@pytest.mark.parametrize('rel', [
    'Collector/collector.py', 'Balancer/HyperCore_balancer.py', 'Balancer/predictive_engine.py',
    'sentinel/config.py', 'sentinel/outcome_tracker.py', 'dashboard/app.py', 'docker-compose.yaml',
])
def test_no_cluster_credentials_from_environment(repo, rel):
    src = (repo / rel).read_text()
    for var in ('SC_HOST', 'SC_USERNAME', 'SC_PASSWORD', 'SC_VERIFY_SSL'):
        # SC_HOST_REPO_PATH (updater) is unrelated and allowed
        assert not re.search(var + r'(?![_A-Z])', src), f'{rel} still reads {var}'


def test_no_hardcoded_default_cluster_in_sentinel(repo):
    for p in (repo / 'sentinel').glob('*.py'):
        src = p.read_text()
        assert "cluster_id = 'default'" not in src and "cluster_id='default'" not in src, p.name


def test_all_services_ship_shared_modules(repo):
    for d in ('Collector', 'Balancer', 'dashboard', 'sentinel'):
        for mod in ('cluster_registry.py', 'hcm_crypto.py'):
            assert (repo / d / mod).exists(), f'{d}/{mod} missing (run tools/sync_shared.py)'
    df = (repo / 'dashboard' / 'Dockerfile').read_text()
    assert 'COPY cluster_registry.py' in df and 'COPY hcm_crypto.py' in df


def test_compose_passes_secret_key_to_every_service(repo):
    text = (repo / 'docker-compose.yaml').read_text()
    for svc in ('collector', 'balancer', 'dashboard', 'sentinel'):
        after = text.split(f'\n  {svc}:\n', 1)[1]
        block = re.split(r'\n  [a-z-]+:\n', after, maxsplit=1)[0]   # up to the next service header
        assert 'HCM_SECRET_KEY=${HCM_SECRET_KEY}' in block, f'{svc} lacks HCM_SECRET_KEY'


def test_dashboard_sentinel_proxies_pass_the_cluster(repo):
    src = (repo / 'dashboard' / 'app.py').read_text()
    urls = re.findall(r"f'\{sentinel_api_url\}/api/v1/[^'\n]*'", src)
    assert len(urls) >= 10
    missing = [u for u in urls if 'cluster={_cluster_id()}' not in u]
    assert missing == [], missing


def test_sentinel_api_resolves_cluster_per_request(repo):
    src = (repo / 'sentinel' / 'api.py').read_text()
    assert "request.args.get('cluster')" in src
    assert 'SENTINEL_CFGS' in src
    assert 'trigger(_cfg().cluster_id)' in src


def test_sentinel_runs_one_engine_per_cluster(repo):
    src = (repo / 'sentinel' / 'sentinel.py').read_text()
    assert 'class ClusterEngine' in src and 'def _reconcile_engines' in src
    assert '_trigger_event' not in src, 'global trigger replaced by per-engine events'
