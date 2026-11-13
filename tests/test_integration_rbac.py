"""Roles and tenants against a running local stack (plans/PLAN-roles.md).

Needs the stack with the synthetic clusters (tools/fake_hypercore/docker-compose.fake.yaml: fakehc ..
fakehc4) and the demo tenants and users (seeded through the API; see plans/PLAN-roles.md, "Local demo"):

    Northwind IT (MSP) > Contoso > Production (cluster Fake), DR (Contoso-DR)
                       > Fabrikam > Main (Fabrikam-Main)
    Blue Harbor (MSP)  > Tailspin > Ops (Tailspin-Ops)

Run with:  pytest -m integration tests/test_integration_rbac.py
Skipped when the dashboard or the demo users are not there. Password of the demo users: RBAC_DEMO_PW
(default Rbac-demo-2026!).
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

import pytest

BASE = os.environ.get('HCM_BASE', 'http://localhost:5000')
PW = os.environ.get('RBAC_DEMO_PW', 'Rbac-demo-2026!')
pytestmark = pytest.mark.integration

# VM names per cluster: the fakes prefix their VMs (Fake has none)
TENANT_VMS = {
    'Fake': re.compile(r'(?<![a-z-])(web01|db01|dc01|rds01|kiosk01|app02|backup-old)\b'),
    'Contoso-DR': re.compile(r'\bctdr-'),
    'Fabrikam-Main': re.compile(r'\bfab-'),
    'Tailspin-Ops': re.compile(r'\btsp-'),
}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class Client:
    def __init__(self):
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()), _NoRedirect())

    def raw(self, method, path, body=None, form=None):
        data, h = None, {}
        if form is not None:
            data, h['Content-Type'] = urllib.parse.urlencode(form).encode(), 'application/x-www-form-urlencoded'
        elif body is not None:
            data, h['Content-Type'] = json.dumps(body).encode(), 'application/json'
        rq = urllib.request.Request(BASE + path, data=data, method=method, headers=h)
        try:
            with self.op.open(rq, timeout=60) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def call(self, method, path, body=None):
        st, raw = self.raw(method, path, body)
        try:
            return st, json.loads(raw.decode() or 'null')
        except ValueError:
            return st, raw.decode(errors='replace')

    def login(self, user, pw):
        st, _ = self.raw('POST', '/login', form={'username': user, 'password': pw})
        return st == 302 and self.call('GET', '/api/users/me')[0] == 200


def _reachable():
    try:
        urllib.request.urlopen(BASE + '/login', timeout=3)
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture(scope='module')
def admin(repo):
    if not _reachable():
        pytest.skip('dashboard not reachable')
    env = dict(line.split('=', 1) for line in (repo / '.env').read_text().splitlines() if '=' in line and not line.startswith('#'))
    c = Client()
    assert c.login(env['SC_DASHBOARD_USER'], env['SC_DASHBOARD_PASSWORD'])
    return c


@pytest.fixture(scope='module')
def clusters(admin):
    cl = {c['name']: c['cluster_id'] for c in admin.call('GET', '/api/clusters')[1]['clusters']}
    if not all(n in cl for n in TENANT_VMS):
        pytest.skip('the demo clusters are not registered')
    return cl


_sessions: dict = {}


def as_user(name):
    if name not in _sessions:
        c = Client()
        if not c.login(name, PW):
            pytest.skip(f'demo user {name} missing')
        _sessions[name] = c
    return _sessions[name]


def visible(name):
    return {c['name'] for c in as_user(name).call('GET', '/api/clusters')[1]['clusters']}


# ------------------------------------------------------------------ who sees which cluster
@pytest.mark.parametrize('user,expected', [
    ('nw-admin', {'Fake', 'Contoso-DR', 'Fabrikam-Main'}),
    ('nw-noc', {'Fake', 'Contoso-DR', 'Fabrikam-Main'}),
    ('contoso-admin', {'Fake', 'Contoso-DR'}),
    ('contoso-prod-admin', {'Fake'}),
    ('contoso-eng', {'Fake', 'Contoso-DR'}),
    ('contoso-erp', {'Fake'}),
    ('contoso-web', {'Fake'}),
    ('fabrikam-admin', {'Fabrikam-Main'}),
    ('tailspin-admin', {'Tailspin-Ops'}),
    ('rbac-noaccess', set()),
])
def test_cluster_visibility(clusters, user, expected):
    assert visible(user) == expected


# ------------------------------------------------------------------ nothing leaks across tenants
LIST_ENDPOINTS = ['/api/vms', '/api/vsds', '/api/storage/summary', '/api/vm-index', '/api/nodes', '/api/drives',
                  '/api/cluster-alerts', '/api/cluster-alerts/history', '/api/backup/vms', '/api/backup/plans',
                  '/api/backup/policies', '/api/backup/runs', '/api/backup/chains', '/api/backup/restores',
                  '/api/backup/vmedit/changes', '/api/console/sessions', '/api/sentinel/summary', '/api/migrations',
                  '/api/access/overview', '/api/folders', '/api/clusters']


@pytest.mark.parametrize('user', ['contoso-admin', 'contoso-prod-admin', 'contoso-erp', 'contoso-web', 'fabrikam-admin',
                                  'tailspin-admin', 'contoso-viewer', 'contoso-backup'])
def test_no_other_tenant_data_anywhere(clusters, user):
    """Every list endpoint, asked for every cluster (also the ones the user may not see): no VM of a
    cluster outside the user's reach appears in any answer."""
    c = as_user(user)
    mine = visible(user)
    foreign = [n for n in TENANT_VMS if n not in mine]
    for ep in LIST_ENDPOINTS:
        for cname, cid in clusters.items():
            if cname not in TENANT_VMS:
                continue
            st, raw = c.raw('GET', f'{ep}{"&" if "?" in ep else "?"}cluster={cid}')
            text = raw.decode(errors='replace')
            for f in foreign:
                assert not TENANT_VMS[f].search(text), f'{user}: {ep}?cluster={cname} shows a VM of {f}'
                if f in clusters:
                    assert clusters[f] not in text or ep in ('/api/access/overview',), \
                        f'{user}: {ep}?cluster={cname} names cluster {f}'


