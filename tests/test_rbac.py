"""Roles and tenants (plans/PLAN-roles.md): the pure rules in dashboard/rbac.py, and repository guards
that keep every route behind a declared permission."""
import re

import pytest

import rbac

ROOT = None


def tree():
    """msp1 > cust1 > fold1 (c1, c2), fold2 (c3); msp1 > cust2 > fold3 (c4); msp2 > cust3 > fold4 (c5); c6 unfiled."""
    t = rbac.Tree()
    for node, parent, level in [('msp1', None, 'msp'), ('cust1', 'msp1', 'customer'), ('fold1', 'cust1', 'folder'),
                                ('fold2', 'cust1', 'folder'), ('cust2', 'msp1', 'customer'), ('fold3', 'cust2', 'folder'),
                                ('msp2', None, 'msp'), ('cust3', 'msp2', 'customer'), ('fold4', 'cust3', 'folder')]:
        t.parent[node] = parent
        t.level[node] = level
        t.name[node] = node
    t.cluster_folder.update({'c1': 'fold1', 'c2': 'fold1', 'c3': 'fold2', 'c4': 'fold3', 'c5': 'fold4', 'c6': None})
    return t


def acc(*grants):
    return rbac.Access(list(grants), tree())


def role(key):
    return rbac.BUILTIN_ROLES[key]['perms']


# ------------------------------------------------------------------ catalogue
def test_implications_are_known_permissions():
    for k, v in rbac.PERMISSIONS.items():
        assert v['level'] in rbac.LEVELS, k
        assert v['area'] in rbac.AREAS, k
        for imp in v['implies']:
            assert imp in rbac.PERMISSIONS, f'{k} implies unknown {imp}'


def test_expand_is_transitive_and_drops_unknown():
    assert rbac.expand(['backup.download']) >= {'backup.browse', 'backup.view', 'vm.view'}
    assert rbac.expand(['no.such.permission']) == frozenset()


def test_builtin_roles_are_closed_under_implication():
    for k, r in rbac.BUILTIN_ROLES.items():
        assert rbac.expand(r['perms']) == r['perms'], k


def test_app_manager_cannot_edit_or_power():
    am = role('app_manager')
    assert 'vm.console' in am and 'vm.view' in am
    assert not ({'vm.edit', 'vm.power', 'vm.delete', 'backup.view'} & am)


def test_backup_manager_has_every_backup_action_but_infra():
    bm = role('backup_manager')
    backup = {k for k, v in rbac.PERMISSIONS.items() if v['area'] == 'backup'}
    assert backup <= bm
    assert not ({'backup.targets.manage', 'backup.proxies.manage', 'vm.console', 'vm.edit'} & bm)


# ------------------------------------------------------------------ tree
def test_tree_ancestors_and_within():
    t = tree()
    assert t.ancestors('fold1') == ['fold1', 'cust1', 'msp1']
    assert t.is_within('fold1', 'msp1') and t.is_within('fold1', None)
    assert not t.is_within('fold4', 'msp1')
    assert t.cluster_within('c1', 'cust1') and not t.cluster_within('c4', 'cust1')
    assert not t.cluster_within('c6', 'msp1') and t.cluster_within('c6', None)
    assert t.subtree('cust1') == {'cust1', 'fold1', 'fold2'}


def test_tree_survives_a_cycle():
    t = rbac.Tree(parent={'a': 'b', 'b': 'a'}, level={'a': 'folder', 'b': 'customer'})
    assert t.ancestors('a') == ['a', 'b']


# ------------------------------------------------------------------ resolution
def test_root_admin_can_everything():
    a = acc(rbac.Grant(role('admin'), 'root'))
    assert a.is_root_admin()
    assert a.can('install.manage') and a.can('users.manage', node_id='fold4') and a.can('vm.delete', 'c6', 'vm1')


def test_msp_admin_stays_inside_the_msp():
    a = acc(rbac.Grant(role('admin'), 'node', node_id='msp1'))
    assert a.can('vm.delete', 'c4', 'x') and a.can('users.manage', node_id='fold3') and a.can('cluster.manage', 'c1')
    assert not a.can('vm.view', 'c5', 'x')                  # the other MSP
    assert not a.can('cluster.view', 'c6')                  # unfiled: root only
    assert not a.can('install.manage')                      # never below root
    assert not a.is_root_admin()
    assert a.visible_clusters(['c1', 'c2', 'c3', 'c4', 'c5', 'c6']) == ['c1', 'c2', 'c3', 'c4']


