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
DEMO_DOMAIN = 'demo.example'      # accounts are email addresses: demo user 'contoso-admin' logs in as contoso-admin@demo.example


def email(name):
    return name if '@' in name else f'{name}@{DEMO_DOMAIN}'


def short(username):
    """A demo account's address back to its short name (the tests speak of 'contoso-admin')."""
    return username[:-len(DEMO_DOMAIN) - 1] if username and username.endswith('@' + DEMO_DOMAIN) else username


def as_user(name):
    if name not in _sessions:
        c = Client()
        if not c.login(email(name), PW):
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
    return next(u['user_id'] for u in admin.call('GET', '/api/access/overview')[1]['users'] if u['username'] == email(name))


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


# ------------------------------------------------------------------ custom roles, groups, copying access (R3)
def test_customer_admin_makes_and_uses_a_role(admin, clusters):
    c = as_user('contoso-admin')
    contoso = _node(admin, 'Contoso')
    st, d = c.call('POST', '/api/access/roles', {'name': 'Contoso console only', 'node_id': contoso,
                                                 'permissions': ['vm.console'], 'description': 'test role'})
    assert st in (201, 409), d
    rid = next(r['role_id'] for r in c.call('GET', '/api/access/overview')[1]['roles'] if r['name'] == 'Contoso console only')
    role = next(r for r in c.call('GET', '/api/access/overview')[1]['roles'] if r['role_id'] == rid)
    assert set(role['permissions']) == {'vm.console', 'vm.view'}, 'implications are stored with it'
    viewer = _uid(admin, 'contoso-viewer')
    st, d = c.call('POST', '/api/access/assignments', {'user_id': viewer, 'role_id': rid, 'scope': {'kind': 'node', 'node_id': _node(admin, 'DR')}})
    assert st == 201, d
    aid = d['assignment_id']
    try:
        # the role exists for Contoso only: even the MSP admin cannot use it at Fabrikam
        st, d = as_user('nw-admin').call('POST', '/api/access/assignments', {'user_id': _uid(admin, 'fabrikam-admin'), 'role_id': rid,
                                                                             'scope': {'kind': 'node', 'node_id': _node(admin, 'Main')}})
        assert st == 403 and 'not defined' in d['error']
        # in use: cannot be deleted
        assert c.call('DELETE', f'/api/access/roles/{rid}')[0] == 409
    finally:
        assert c.call('DELETE', f'/api/access/assignments/{aid}')[0] == 200
    assert c.call('DELETE', f'/api/access/roles/{rid}')[0] == 200


def test_builtin_roles_are_read_only(admin):
    rid = _role(admin, 'viewer')
    st, d = admin.call('PUT', f'/api/access/roles/{rid}', {'permissions': ['vm.view', 'vm.console']})
    assert st == 409 and 'template' in d['error']
    assert admin.call('DELETE', f'/api/access/roles/{rid}')[0] == 409


def test_group_membership_follows_the_no_escalation_rule(admin):
    c = as_user('contoso-admin')
    gid = next(g['group_id'] for g in c.call('GET', '/api/access/overview')[1]['groups'] if g['name'] == 'Contoso helpdesk')
    st, _ = c.call('PUT', f'/api/access/groups/{gid}/members', {'user_id': _uid(admin, 'fabrikam-admin'), 'member': True})
    assert st == 404, 'a user of another customer cannot be pulled into a Contoso group'
    st, _ = as_user('contoso-eng').call('PUT', f'/api/access/groups/{gid}/members', {'user_id': _uid(admin, 'contoso-viewer'), 'member': True})
    assert st == 403


def test_copy_vm_access_to_a_new_vm(admin, clusters):
    vms = {v['name']: v['uuid'] for v in admin.call('GET', f"/api/vm-index?cluster={clusters['Fake']}")[1]['vms']}
    c = as_user('contoso-admin')
    st, d = c.call('POST', '/api/access/copy-vm-access', {'cluster_id': clusters['Fake'], 'from_vm': vms['web01'], 'to_vm': vms['kiosk01']})
    assert st == 200 and any('contoso-web' in x for x in d['copied']), d
    try:
        idx = {v['name'] for v in as_user('contoso-web').call('GET', f"/api/vm-index?cluster={clusters['Fake']}")[1]['vms']}
        assert idx == {'web01', 'kiosk01'}
    finally:
        uid = _uid(admin, 'contoso-web')
        for a in admin.call('GET', f'/api/access/users/{uid}')[1]['assignments']:
            if a.get('vm_uuid') == vms['kiosk01']:
                admin.call('DELETE', f"/api/access/assignments/{a['assignment_id']}")
    # outside their reach
    assert as_user('fabrikam-admin').call('POST', '/api/access/copy-vm-access',
                                          {'cluster_id': clusters['Fake'], 'from_vm': vms['web01'], 'to_vm': vms['kiosk01']})[0] == 404


