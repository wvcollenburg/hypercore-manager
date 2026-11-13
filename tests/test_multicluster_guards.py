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


def _ranges_followed_by_cluster_filter(src: str, *filter_markers: str) -> tuple[int, int]:
    ranges = list(re.finditer(r'\|> range\(start: [^\n]*\)\n', src))
    ok = sum(1 for m in ranges if any(f in src[m.end(): m.end() + 120] for f in filter_markers))
    return len(ranges), ok


def test_dashboard_flux_queries_filter_on_cluster(repo):
    src = (repo / 'dashboard' / 'app.py').read_text()
    # one cluster (the selected one), or several for the alert badges of every visible cluster
    total, ok = _ranges_followed_by_cluster_filter(src, '{_flux_cluster()}', '{_flux_clusters(cids)}')
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
    # the dashboard builds from the repository root (it carries docs/manual), so paths are prefixed
    assert 'COPY dashboard/cluster_registry.py' in df and 'COPY dashboard/hcm_crypto.py' in df


def test_compose_passes_secret_key_to_every_service(repo):
    text = (repo / 'docker-compose.yaml').read_text()
    for svc in ('collector', 'balancer', 'dashboard', 'sentinel'):
        after = text.split(f'\n  {svc}:\n', 1)[1]
        block = re.split(r'\n  [a-z-]+:\n', after, maxsplit=1)[0]   # up to the next service header
        assert 'HCM_SECRET_KEY=${HCM_SECRET_KEY}' in block, f'{svc} lacks HCM_SECRET_KEY'


def test_dashboard_sentinel_proxies_pass_the_cluster(repo):
    src = (repo / 'dashboard' / 'app.py').read_text()
    urls = re.findall(r"f'\{sentinel_api_url\}/api/v1/[^'\n]*'", src)
    assert len(urls) >= 5           # vms, whatif, tunables (2), trend/run; the sharing proxies are gone
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


def test_folder_rbac_wiring(repo):
    sql = (repo / 'postgres' / 'init' / '021_folders_rbac.sql').read_text()
    assert 'config.folders' in sql and 'config.folder_members' in sql and 'folder_id' in sql
    app = (repo / 'dashboard' / 'app.py').read_text()
    for route in ("'/api/folders', methods=['GET']", "'/api/folders', methods=['POST']",
                  "'/api/folders/<folder_id>', methods=['DELETE']", "'/api/folders/<folder_id>/members', methods=['PUT']"):
        assert route in app, route
    # every data call resolves the cluster through the visibility filter
    assert 'def _visible_clusters' in app and 'visible = _visible_clusters()' in app
    reg = (repo / 'shared' / 'cluster_registry.py').read_text()
    for fn in ('def list_folders', 'def create_folder', 'def delete_folder', 'def set_folder_member', 'def folder_ids_for_user'):
        assert fn in reg, fn


def test_backup_module_wiring(repo):
    assert (repo / 'postgres' / 'init' / '022_exports_schema.sql').exists()
    for f in ('Dockerfile', 'requirements.txt', 'backup.py', 'api.py', 'jobs.py', 'scheduler.py', 'db.py', 'hypercore.py',
              'cluster_registry.py', 'hcm_crypto.py'):
        assert (repo / 'backup' / f).exists(), f
    compose = (repo / 'docker-compose.yaml').read_text()
    assert '\n  backup:\n' in compose and 'BACKUP_API_URL=http://backup:8082' in compose
    app = (repo / 'dashboard' / 'app.py').read_text()
    assert "@app.route('/api/backup/<path:sub>'" in app and "'/api/backup/status'" in app
    for line in (repo / 'backup' / 'requirements.txt').read_text().splitlines():
        if line.strip() and not line.startswith('#'):
            assert '==' in line, f'backup/requirements.txt: {line!r} is not pinned'