def test_folder_engineer():
    a = acc(rbac.Grant(role('engineer'), 'node', node_id='fold1'))
    assert a.can('vm.power', 'c1', 'v') and a.can('vm.create', 'c2')
    assert not a.can('vm.power', 'c3', 'v')
    assert not a.can('users.manage', node_id='fold1')       # not in the template
    assert not a.can('backup.restore.full', 'c1', 'v')


def test_vm_grant_carries_only_vm_level_permissions():
    g = rbac.Grant(role('admin'), 'vm', cluster_id='c1', vm_uuid='v1')
    assert g.effective() <= rbac.VM_LEVEL
    a = acc(g)
    assert a.can('vm.console', 'c1', 'v1') and a.can('vm.delete', 'c1', 'v1')
    assert not a.can('vm.console', 'c1', 'v2')
    assert not a.can('cluster.view', 'c1')                  # a VM grant never shows the cluster overview
    assert not a.can('vm.create', 'c1')
    assert a.visible_clusters(['c1', 'c2']) == ['c1']


def test_tag_grant_follows_the_tag():
    a = acc(rbac.Grant(role('app_manager'), 'tag', cluster_id='c1', tag='erp'))
    assert a.can('vm.console', 'c1', 'v9', vm_tags=['x', 'erp'])
    assert not a.can('vm.console', 'c1', 'v9', vm_tags=['x'])
    assert not a.can('vm.console', 'c2', 'v9', vm_tags=['erp'])      # same tag, other cluster
    assert a.scope_for('vm.console', ['c1', 'c2']) == {'c1': {'vms': [], 'tags': ['erp']}}


def test_scope_for_mixes_whole_and_partial():
    a = acc(rbac.Grant(role('viewer'), 'cluster', cluster_id='c1'),
            rbac.Grant(role('app_manager'), 'vm', cluster_id='c2', vm_uuid='v7'))
    assert a.scope_for('vm.view', ['c1', 'c2', 'c3']) == {'c1': 'all', 'c2': {'vms': ['v7'], 'tags': []}}
    assert a.scope_for('cluster.view', ['c1', 'c2']) == {'c1': 'all'}


def test_cluster_grant_carries_no_node_permissions():
    a = acc(rbac.Grant(role('admin'), 'cluster', cluster_id='c1'))
    assert a.can('vm.delete', 'c1', 'v') and a.can('cluster.settings', 'c1')
    assert not a.can('users.manage', node_id='fold1')
    assert not a.can('cluster.manage', 'c1')


def test_union_of_grants():
    a = acc(rbac.Grant(role('viewer'), 'node', node_id='cust1'),
            rbac.Grant(role('engineer'), 'node', node_id='fold2'))
    assert a.can('vm.power', 'c3', 'v') and not a.can('vm.power', 'c1', 'v') and a.can('vm.view', 'c1', 'v')


def test_superuser():
    a = rbac.Access([], rbac.Tree(), superuser=True)
    assert a.can('install.manage') and a.can('vm.delete', 'any', 'vm') and a.is_root_admin()


def test_unknown_permission_is_refused():
    assert not acc(rbac.Grant(role('admin'), 'root')).can('no.such.thing')


def test_top_nodes_and_visible_nodes():
    a = acc(rbac.Grant(role('admin'), 'node', node_id='cust1'), rbac.Grant(role('admin'), 'node', node_id='fold1'))
    assert a.top_nodes_with('users.manage') == ['cust1']
    assert a.visible_nodes() == {'msp1', 'cust1', 'fold1', 'fold2'}
    b = acc(rbac.Grant(role('viewer'), 'cluster', cluster_id='c4'))
    assert b.visible_nodes() == {'fold3', 'cust2', 'msp1'}


def test_vm_in_scope():
    assert rbac.vm_in_scope('all', 'x')
    assert rbac.vm_in_scope({'vms': ['a'], 'tags': []}, 'a')
    assert rbac.vm_in_scope({'vms': [], 'tags': ['t']}, 'b', ['t'])
    assert not rbac.vm_in_scope({'vms': ['a'], 'tags': ['t']}, 'b', ['u'])
    assert not rbac.vm_in_scope(None, 'a')