def test_tenant_browser_pane(admin, clusters):
    """Settings → Access → Tenants: what is assigned on a layer and what it inherits, never across tenants."""
    nodes = {n['name']: n['id'] for n in admin.call('GET', '/api/access/overview')[1]['nodes']}
    names = lambda rows: {short(r['username']) or r['group_name'] for r in rows}  # noqa: E731
    st, d = admin.call('GET', f"/api/access/at?node={nodes['Production']}&cluster={clusters['Fake']}")
    assert st == 200 and d['cluster_id'] is None, 'a node wins over the cluster apiFetch adds'
    assert {'contoso-prod-admin', 'Contoso helpdesk'} <= names(d['here'])
    assert {'contoso-admin', 'nw-admin'} <= names(d['above']) and 'tailspin-admin' not in names(d['above'])
    st, d = as_user('contoso-admin').call('GET', f"/api/access/at?cluster={clusters['Fake']}")
    assert st == 200 and {'contoso-erp', 'contoso-web'} <= names(d['here'])
    assert as_user('contoso-admin').call('GET', f"/api/access/at?node={nodes['Tailspin']}")[0] == 404
    assert as_user('contoso-admin').call('GET', f"/api/access/at?node={nodes['Fabrikam']}")[0] == 404
    assert as_user('contoso-eng').call('GET', f"/api/access/at?node={nodes['Contoso']}")[0] == 403


def test_the_layers_above_stay_hidden(admin, clusters):
    """A customer's people never see the MSP above them: not in the tree, not in a label, not its staff."""
    c = as_user('contoso-admin')
    ov = c.call('GET', '/api/access/overview')[1]
    assert 'Northwind IT' not in {n['name'] for n in ov['nodes']}
    top = next(n for n in ov['nodes'] if n['name'] == 'Contoso')
    assert top['parent_id'] is None
    nodes = {n['name']: n['id'] for n in ov['nodes']}
    d = c.call('GET', f"/api/access/at?node={nodes['Production']}")[1]
    blob = json.dumps(d)
    assert 'Northwind' not in blob and 'nw-admin' not in blob and 'nw-noc' not in blob
    assert 'contoso-admin' in blob, 'the layers of their own reach are still shown'
    who = json.dumps(c.call('GET', f"/api/access/who?cluster={clusters['Fake']}")[1])
    assert 'nw-' not in who and 'Northwind' not in who and '"admin"' not in who
    assert 'Northwind' not in json.dumps(c.call('GET', '/api/access/audit')[1])
    assert 'Northwind' not in json.dumps(c.call('GET', '/api/clusters')[1]['folders'])
    nw_id = next(n['id'] for n in admin.call('GET', '/api/access/overview')[1]['nodes'] if n['name'] == 'Northwind IT')
    assert nw_id not in json.dumps(c.call('GET', '/api/clusters')[1]['folders']), 'not even the id of the MSP'
    assert nw_id not in json.dumps(c.call('GET', '/api/access/audit')[1])
    for uid in [u['user_id'] for u in ov['users']][:5]:
        assert 'Northwind' not in json.dumps(c.call('GET', f'/api/access/users/{uid}')[1])
    # the MSP's own admin still sees its layer
    assert 'Northwind IT' in {n['name'] for n in as_user('nw-admin').call('GET', '/api/access/overview')[1]['nodes']}


def test_the_last_root_admin_cannot_be_removed(admin):
    me = admin.call('GET', '/api/users/me')[1]
    rows = admin.call('GET', f"/api/access/users/{me['user_id']}")[1]['assignments']
    root = next(a for a in rows if a['scope_kind'] == 'root')
    st, _ = admin.call('DELETE', f"/api/access/assignments/{root['assignment_id']}")
    assert st == 409, 'nobody changes their own access (and the last root admin stays)'


def test_a_removed_right_is_gone_on_every_worker(admin, clusters):
    """The dashboard runs several gunicorn workers, each with caches; a change bumps a shared version, so a
    removed right is refused by all of them within about a second."""
    import time
    vms = {v['name']: v['uuid'] for v in admin.call('GET', f"/api/vm-index?cluster={clusters['Fake']}")[1]['vms']}
    uid = _uid(admin, 'contoso-viewer')
    st, d = admin.call('POST', '/api/access/assignments', {'user_id': uid, 'role_id': _role(admin, 'app_manager'),
                                                           'scope': {'kind': 'vm', 'cluster_id': clusters['Fake'], 'vm_uuid': vms['dc01']}})
    assert st == 201, d
    c = as_user('contoso-viewer')
    time.sleep(1.2)
    assert all(c.call('POST', '/api/console/ticket', {'cluster_id': clusters['Fake'], 'vm_uuid': vms['dc01']})[0] == 200 for _ in range(6))
    assert admin.call('DELETE', f"/api/access/assignments/{d['assignment_id']}")[0] == 200
    time.sleep(1.2)
    codes = [c.call('POST', '/api/console/ticket', {'cluster_id': clusters['Fake'], 'vm_uuid': vms['dc01']})[0] for _ in range(12)]
    assert all(x in (403, 404) for x in codes), codes