def test_vm_scoped_user_sees_only_that_vm(clusters):
    c = as_user('contoso-web')
    names = {v['name'] for v in c.call('GET', f"/api/vms?cluster={clusters['Fake']}")[1]}
    assert names <= {'web01'} and 'web01' in names
    idx = {v['name'] for v in c.call('GET', f"/api/vm-index?cluster={clusters['Fake']}")[1]['vms']}
    assert idx == {'web01'}
    st, _ = c.call('GET', f"/api/nodes?cluster={clusters['Fake']}")
    assert st == 403, 'a VM scope never shows the cluster overview'


def test_tag_scoped_user_sees_the_tagged_vms(clusters):
    c = as_user('contoso-erp')
    idx = {v['name'] for v in c.call('GET', f"/api/vm-index?cluster={clusters['Fake']}")[1]['vms']}
    assert idx == {'db01', 'rds01', 'app02'}


def test_console_ticket_follows_the_vm_scope(clusters, admin):
    vms = {v['name']: v['uuid'] for v in admin.call('GET', f"/api/vm-index?cluster={clusters['Fake']}")[1]['vms']}
    c = as_user('contoso-web')
    assert c.call('POST', '/api/console/ticket', {'cluster_id': clusters['Fake'], 'vm_uuid': vms['web01']})[0] == 200
    assert c.call('POST', '/api/console/ticket', {'cluster_id': clusters['Fake'], 'vm_uuid': vms['db01']})[0] == 404
    viewer = as_user('contoso-viewer')
    assert viewer.call('POST', '/api/console/ticket', {'cluster_id': clusters['Fake'], 'vm_uuid': vms['web01']})[0] == 403


def test_app_manager_cannot_power_or_edit(clusters, admin):
    vms = {v['name']: v['uuid'] for v in admin.call('GET', f"/api/vm-index?cluster={clusters['Fake']}")[1]['vms']}
    c = as_user('contoso-erp')
    for kind in ('power', 'edit', 'delete', 'snapshot', 'clone'):
        st, _ = c.call('POST', f"/api/backup/vmedit/changes?cluster={clusters['Fake']}",
                       {'cluster_id': clusters['Fake'], 'vm_uuid': vms['db01'], 'kind': kind, 'ops': []})
        assert st == 403, kind