def test_backup_targets_wiring(repo):
    assert (repo / 'postgres' / 'init' / '023_exports_shares.sql').exists()
    assert (repo / 'postgres' / 'init' / '025_backup_rename.sql').exists()
    api = (repo / 'backup' / 'api.py').read_text()
    for route in ("@app.get('/api/v1/targets')", "@app.post('/api/v1/targets')", "@app.delete('/api/v1/targets/<int:sid>')", "@app.post('/api/v1/targets/<int:sid>/test')"):
        assert route in api, route
    app = (repo / 'dashboard' / 'app.py').read_text()
    assert "if sub.startswith(('targets', 'proxies', 'vmedit')) and not _is_admin_request()" in app, \
        'target and proxy management, and VM edits, must stay admin-only'
    reg = (repo / 'shared' / 'cluster_registry.py').read_text()
    assert 'share_id' in reg


def test_backup_forwarding_sends_only_the_validated_cluster(repo):
    """The backup service reads the first `cluster` parameter. The dashboard used to forward the
    browser's query string and append the validated cluster after it, so a folder member could read
    another cluster's backups by naming it. Only the validated one may be forwarded."""
    app = (repo / 'dashboard' / 'app.py').read_text()
    body = app[app.index('def backup_proxy(sub):'):]
    body = body[:body.index('\n@app.')] if '\n@app.' in body else body
    assert 'request.query_string' not in body
    assert "if k != 'cluster'] + [('cluster', cid)]" in body
    assert "payload['muted_by'] = session.get('username')" in body, 'who muted a notice comes from the session'


def test_cluster_notice_mutes_migration(repo):
    sql = (repo / 'postgres' / 'init' / '040_backup_notice_mutes.sql').read_text()
    assert 'CREATE TABLE IF NOT EXISTS backup.notice_mutes' in sql and 'PRIMARY KEY (cluster_id, key)' in sql


def _forwarding():
    import importlib.util, pathlib
    p = pathlib.Path(__file__).resolve().parent.parent / 'dashboard' / 'forwarding.py'
    spec = importlib.util.spec_from_file_location('hcm_dashboard_forwarding', p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_a_cluster_chosen_in_a_form_is_checked_not_replaced():
    """2026-11-12: a proxy deployed for the second cluster landed on the first. The dashboard
    replaced every body's cluster_id with the cluster selected in the sidebar, so the deploy form's
    own cluster choice (and the proxy registration form's) never reached the backup service."""
    f = _forwarding()
    seen = {'c1', 'c2'}
    assert f.body_cluster({'cluster_id': 'c2'}, 'c1', seen, fill=True) == ('c2', None), 'the form chose c2'
    assert f.body_cluster({'cluster_id': ' c2 '}, 'c1', seen, fill=True) == ('c2', None)
    cid, err = f.body_cluster({'cluster_id': 'c3'}, 'c1', seen, fill=True)
    assert cid is None and err, 'a cluster the user may not see is refused, never swapped for another'
    assert f.body_cluster({'cluster_id': ''}, 'c1', seen, fill=True) == ('c1', None), 'empty: the selected one, as before'
    assert f.body_cluster({}, 'c1', seen, fill=True) == ('c1', None), 'plans and imports get the selected cluster'
    assert f.body_cluster({'name': 'x'}, 'c1', seen, fill=False) == (None, None), 'nothing added where none is needed'


def test_backup_forwarding_uses_the_cluster_check(repo):
    app = (repo / 'dashboard' / 'app.py').read_text()
    body = app[app.index('def backup_proxy(sub):'):]
    body = body[:body.index('\n@app.')] if '\n@app.' in body else body
    assert 'body_cluster(payload, cid, {c.cluster_id for c in _visible_clusters()}' in body
    assert "payload['cluster_id'] = cid" not in body, 'the old unconditional override'
    assert 'COPY dashboard/forwarding.py .' in (repo / 'dashboard' / 'Dockerfile').read_text(), 'the image needs the module'