# ------------------------------------------------------------------ guest console access (R4)
def _guest_body(clusters, vms, vm, **kw):
    import datetime as dt
    now = dt.datetime.now(dt.timezone.utc)
    b = {'cluster_id': clusters['Fake'], 'vm_uuid': vms[vm], 'guest_name': 'Test Guest', 'guest_company': 'Outside BV',
         'valid_from': now.isoformat(), 'valid_until': (now + dt.timedelta(hours=1)).isoformat(), 'max_minutes': 30,
         'max_sessions': 1, 'label': 'Maintenance'}
    b.update(kw)
    return b


@pytest.fixture(scope='module')
def fake_vms(admin, clusters):
    return {v['name']: v['uuid'] for v in admin.call('GET', f"/api/vm-index?cluster={clusters['Fake']}")[1]['vms']}


def test_guest_link_code_and_one_console(admin, clusters, fake_vms):
    ca = as_user('contoso-admin')
    st, g = ca.call('POST', '/api/guest/grants', _guest_body(clusters, fake_vms, 'web01'))
    assert st == 201, g
    token = g['link'].rsplit('/', 1)[1]
    try:
        guest = Client()
        st, html = guest.raw('GET', f'/guest/{token}')
        assert st == 200 and b'name="code"' in html and b'web01' not in html
        assert guest.raw('POST', f'/guest/{token}', form={'code': '000000'})[0] == 401
        assert guest.raw('POST', f'/guest/{token}', form={'code': g['code']})[0] == 303
        st, html = guest.raw('GET', f'/guest/{token}')
        assert st == 200 and b'/guest/ws' in html
        assert b'web01' not in html and clusters['Fake'].encode() not in html and fake_vms['web01'].encode() not in html
        st, t1 = guest.call('POST', f'/guest/{token}/ticket', {})
        assert st == 200 and set(t1) == {'ticket', 'max_minutes', 'idle_minutes'}, 'no VM name, no cluster'
        assert guest.call('POST', f'/guest/{token}/ticket', {})[0] == 403, 'one session only'
        # the guest cookie opens nothing else
        for path in ('/api/vms', '/api/clusters', '/api/users/me', f"/api/vm-info?vm={fake_vms['web01']}", '/'):
            assert guest.raw('GET', path)[0] in (302, 401), path
        # a second browser with the right code takes over; the first one's cookie stops working
        other = Client()
        assert other.raw('POST', f'/guest/{token}', form={'code': g['code']})[0] == 303
        assert guest.raw('GET', f'/guest/{token}')[1].count(b'name="code"') == 1
    finally:
        ca.call('POST', f"/api/guest/grants/{g['id']}/revoke", {})
    assert Client().raw('GET', f'/guest/{token}')[0] == 404, 'a revoked link is dead'


def test_guest_only_onto_a_locked_console(clusters, fake_vms):
    ca = as_user('contoso-admin')
    st, g = ca.call('POST', '/api/guest/grants', _guest_body(clusters, fake_vms, 'db01'))      # db01 shows a logged-in shell
    token = g['link'].rsplit('/', 1)[1]
    try:
        guest = Client()
        guest.raw('POST', f'/guest/{token}', form={'code': g['code']})
        st, d = guest.call('POST', f'/guest/{token}/ticket', {})
        assert st == 409 and d.get('not_ready'), d
    finally:
        ca.call('POST', f"/api/guest/grants/{g['id']}/revoke", {})


def test_guest_code_locks_after_five_tries(clusters, fake_vms):
    ca = as_user('contoso-admin')
    st, g = ca.call('POST', '/api/guest/grants', _guest_body(clusters, fake_vms, 'dc01'))
    token = g['link'].rsplit('/', 1)[1]
    try:
        guest = Client()
        for _ in range(5):
            st, _ = guest.raw('POST', f'/guest/{token}', form={'code': '999999'})
        assert st == 403
        assert guest.raw('POST', f'/guest/{token}', form={'code': g['code']})[0] == 403, 'locked: even the right code'
        st, r = ca.call('POST', f"/api/guest/grants/{g['id']}/reissue", {})
        assert st == 200
        assert Client().raw('GET', f'/guest/{token}')[0] == 404, 'the old link died with the reissue'
        assert Client().raw('POST', f"/guest/{r['link'].rsplit('/', 1)[1]}", form={'code': r['code']})[0] == 303
    finally:
        ca.call('POST', f"/api/guest/grants/{g['id']}/revoke", {})


