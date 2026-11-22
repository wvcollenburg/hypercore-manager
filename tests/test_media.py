"""dashboard/media.py — who sees, uses and manages a cluster's ISOs and disk images (William, 2026-11-22).

Tree: MSP M with customers A and B; A has folders A1 and A2, B has B1. Cluster CA1 is in A1, CB1 in B1.
"""
import pytest

import media
import rbac

ADMIN = rbac.BUILTIN_ROLES['admin']['perms']
ENG = rbac.BUILTIN_ROLES['engineer']['perms']          # media.manage, vm.create, vm.edit
VIEWER = rbac.BUILTIN_ROLES['viewer']['perms']
APPMGR = rbac.BUILTIN_ROLES['app_manager']['perms']


def _tree():
    t = rbac.Tree()
    for n, p, lvl in (('M', None, 'msp'), ('A', 'M', 'customer'), ('B', 'M', 'customer'),
                      ('A1', 'A', 'folder'), ('A2', 'A', 'folder'), ('B1', 'B', 'folder')):
        t.parent[n], t.level[n], t.name[n] = p, lvl, n
    t.cluster_folder.update({'CA1': 'A1', 'CB1': 'B1'})
    return t


def _acc(*grants):
    return rbac.Access(list(grants), _tree(), user_id='u')


def root(perms=ADMIN):
    return rbac.Grant(perms, 'root')


def node(n, perms=ENG):
    return rbac.Grant(perms, 'node', node_id=n)


def cluster(c, perms=ENG):
    return rbac.Grant(perms, 'cluster', cluster_id=c)


def item(owner=media.TOP, assigned=()):
    return {'owner': owner, 'assigned': list(assigned)}


# ------------------------------------------------------------------------------------- uploads
def test_an_upload_belongs_to_the_uploaders_highest_layer_over_that_cluster():
    assert media.upload_owner(_acc(root()), 'CA1') == (True, media.TOP)
    assert media.upload_owner(_acc(node('M')), 'CA1') == (True, 'M')
    assert media.upload_owner(_acc(node('A1'), node('A')), 'CA1') == (True, 'A'), 'the highest of two'
    assert media.upload_owner(_acc(cluster('CA1')), 'CA1') == (True, 'A1'), 'a cluster grant sits at its folder'


def test_who_may_not_upload():
    assert media.upload_owner(_acc(node('A', VIEWER)), 'CA1') == (False, None)
    assert media.upload_owner(_acc(node('B')), 'CA1') == (False, None), 'another customer'
    assert media.upload_owner(_acc(rbac.Grant(ENG, 'vm', cluster_id='CA1', vm_uuid='v')), 'CA1') == (False, None), \
        'media.manage is a cluster permission: a VM grant does not carry it'


# ------------------------------------------------------------------------------------- seeing
def test_media_already_on_the_cluster_are_for_the_top_level_only():
    assert media.may_see(_acc(root()), item(), 'CA1')
    for g in (node('M'), node('A'), node('A1'), cluster('CA1')):
        assert not media.may_see(_acc(g), item(), 'CA1'), g


def test_a_customers_upload_is_seen_in_the_customer_and_its_folders_not_above():
    a_iso = item('A')
    assert media.may_see(_acc(node('A')), a_iso, 'CA1')
    assert media.may_see(_acc(node('A1')), a_iso, 'CA1')
    assert media.may_see(_acc(cluster('CA1')), a_iso, 'CA1')
    assert media.may_see(_acc(rbac.Grant(ENG, 'tag', cluster_id='CA1', tag='web')), a_iso, 'CA1'), 'a tag grant uses vm.edit'
    assert not media.may_see(_acc(node('M')), a_iso, 'CA1'), 'the MSP above does not see its customer\'s upload'
    assert media.may_see(_acc(root()), a_iso, 'CA1'), 'the top sees everything'


def test_a_folder_upload_stays_in_its_folder():
    assert media.may_see(_acc(node('A1')), item('A1'), 'CA1')
    assert not media.may_see(_acc(node('A')), item('A1'), 'CA1')
    assert not media.may_see(_acc(node('A2')), item('A1'), 'CA1')


def test_seeing_needs_a_permission_that_uses_media():
    assert not media.may_see(_acc(node('A', VIEWER)), item('A'), 'CA1')
    assert not media.may_see(_acc(node('A', APPMGR)), item('A'), 'CA1')


def test_an_msps_upload_is_its_own_until_it_assigns_it_to_a_customer():
    assert media.may_see(_acc(node('M')), item('M'), 'CA1')
    assert not media.may_see(_acc(node('A')), item('M'), 'CA1'), 'the customers below an MSP are other tenants'
    assert not media.may_see(_acc(node('A1')), item('M'), 'CA1')
    assigned = item('M', assigned=['A'])
    assert media.may_see(_acc(node('A')), assigned, 'CA1') and media.may_see(_acc(node('A1')), assigned, 'CA1')
    assert not media.may_see(_acc(node('B1')), assigned, 'CB1'), 'only the customer it was given to'


def test_the_tops_media_reach_a_layer_only_by_assignment():
    assert media.may_see(_acc(node('M')), item(media.TOP, assigned=['M']), 'CA1')
    assert not media.may_see(_acc(node('A')), item(media.TOP, assigned=['M']), 'CA1'), 'given to the MSP, not its customers'
    assert media.may_see(_acc(node('A1')), item(media.TOP, assigned=['A']), 'CA1')


# ------------------------------------------------------------------------------------- managing
def test_assign_and_delete_are_for_the_owner_layer_and_the_top():
    assert media.may_manage(_acc(node('A')), item('A'), 'CA1')
    assert not media.may_manage(_acc(node('A1')), item('A'), 'CA1'), 'a folder below sees it but does not manage it'
    assert not media.may_manage(_acc(node('A', VIEWER)), item('A'), 'CA1')
    assert media.may_manage(_acc(root()), item('A'), 'CA1')
    assert not media.may_manage(_acc(node('M')), item(), 'CA1'), 'the top\'s own media are the top\'s'


def test_assignment_goes_down_the_clusters_own_branch_only():
    assert media.assign_targets(_acc(root()), item(), 'CA1') == ['M', 'A', 'A1']
    assert media.assign_targets(_acc(node('M')), item('M'), 'CA1') == ['A', 'A1'], 'an MSP: its own customer and folder'
    assert media.assign_targets(_acc(node('A')), item('A'), 'CA1') == [], 'its folders see it already'
    assert media.assign_targets(_acc(node('A1')), item('A1'), 'CA1') == []
    assert media.assign_targets(_acc(node('A1')), item('A'), 'CA1') == [], 'not the owner: nothing to assign'


# ------------------------------------------------------------------------------------- names
@pytest.mark.parametrize('kind,name', [('iso', 'Win Server 2025.iso'), ('iso', 'ubuntu-26.04.1-live-server-amd64.ISO'),
                                       ('disk', 'appliance (v2).qcow2'), ('disk', 'legacy.vmdk'), ('disk', 'w.vhdx')])
def test_good_names(kind, name):
    assert media.check_name(kind, name) == name


@pytest.mark.parametrize('kind,name', [('iso', 'win.img'), ('disk', 'x.iso'), ('iso', '../etc/passwd.iso'),
                                       ('iso', 'a/b.iso'), ('iso', 'bad\nname.iso'), ('iso', ''), ('iso', '.hidden.iso'),
                                       ('iso', 'x' * 300 + '.iso'), ('tape', 'x.iso')])
def test_bad_names(kind, name):
    with pytest.raises(ValueError):
        media.check_name(kind, name)