# ------------------------------------------------------------------ deny by default, every route
def test_every_route_refuses_an_account_without_access(admin, clusters):
    """Route x permission: an enabled account with no assignment gets nothing from any route that
    declares a permission (only routes declared for any logged-in user answer)."""
    routes = admin.call('GET', '/api/access/routes')[1]
    c = as_user('rbac-noaccess')
    checked = 0
    for r in routes['routes']:
        if 'GET' not in r['methods'] or '<' in r['rule'] or not r['rule'].startswith('/api/') or r['permission'] in (None, 'public'):
            continue
        st, _ = c.raw('GET', r['rule'] + f"?cluster={clusters['Fake']}")
        assert st in (403, 404), f"{r['rule']} ({r['permission']}) answered {st} to an account without access"
        checked += 1
    assert checked > 30
    for m, rx, perm, tgt in [(x['methods'], x['path'], x['permission'], x['target']) for x in routes['backup']]:
        if 'GET' not in m or '(' in rx or '|' in rx:
            continue
        st, _ = c.raw('GET', f"/api/backup/{rx}?cluster={clusters['Fake']}")
        assert st in (403, 404), f'backup {rx} answered {st}'


def test_unknown_backup_path_is_refused(admin):
    assert admin.call('GET', '/api/backup/definitely/not/a/route')[0] == 404


# ------------------------------------------------------------------ no escalation
def _role(admin, key):
    return next(r['role_id'] for r in admin.call('GET', '/api/access/overview')[1]['roles'] if r['builtin_key'] == key)


def _node(admin, name):
    return next(n['id'] for n in admin.call('GET', '/api/access/overview')[1]['nodes'] if n['name'] == name)


def _uid(admin, name):
    return next(u['user_id'] for u in admin.call('GET', '/api/access/overview')[1]['users'] if u['username'] == name)


def test_customer_admin_cannot_escalate(admin, clusters):
    c = as_user('contoso-admin')
    viewer = _uid(admin, 'contoso-viewer')
    # above their own seat
    st, d = c.call('POST', '/api/access/assignments', {'user_id': viewer, 'role_id': _role(admin, 'admin'),
                                                       'scope': {'kind': 'node', 'node_id': _node(admin, 'Northwind IT')}})
    assert st == 403, d
    # another customer
    st, d = c.call('POST', '/api/access/assignments', {'user_id': viewer, 'role_id': _role(admin, 'viewer'),
                                                       'scope': {'kind': 'node', 'node_id': _node(admin, 'Main')}})
    assert st == 403, d
    # the whole installation
    st, d = c.call('POST', '/api/access/assignments', {'user_id': viewer, 'role_id': _role(admin, 'viewer'), 'scope': {'kind': 'root'}})
    assert st == 403, d
    # their own access
    me = _uid(admin, 'contoso-admin')
    st, d = c.call('POST', '/api/access/assignments', {'user_id': me, 'role_id': _role(admin, 'viewer'),
                                                       'scope': {'kind': 'node', 'node_id': _node(admin, 'DR')}})
    assert st == 409, d
    # a user of another customer is not theirs to manage
    st, d = c.call('GET', f"/api/access/users/{_uid(admin, 'fabrikam-admin')}")
    assert st == 404


def test_engineer_cannot_manage_users(admin):
    c = as_user('contoso-eng')
    st, _ = c.call('POST', '/api/access/assignments', {'user_id': _uid(admin, 'contoso-viewer'), 'role_id': _role(admin, 'viewer'),
                                                       'scope': {'kind': 'node', 'node_id': _node(admin, 'DR')}})
    assert st == 403
    assert c.call('GET', '/api/access/overview')[1]['users'] == []


def test_folder_admin_cannot_delete_its_own_folder(admin):
    c = as_user('contoso-prod-admin')
    st, _ = c.call('DELETE', f"/api/access/nodes/{_node(admin, 'Production')}")
    assert st == 403