def test_guest_request_and_approval(clusters, fake_vms):
    erp = as_user('contoso-erp')                       # App Manager on tag erp: may ask, not give
    st, rq = erp.call('POST', '/api/guest/requests', _guest_body(clusters, fake_vms, 'db01', guest_name='Asked For'))
    assert st == 201, rq
    try:
        assert erp.call('POST', '/api/guest/grants', _guest_body(clusters, fake_vms, 'db01'))[0] == 403
        assert erp.call('POST', '/api/guest/requests', _guest_body(clusters, fake_vms, 'web01'))[0] == 404, 'not their VM'
        assert erp.call('POST', f"/api/guest/grants/{rq['id']}/approve", {})[0] == 403
        mine = erp.call('GET', '/api/guest/grants')[1]['grants']
        assert all(x['requested_by'] == email('contoso-erp') for x in mine)
        assert as_user('fabrikam-admin').call('POST', f"/api/guest/grants/{rq['id']}/approve", {})[0] == 404
        st, ap = as_user('contoso-admin').call('POST', f"/api/guest/grants/{rq['id']}/approve", {})
        assert st == 200 and ap['link'] and len(ap['code']) == 6
    finally:
        as_user('contoso-admin').call('POST', f"/api/guest/grants/{rq['id']}/revoke", {})


def test_guest_routes_are_public_but_bounded(admin):
    routes = {r['rule']: r for r in admin.call('GET', '/api/access/routes')[1]['routes']}
    for rule in ('/guest/<token>', '/guest/<token>/ticket', '/guest/ws', '/guest/vendor/novnc/<path:filename>', '/guest/lang/<filename>'):
        assert routes[rule]['permission'] == 'public', rule
    public = [r for r in routes if routes[r]['permission'] == 'public']
    assert all(r.startswith(('/guest', '/login', '/logout', '/internal/', '/console/ws', '/invite/')) for r in public), public
    assert Client().raw('GET', '/guest/lang/../app.py')[0] == 404


def test_guest_and_console_lists_do_not_name_accounts_above(admin, clusters, fake_vms):
    """A grant given by the installation's admin, and its console sessions, show no name to the customer."""
    st, g = admin.call('POST', '/api/guest/grants', _guest_body(clusters, fake_vms, 'web01', guest_name='Above Test'))
    assert st == 201, g
    try:
        ca = as_user('contoso-admin')
        row = next(x for x in ca.call('GET', '/api/guest/grants')[1]['grants'] if x['id'] == g['id'])
        assert row['granted_by'] is None and 'granted_by' in row['hidden']
        mine = next(x for x in admin.call('GET', '/api/guest/grants')[1]['grants'] if x['id'] == g['id'])
        assert mine['granted_by'] and 'hidden' not in mine, 'the admin still sees the name'
    finally:
        admin.call('POST', f"/api/guest/grants/{g['id']}/revoke", {})
    st, d = ca.call('GET', f"/api/console/sessions?cluster={clusters['Fake']}&limit=200")
    assert st == 200
    rows = d['sessions'] if isinstance(d, dict) else d
    above = {'admin', email('nw-admin'), email('nw-noc')}
    assert not any(r.get('username') in above for r in rows)
    assert all(r.get('client_ip') is None for r in rows if 'username' in r.get('hidden', []))



# ------------------------------------------------------------------ accounts are email addresses (PLAN-roles, Accounts, E1)
def _mk(admin, address, role_key, node_name, **extra):
    st, d = admin.call('POST', '/api/access/users', dict({'email': address, 'password': PW, 'display_name': 'John Smith',
                                                          'role_id': _role(admin, role_key),
                                                          'scope': {'kind': 'node', 'node_id': _node(admin, node_name)}}, **extra))
    assert st == 201, d
    return d['user_id']


def _drop(admin, *uids):
    for uid in uids:
        admin.call('DELETE', f'/api/access/users/{uid}')


def test_two_john_smiths(admin, clusters):
    a = _mk(admin, 'jsmith@contoso.example', 'viewer', 'Contoso')
    b = _mk(admin, 'jsmith@fabrikam.example', 'viewer', 'Fabrikam')
    try:
        ca, fa = Client(), Client()
        assert ca.login('JSmith@Contoso.Example', PW), 'the address logs in whatever its capitals'
        assert fa.login('jsmith@fabrikam.example', PW)
        assert {c['name'] for c in ca.call('GET', '/api/clusters')[1]['clusters']} == {'Fake', 'Contoso-DR'}
        assert {c['name'] for c in fa.call('GET', '/api/clusters')[1]['clusters']} == {'Fabrikam-Main'}
        seen = {u['username'] for u in as_user('contoso-admin').call('GET', '/api/access/overview')[1]['users']}
        assert 'jsmith@contoso.example' in seen and 'jsmith@fabrikam.example' not in seen
        assert admin.call('POST', '/api/access/users', {'email': 'JSMITH@contoso.example', 'password': PW})[0] == 409
    finally:
        _drop(admin, a, b)