# ------------------------------------------------------------------ no escalation
def test_may_grant_only_what_you_hold_where_you_hold_it():
    cust_admin = acc(rbac.Grant(role('admin'), 'node', node_id='cust1'))
    assert cust_admin.can('users.manage', node_id='fold1')
    assert rbac.may_grant(cust_admin, role('engineer'), 'node', node_id='fold1')[0]
    assert rbac.may_grant(cust_admin, role('admin'), 'node', node_id='cust1')[0]
    assert not rbac.may_grant(cust_admin, role('engineer'), 'node', node_id='fold3')[0]    # other customer
    assert not rbac.may_grant(cust_admin, role('admin'), 'root')[0]
    assert not rbac.may_grant(cust_admin, ['install.manage'], 'node', node_id='fold1')[0]  # carries nothing there


def test_engineer_cannot_hand_out_backup_rights():
    eng = acc(rbac.Grant(role('engineer'), 'node', node_id='fold1'))
    ok, why = rbac.may_grant(eng, role('backup_manager'), 'vm', cluster_id='c1', vm_uuid='v')
    assert not ok and 'backup' in why
    assert rbac.may_grant(eng, role('app_manager'), 'vm', cluster_id='c1', vm_uuid='v')[0]


def test_vm_scoped_user_cannot_create_a_tag_grant():
    """A tag grant reaches every VM that gets the tag later: only someone covering the cluster may make one."""
    vm_admin = acc(rbac.Grant(role('admin'), 'vm', cluster_id='c1', vm_uuid='v1'))
    assert not rbac.may_grant(vm_admin, role('app_manager'), 'tag', cluster_id='c1')[0]
    assert rbac.may_grant(vm_admin, role('app_manager'), 'vm', cluster_id='c1', vm_uuid='v1')[0]


# ------------------------------------------------------------------ repository guards
@pytest.mark.parametrize('rel,minimum', [('dashboard/app.py', 60), ('dashboard/access_api.py', 20)])
def test_every_dashboard_route_declares_a_permission(repo, rel, minimum):
    """Deny by default: every Flask route carries @requires(...) or @public(...) directly under its
    @app.route / @sock.route lines (the app also refuses to serve an undeclared one at runtime)."""
    src = (repo / rel).read_text()
    blocks = re.findall(r'((?:\s*@(?:app|sock)\.route\([^\n]*\)\n)+)\s*(@[^\n]+)\n', src)
    assert len(blocks) > minimum
    bad = [b[0].strip().splitlines()[0] for b in blocks if not b[1].startswith(('@requires(', '@public('))]
    assert bad == [], f'routes without a permission declaration: {bad}'


def test_declared_permissions_exist(repo):
    src = (repo / 'dashboard' / 'app.py').read_text()
    for perm in re.findall(r"@requires\('([a-z_.]+)'", src):
        assert perm in rbac.PERMISSIONS, perm
    for perm in re.findall(r"^\s+\('[A-Z|]+',\s+r'[^']+',\s+'([a-z_.]+)'", src, re.M):
        assert perm in rbac.PERMISSIONS, perm


def test_no_admin_role_checks_left(repo):
    """Rights come from the access model only; a role string compare would bypass it."""
    src = (repo / 'dashboard' / 'app.py').read_text()
    assert "session.get('role')" not in src
    assert '@require_admin' not in src


def test_flux_day_ranges_are_numbers(repo):
    """?days= was pasted into Flux unchecked (a way around the cluster filter); it goes through _days_arg."""
    src = (repo / 'dashboard' / 'app.py').read_text()
    assert "request.args.get('days', '7')" not in src


def test_rbac_migration_upgrade_runs_once(repo):
    """The fresh-install runner re-applies every file once; the step that disables non-admin accounts
    must not run a second time (it is guarded by config.rbac_state)."""
    sql = (repo / 'postgres' / 'init' / '044_rbac.sql').read_text()
    assert "IF NOT EXISTS (SELECT 1 FROM config.rbac_state WHERE key = 'upgraded')" in sql
    assert 'UPDATE config.users SET enabled = FALSE, awaiting_access = TRUE' in sql
    for table in ('config.roles', 'config.groups', 'config.group_members', 'config.assignments', 'config.access_audit',
                  'config.permissions_known'):
        assert f'CREATE TABLE IF NOT EXISTS {table}' in sql, table
    for key in rbac.BUILTIN_ROLES:
        assert f"'{key}'," in sql, f'built-in role {key} is not seeded'


def test_access_frontend_uses_no_browser_popups(repo):
    """William, 2026-11-13: only modals, no browser prompt/confirm/alert."""
    for rel in ('dashboard/static/index.html', 'dashboard/static/access.js', 'dashboard/templates/console.html'):
        src = (repo / rel).read_text()
        assert not re.search(r'(?<![A-Za-z_.])(prompt|confirm|alert)\(', src), rel