def test_customer_admin_custom_role_only_with_held_permissions(admin):
    c = as_user('contoso-admin')
    st, d = c.call('POST', '/api/access/roles', {'name': 'too-much', 'node_id': _node(admin, 'Contoso'),
                                                 'permissions': ['install.manage']})
    assert st == 403, d
    st, d = c.call('POST', '/api/access/roles', {'name': 'above-me', 'node_id': _node(admin, 'Northwind IT'), 'permissions': ['vm.view']})
    assert st == 403, d


# ------------------------------------------------------------------ IDOR: ids of another tenant answer 404
def test_foreign_change_id_is_not_found(admin, clusters):
    vms = {v['name']: v['uuid'] for v in admin.call('GET', f"/api/vm-index?cluster={clusters['Fabrikam-Main']}")[1]['vms']}
    st, d = admin.call('POST', f"/api/backup/vmedit/changes?cluster={clusters['Fabrikam-Main']}",
                       {'cluster_id': clusters['Fabrikam-Main'], 'vm_uuid': vms['fab-web01'], 'kind': 'power',
                        'ops': [{'op': 'power', 'action': 'REBOOT'}], 'run_at': '2099-01-01T03:00:00+00:00'})
    assert st == 200, d
    cid = d['id']
    try:
        st, _ = as_user('contoso-admin').call('POST', f"/api/backup/vmedit/changes/{cid}/cancel?cluster={clusters['Fake']}",
                                              {'cluster_id': clusters['Fake']})
        assert st == 404
        st, _ = as_user('fabrikam-admin').call('POST', f"/api/backup/vmedit/changes/{cid}/cancel?cluster={clusters['Fabrikam-Main']}",
                                               {'cluster_id': clusters['Fabrikam-Main']})
        assert st == 200
    finally:
        admin.call('POST', f"/api/backup/vmedit/changes/{cid}/cancel?cluster={clusters['Fabrikam-Main']}", {'cluster_id': clusters['Fabrikam-Main']})


@pytest.mark.parametrize('path', ['/api/backup/plans/1/toggle', '/api/backup/chains/1/verify', '/api/backup/proxies/1/test'])
def test_resource_ids_are_checked(clusters, path):
    """Ids that do not exist, or belong to another tenant, all answer 404 to a tenant user."""
    st, _ = as_user('fabrikam-admin').call('POST', path + f"?cluster={clusters['Fabrikam-Main']}", {})
    assert st == 404


def test_reads_with_a_foreign_cluster_never_show_it(clusters):
    """A cluster named in a read that the user may not use falls back to one of theirs (the UI recovers
    after a cluster was taken away); changes are refused."""
    c = as_user('fabrikam-admin')
    st, rows = c.call('GET', f"/api/vms?cluster={clusters['Tailspin-Ops']}")
    assert st == 200 and all(r['name'].startswith('fab-') for r in rows)
    st, _ = c.call('POST', f"/api/config?cluster={clusters['Tailspin-Ops']}", {'DRY_RUN': 'true'})
    assert st == 403


# ------------------------------------------------------------------ the tag guard
def test_tag_that_gives_access_cannot_be_moved_by_an_engineer(admin, clusters):
    vms = {v['name']: v for v in admin.call('GET', f"/api/vm-index?cluster={clusters['Fake']}")[1]['vms']}
    web = vms['web01']
    eng = as_user('contoso-eng')
    tags = ','.join(web['tags'] + ['erp'])                  # erp gives contoso-erp its access
    st, d = eng.call('POST', f"/api/backup/vmedit/changes?cluster={clusters['Fake']}",
                     {'cluster_id': clusters['Fake'], 'vm_uuid': web['uuid'], 'kind': 'edit',
                      'ops': [{'op': 'vm', 'fields': {'tags': tags}}], 'run_at': '2099-01-01T03:00:00+00:00'})
    assert st == 403 and 'erp' in d['error']
    st, d = eng.call('GET', f"/api/access/scoped-tags?cluster={clusters['Fake']}")
    assert st == 200 and 'erp' in d['tags'] and d['may_change'] is False and d['tags']['erp'] == []