def test_accounts_with_a_password_are_made_at_the_top_level_only(admin, clusters):
    """Below the top level people are invited (E3): nobody else chooses another person's password."""
    st, d = as_user('contoso-admin').call('POST', '/api/access/users', {
        'email': 'new@contoso.example', 'password': PW, 'role_id': _role(admin, 'viewer'),
        'scope': {'kind': 'node', 'node_id': _node(admin, 'Contoso')}})
    assert st == 403
    assert admin.call('POST', '/api/access/users', {'email': 'not-an-address', 'password': PW})[0] == 400


def test_a_consultant_in_two_tenants(admin, clusters):
    uid = _mk(admin, 'consultant@mymail.example', 'viewer', 'Contoso')
    try:
        st, _ = admin.call('POST', '/api/access/assignments', {'user_id': uid, 'role_id': _role(admin, 'viewer'),
                                                               'scope': {'kind': 'node', 'node_id': _node(admin, 'Tailspin')}})
        assert st == 201
        c = Client()
        assert c.login('consultant@mymail.example', PW)
        ov = c.call('GET', '/api/access/overview')[1]
        tops = {n['name'] for n in ov['nodes'] if n['parent_id'] is None}
        assert {'Contoso', 'Tailspin'} <= tops, 'a tree without a root'
        assert not {'Northwind IT', 'Blue Harbor'} & {n['name'] for n in ov['nodes']}
        assert {x['name'] for x in c.call('GET', '/api/clusters')[1]['clusters']} == {'Fake', 'Contoso-DR', 'Tailspin-Ops'}
        # each tenant admin sees the person, and only the access held in their own tenant
        d = as_user('contoso-admin').call('GET', f'/api/access/users/{uid}')[1]
        blob = json.dumps(d)
        assert 'Tailspin' not in blob and 'Blue Harbor' not in blob and len(d['assignments']) == 1
        assert 'Contoso' not in json.dumps(as_user('tailspin-admin').call('GET', f'/api/access/users/{uid}')[1])
        assert as_user('fabrikam-admin').call('GET', f'/api/access/users/{uid}')[0] == 404
        # credentials and the account itself are not a tenant matter
        ca = as_user('contoso-admin')
        assert ca.call('PUT', f'/api/access/users/{uid}/password', {'new_password': 'Other-pass-123'})[0] == 403
        assert ca.call('PUT', f'/api/access/users/{uid}', {'email': 'mine@contoso.example', 'password': 'Other-pass-123'})[0] == 403
        assert ca.call('PUT', f'/api/access/users/{uid}', {'enabled': False})[0] == 403
        assert ca.call('PUT', f'/api/access/users/{uid}', {'display_name': 'Renamed'})[0] == 403
        assert ca.call('DELETE', f'/api/access/users/{uid}')[0] == 403
        # removing them from Contoso leaves Tailspin
        st, d = ca.call('POST', f'/api/access/users/{uid}/remove', {})
        assert st == 200 and d['removed'] == 1
        assert ca.call('GET', f'/api/access/users/{uid}')[0] == 404
        assert {x['name'] for x in c.call('GET', '/api/clusters')[1]['clusters']} == {'Tailspin-Ops'}
    finally:
        _drop(admin, uid)


def test_an_address_change_keeps_access(admin, clusters):
    uid = _mk(admin, 'old@contoso.example', 'viewer', 'Contoso')
    try:
        st, _ = admin.call('PUT', f'/api/access/users/{uid}', {'email': 'New@Contoso.example'})
        assert st == 200
        c = Client()
        assert not c.login('old@contoso.example', PW)
        assert c.login('new@contoso.example', PW)
        assert {x['name'] for x in c.call('GET', '/api/clusters')[1]['clusters']} == {'Fake', 'Contoso-DR'}
        assert c.call('PUT', f'/api/access/users/{uid}', {'display_name': 'Me'})[0] in (403, 200)
    finally:
        _drop(admin, uid)


def test_an_account_without_address_waits_for_one(admin):
    u = next((x for x in admin.call('GET', '/api/access/overview')[1]['users'] if x['username'] == 'legacy-anna'), None)
    if not u:
        pytest.skip('demo account legacy-anna missing')
    assert u['awaiting_email'] and not u['enabled']
    st, raw = Client().raw('POST', '/login', form={'username': 'legacy-anna', 'password': PW})
    if b'Invalid email address or password' in raw:
        pytest.skip('legacy-anna does not use the demo password')
    assert st == 200 and b'needs an email address' in raw
    # giving it an address sends an invitation to set a new password: see test_an_account_from_before_gets_its_address_by_invitation


