"""Backups belong to the folder (dashboard/backup_scope.py): folder-wide lists, each row judged by its
own cluster, so a right on one cluster never shows another cluster's VMs of the same folder."""
import backup_scope
import rbac


def tree():
    t = rbac.Tree()
    for node, parent, level in [('msp', None, 'msp'), ('cust', 'msp', 'customer'), ('prod', 'cust', 'folder'), ('dr', 'cust', 'folder')]:
        t.parent[node], t.level[node], t.name[node] = parent, level, node
    t.cluster_folder.update({'A': 'prod', 'B': 'prod', 'C': 'dr', 'U': None})
    return t


def role(key):
    return rbac.BUILTIN_ROLES[key]['perms']


def acc(*grants):
    return rbac.Access(list(grants), tree())


def test_folder_scopes_cover_the_folder_where_the_right_is_held():
    customer = acc(rbac.Grant(role('backup_manager'), 'node', node_id='cust'))
    assert backup_scope.folder_scopes(customer, 'backup.view', 'A') == {'A': 'all', 'B': 'all'}
    one = acc(rbac.Grant(role('viewer'), 'cluster', cluster_id='A'))
    assert backup_scope.folder_scopes(one, 'backup.view', 'A') == {'A': 'all'}
    vm = acc(rbac.Grant(role('viewer'), 'vm', cluster_id='B', vm_uuid='b1'))
    assert backup_scope.folder_scopes(vm, 'backup.view', 'A') == {'B': {'vms': ['b1'], 'tags': []}}
    root = acc(rbac.Grant(role('admin'), 'root'))
    assert backup_scope.folder_scopes(root, 'backup.view', 'U') == {'U': 'all'}, 'a cluster in no folder stands alone'


ROWS = [{'cluster_id': 'A', 'vm_uuid': 'a1', 'vm_name': 'a1'}, {'cluster_id': 'A', 'vm_uuid': 'a2', 'vm_name': 'a2'},
        {'cluster_id': 'B', 'vm_uuid': 'b1', 'vm_name': 'b1'}]
NO_TAGS = lambda c: []  # noqa: E731


def names(rows):
    return sorted(r['vm_name'] for r in rows)


def test_rows_of_a_cluster_without_the_right_are_dropped():
    assert names(backup_scope.narrow('chains', ROWS, {'A': 'all', 'B': 'all'}, 'A', NO_TAGS)) == ['a1', 'a2', 'b1']
    assert names(backup_scope.narrow('chains', ROWS, {'A': 'all'}, 'A', NO_TAGS)) == ['a1', 'a2']
    assert names(backup_scope.narrow('plans', ROWS, {'A': {'vms': ['a2'], 'tags': []}, 'B': 'all'}, 'A', NO_TAGS)) == ['a2', 'b1']


def test_tag_grants_read_the_tags_of_the_rows_own_cluster():
    tags = {'A': [{'uuid': 'a1', 'tags': ['erp']}], 'B': [{'uuid': 'b1', 'tags': ['erp']}]}
    asked = []

    def index(c):
        asked.append(c)
        return tags[c]
    got = backup_scope.narrow('chains', ROWS, {'A': {'vms': [], 'tags': ['erp']}}, 'A', index)
    assert names(got) == ['a1'] and asked == ['A'], 'cluster B is not read: no right there at all'


def test_schedule_members_on_another_cluster_stay_hidden():
    """The leak this module closed: a right on cluster A showed a folder schedule's members on cluster B."""
    data = {'policies': [{'name': 'nightly', 'members': ROWS}, {'name': 'empty', 'members': []},
                         {'name': 'only-b', 'members': [ROWS[2]]}]}
    got = backup_scope.narrow('policies', data, {'A': 'all'}, 'A', NO_TAGS)
    assert [(p['name'], names(p['members'])) for p in got['policies']] == [('nightly', ['a1', 'a2']), ('empty', [])]
    vm_only = backup_scope.narrow('policies', data, {'A': {'vms': ['a1'], 'tags': []}}, 'A', NO_TAGS)
    assert [p['name'] for p in vm_only['policies']] == ['nightly'], 'an empty schedule is for whole-cluster holders'


def test_runs_imports_are_cluster_level():
    data = {'runs': ROWS, 'restores': [ROWS[2]], 'imports': [{'cluster_id': 'A', 'id': 1}, {'cluster_id': 'B', 'id': 2}]}
    got = backup_scope.narrow('runs', data, {'A': 'all', 'B': {'vms': ['b1'], 'tags': []}}, 'A', NO_TAGS)
    assert names(got['runs']) == ['a1', 'a2', 'b1'] and names(got['restores']) == ['b1']
    assert [i['id'] for i in got['imports']] == [1], 'imports need the right on the whole cluster'


def test_night_view_items():
    got = backup_scope.narrow('policies/7/night', {'items': ROWS, 'night': 'x'}, {'B': 'all'}, 'A', NO_TAGS)
    assert names(got['items']) == ['b1'] and got['night'] == 'x'
    assert backup_scope.narrow('policies/7/night', {'items': ROWS}, {'C': 'all'}, 'A', NO_TAGS) == {'items': []}