# ------------------------------------------------------------------ mail (PLAN-roles, Accounts, E2)
SINK_HOST = os.environ.get('HCM_SINK_HOST')      # this test container's network alias, for the dashboard to deliver to


@pytest.fixture(scope='module')
def sink(admin):
    """An SMTP sink in this process, set as the installation's relay for the module; the old settings return after."""
    if not SINK_HOST:
        pytest.skip('HCM_SINK_HOST not set (run with --network-alias <name> -e HCM_SINK_HOST=<name>)')
    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    from smtp_sink import Sink
    before = admin.call('GET', '/api/mail/settings')[1]
    if before.get('host') not in (None, '', SINK_HOST):
        # a real relay is set up on this stack: its stored password cannot be read back, so never overwrite it
        pytest.skip(f"mail is set up for {before['host']} on this stack; clear Settings -> Mail to run the mail tests")
    s = Sink(2525).start()
    st, d = admin.call('PUT', '/api/mail/settings', {'host': SINK_HOST, 'port': 2525, 'security': 'none', 'username': '',
                                                     'sender': 'hcm@demo.example', 'sender_name': 'HCM test',
                                                     'public_url': 'https://hcm.demo.example', 'password': ''})
    assert st == 200, d
    yield s
    admin.call('PUT', '/api/mail/settings', {'host': '', 'sender': '', 'password': ''})     # back to "not set up"
    s.stop()


def test_mail_settings_are_for_the_top_level_only(admin):
    for u in ('contoso-admin', 'nw-admin'):
        assert as_user(u).call('GET', '/api/mail/settings')[0] == 403
        assert as_user(u).call('PUT', '/api/mail/settings', {'host': 'evil.example', 'sender': 'x@evil.example'})[0] == 403
        assert as_user(u).call('POST', '/api/mail/test', {'to': 'x@demo.example'})[0] == 403


def test_mail_settings_are_checked(admin, sink):
    good = {'host': SINK_HOST, 'port': 2525, 'security': 'none', 'sender': 'hcm@demo.example', 'sender_name': 'HCM test'}
    assert admin.call('PUT', '/api/mail/settings', dict(good, port=0))[0] == 400
    assert admin.call('PUT', '/api/mail/settings', dict(good, security='ssl3'))[0] == 400
    assert admin.call('PUT', '/api/mail/settings', dict(good, sender=''))[0] == 400
    assert admin.call('PUT', '/api/mail/settings', dict(good, host='smtp://x'))[0] == 400
    assert admin.call('PUT', '/api/mail/settings', dict(good, public_url='ftp://x'))[0] == 400
    assert admin.call('PUT', '/api/mail/settings', dict(good, sender_name='a\r\nBcc: x@y.example'))[0] == 400
    # a password is stored, never returned
    st, d = admin.call('PUT', '/api/mail/settings', dict(good, public_url='https://hcm.demo.example', password='s3cret-relay'))
    assert st == 200 and d['has_password'] and 'password' not in d and 's3cret' not in json.dumps(d)
    st, d = admin.call('PUT', '/api/mail/settings', dict(good, public_url='https://hcm.demo.example'))
    assert d['has_password'], 'a password left out is kept'
    st, d = admin.call('PUT', '/api/mail/settings', dict(good, public_url='https://hcm.demo.example', password=''))
    assert not d['has_password'], 'an empty password removes it'


def test_the_test_mail_arrives(admin, sink):
    st, d = admin.call('POST', '/api/mail/test', {'to': 'Mail-Test@Demo.example', 'lang': 'nl'})
    assert st == 200 and d['sent'], d
    m = sink.to('mail-test@demo.example')[-1]
    assert m['subject'] == 'HyperCore Manager: testmail' and 'HCM test' in str(m['msg']['From'])
    rows = admin.call('GET', '/api/access/audit')[1]['rows']
    assert any(r['action'] == 'mail.test' and r['target'] == 'mail-test@demo.example' for r in rows)


def test_an_unreachable_relay_is_reported(admin, sink):
    st, _ = admin.call('PUT', '/api/mail/settings', {'host': 'no-such-relay.invalid', 'port': 25, 'security': 'none',
                                                     'sender': 'hcm@demo.example', 'public_url': 'https://hcm.demo.example'})
    assert st == 200
    try:
        st, d = admin.call('POST', '/api/mail/test', {'to': 'x@demo.example'})
        assert st == 502 and not d['sent'] and 'no-such-relay.invalid' in d['error']
        assert any(r['action'] == 'mail.failed' for r in admin.call('GET', '/api/access/audit')[1]['rows'])
    finally:
        admin.call('PUT', '/api/mail/settings', {'host': SINK_HOST, 'port': 2525, 'security': 'none',
                                                 'sender': 'hcm@demo.example', 'sender_name': 'HCM test',
                                                 'public_url': 'https://hcm.demo.example'})


# ------------------------------------------------------------------ invitations (PLAN-roles, Accounts, E3)
_LINK = re.compile(r'https?://\S+?(/invite/[A-Za-z0-9_-]+)')


def _invite(c, admin, address, role_key='viewer', node='Contoso', **extra):
    body = dict({'email': address, 'role_id': _role(admin, role_key), 'lang': 'en',
                 'scope': {'kind': 'node', 'node_id': _node(admin, node)}}, **extra)
    return c.call('POST', '/api/access/invitations', body)


def _path_in(mail):
    m = _LINK.search(mail['body'])
    assert m, mail['body']
    return m.group(1)


def _uid_of(admin, address):
    return next((u['user_id'] for u in admin.call('GET', '/api/access/overview')[1]['users'] if u['username'] == address), None)


def test_an_invited_new_address_sets_its_password(admin, clusters, sink):
    address = 'newbie@invited.example'
    ca = as_user('contoso-admin')
    st, d = _invite(ca, admin, address, lang='de')
    try:
        assert st == 201 and d['sent'] and d['link'] is None and d['hours'] == 48, d
        mail = sink.to(address)[-1]
        assert mail['subject'] == 'Sie sind zu HyperCore Manager eingeladen'
        assert 'Contoso' in mail['body'] and 'Northwind' not in mail['body'] and 'contoso-admin' not in mail['body']
        path = _path_in(mail)
        p = Client()
        st, raw = p.raw('GET', path)
        assert st == 200 and b'Willkommen' in raw and address.encode() in raw
        st, raw = p.raw('POST', path, form={'password': 'Short1', 'password2': 'Short1'})
        assert st == 200 and b'8 Zeichen' in raw, 'too short is refused'
        st, raw = p.raw('POST', path, form={'display_name': 'New Bie', 'password': 'Invited-2026!', 'password2': 'Invited-2026!'})
        assert st == 302, raw[:300]
        assert {x['name'] for x in p.call('GET', '/api/clusters')[1]['clusters']} == {'Fake', 'Contoso-DR'}, 'signed in, with the access'
        assert Client().raw('GET', path)[0] == 404, 'the link works once'
        assert Client().login('NewBie@Invited.example', 'Invited-2026!')
    finally:
        uid = _uid_of(admin, address)
        if uid:
            _drop(admin, uid)


def test_inviting_an_existing_account_answers_the_same(admin, clusters, sink):
    """The inviter cannot tell whether the address had an account; the person accepts after signing in."""
    known = _mk(admin, 'known@mymail.example', 'viewer', 'Tailspin')
    ca = as_user('contoso-admin')
    try:
        st1, fresh = _invite(ca, admin, 'unknown@mymail.example')
        st2, existing = _invite(ca, admin, 'Known@MyMail.example')
        assert st1 == st2 == 201
        assert set(fresh) == set(existing) and fresh['sent'] == existing['sent'] and existing['link'] is None
        mail = sink.to('known@mymail.example')[-1]
        assert mail['subject'] == 'New access in HyperCore Manager'
        path = _path_in(mail)
        # someone else signed in cannot take it
        st, raw = as_user('contoso-viewer').raw('GET', path)
        assert st == 200 and b'invitation is for known@mymail.example' in raw
        c = Client()
        st, raw = c.raw('GET', path)
        assert st == 200 and b'Sign in as known@mymail.example' in raw
        st, _ = c.raw('POST', '/login', form={'username': 'known@mymail.example', 'password': PW})
        assert st == 302
        st, raw = c.raw('GET', path)
        assert st == 200 and b'Accept' in raw
        assert c.raw('POST', path)[0] == 302
        assert {x['name'] for x in c.call('GET', '/api/clusters')[1]['clusters']} == {'Fake', 'Contoso-DR', 'Tailspin-Ops'}
    finally:
        _drop(admin, known)
        for i in admin.call('GET', '/api/access/invitations')[1]['invitations']:
            if i['email'] == 'unknown@mymail.example':
                admin.call('DELETE', f"/api/access/invitations/{i['invitation_id']}")


def test_without_mail_the_inviter_gets_the_link(admin, clusters, sink):
    admin.call('PUT', '/api/mail/settings', {'host': '', 'sender': ''})
    try:
        st, d = _invite(as_user('contoso-admin'), admin, 'nomail@invited.example')
        assert st == 201 and not d['sent'] and d['error'] == 'mail is not set up' and '/invite/' in d['link']
        path = _LINK.search(d['link']).group(1)
        assert Client().raw('GET', path)[0] == 200
        admin.call('DELETE', f"/api/access/invitations/{d['invitation_id']}")
        assert Client().raw('GET', path)[0] == 404, 'a withdrawn link is dead'
    finally:
        admin.call('PUT', '/api/mail/settings', {'host': SINK_HOST, 'port': 2525, 'security': 'none', 'sender': 'hcm@demo.example',
                                                 'sender_name': 'HCM test', 'public_url': 'https://hcm.demo.example'})


def test_invitations_stay_within_the_inviters_reach(admin, clusters, sink):
    ca, fa = as_user('contoso-admin'), as_user('fabrikam-admin')
    assert _invite(ca, admin, 'x@invited.example', node='Fabrikam')[0] in (403, 404)
    assert _invite(ca, admin, 'x@invited.example', role_key='admin', node='Northwind IT')[0] in (403, 404)
    assert _invite(as_user('contoso-eng'), admin, 'x@invited.example')[0] == 403
    assert _invite(ca, admin, 'contoso-admin@demo.example')[0] == 409, 'not your own address'
    st, d = _invite(fa, admin, 'fab-only@invited.example', node='Fabrikam')
    assert st == 201
    try:
        assert not any(i['email'] == 'fab-only@invited.example' for i in ca.call('GET', '/api/access/invitations')[1]['invitations'])
        assert ca.call('DELETE', f"/api/access/invitations/{d['invitation_id']}")[0] == 404
        assert ca.call('POST', f"/api/access/invitations/{d['invitation_id']}/resend")[0] == 404
        mine = next(i for i in fa.call('GET', '/api/access/invitations')[1]['invitations'] if i['invitation_id'] == d['invitation_id'])
        from datetime import datetime
        span = datetime.fromisoformat(mine['expires_at']) - datetime.fromisoformat(mine['created_at'])
        assert abs(span.total_seconds() - 48 * 3600) < 60, 'valid 48 hours'
        assert 'Northwind' not in json.dumps(mine)
    finally:
        fa.call('DELETE', f"/api/access/invitations/{d['invitation_id']}")


def test_resend_kills_the_old_link(admin, clusters, sink):
    ca = as_user('contoso-admin')
    st, d = _invite(ca, admin, 'resend@invited.example')
    old = _path_in(sink.to('resend@invited.example')[-1])
    try:
        st, r = ca.call('POST', f"/api/access/invitations/{d['invitation_id']}/resend")
        assert st == 200 and r['sent']
        new = _path_in(sink.to('resend@invited.example')[-1])
        assert new != old and Client().raw('GET', old)[0] == 404 and Client().raw('GET', new)[0] == 200
    finally:
        ca.call('DELETE', f"/api/access/invitations/{d['invitation_id']}")


def test_an_inviter_who_loses_the_right_voids_the_invitation(admin, clusters, sink):
    temp = _mk(admin, 'temp-admin@contoso.example', 'admin', 'Contoso')
    try:
        t = Client()
        assert t.login('temp-admin@contoso.example', PW)
        st, d = _invite(t, admin, 'via-temp@invited.example')
        assert st == 201
        path = _path_in(sink.to('via-temp@invited.example')[-1])
        assert Client().raw('GET', path)[0] == 200
        for a in admin.call('GET', f'/api/access/users/{temp}')[1]['assignments']:
            admin.call('DELETE', f"/api/access/assignments/{a['assignment_id']}")
        assert Client().raw('GET', path)[0] == 404, 'what the inviter may no longer give is not given'
    finally:
        _drop(admin, temp)
        for i in admin.call('GET', '/api/access/invitations')[1]['invitations']:
            if i['email'] == 'via-temp@invited.example':
                admin.call('DELETE', f"/api/access/invitations/{i['invitation_id']}")


def test_an_account_from_before_gets_its_address_by_invitation(admin, clusters, sink):
    u = next((x for x in admin.call('GET', '/api/access/overview')[1]['users'] if x['username'] == 'r0user'), None)
    if not u:
        pytest.skip('demo account r0user missing (already given an address)')
    assert as_user('contoso-admin').call('PUT', f"/api/access/users/{u['user_id']}", {'email': 'r0@demo.example'})[0] in (403, 404)
    st, d = admin.call('PUT', f"/api/access/users/{u['user_id']}", {'email': 'R0@demo.example'})
    assert st == 200 and d['invitation']['sent'], d
    mail = sink.to('r0@demo.example')[-1]
    assert mail['subject'] == 'Your HyperCore Manager account needs a new password'
    st, raw = Client().raw('POST', '/login', form={'username': 'r0@demo.example', 'password': PW})
    assert b'invitation to set a new password was sent' in raw, 'the old password does not open it'
    c = Client()
    st, raw = c.raw('POST', _path_in(mail), form={'password': 'New-pass-2026', 'password2': 'New-pass-2026'})
    assert st == 302, raw[:300]
    assert c.call('GET', '/api/users/me')[1]['username'] == 'r0@demo.example'
