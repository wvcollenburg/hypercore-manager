"""dashboard/verify_mail.py: failed / overdue verification mails go to the closest admin of the folder."""
from datetime import datetime, timezone

import pytest

import rbac
import verify_mail as vm

ADMIN = rbac.ALL
VIEW = frozenset({'backup.view', 'vm.view'})


def tree():
    t = rbac.Tree()
    t.parent.update({'msp': None, 'cust': 'msp', 'fold': 'cust', 'other': 'cust', 'cust2': 'msp', 'fold2': 'cust2'})
    t.level.update({'msp': 'msp', 'cust': 'customer', 'fold': 'folder', 'other': 'folder', 'cust2': 'customer', 'fold2': 'folder'})
    return t


def u(name):
    return {'username': name}


def g(kind, node=None, perms=ADMIN):
    return rbac.Grant(perms=perms, kind=kind, node_id=node)


def names(users):
    return sorted(x['username'] for x in users)


def test_the_customer_admin_before_the_msp_before_the_installation():
    t = tree()
    cands = [(u('root@x'), [g('root')]), (u('msp@x'), [g('node', 'msp')]), (u('cust@x'), [g('node', 'cust')]),
             (u('viewer@x'), [g('node', 'fold', VIEW)]), (u('other-cust@x'), [g('node', 'cust2')])]
    assert names(vm.closest_admins(cands, t, 'fold')) == ['cust@x']
    assert names(vm.closest_admins([c for c in cands if c[0]['username'] != 'cust@x'], t, 'fold')) == ['msp@x']
    assert names(vm.closest_admins([c for c in cands if c[0]['username'] in ('root@x', 'viewer@x', 'other-cust@x')], t, 'fold')) == ['root@x']


def test_a_folder_admin_is_closer_still_and_several_at_one_level_all_get_it():
    t = tree()
    cands = [(u('cust@x'), [g('node', 'cust')]), (u('f1@x'), [g('node', 'fold')]), (u('f2@x'), [g('node', 'fold')]),
             (u('sibling@x'), [g('node', 'other')])]
    assert names(vm.closest_admins(cands, t, 'fold')) == ['f1@x', 'f2@x']


def test_unfiled_clusters_go_to_the_installation_admins():
    t = tree()
    cands = [(u('root@x'), [g('root')]), (u('cust@x'), [g('node', 'cust')])]
    assert names(vm.closest_admins(cands, t, None)) == ['root@x']
    assert vm.closest_admins([(u('cust@x'), [g('node', 'cust')])], t, None) == []


def test_a_mail_per_kind_lists_every_alert_of_the_folder():
    rows = [{'kind': 'failed', 'cluster_id': 'c1', 'vm_name': 'dc01', 'seq': 4, 'detail': 'disk.qcow2: checksum differs'},
            {'kind': 'failed', 'cluster_id': 'c1', 'vm_name': 'sql01', 'seq': 0, 'detail': 'nvram: missing'},
            {'kind': 'overdue', 'cluster_id': 'c2', 'vm_name': 'web01', 'seq': None, 'detail': '3 restore point(s) not verified'}]
    out = dict(vm.compose(rows, 'Contoso', {'c1': 'Prod', 'c2': 'DR'}, 'https://hcm.example'))
    assert set(out) == {'verify_failed', 'verify_overdue'}
    f = out['verify_failed']
    assert f['n'] == 2 and f['folder'] == 'Contoso' and f['link'] == 'https://hcm.example'
    assert f['items'].splitlines() == ['- dc01 #4 (Prod): disk.qcow2: checksum differs', '- sql01 #0 (Prod): nvram: missing']
    assert out['verify_overdue']['items'] == '- web01 (DR): 3 restore point(s) not verified'


class _Cur:
    def __init__(self, conn): self.conn = conn
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None):
        self.conn.sql.append((sql, params))
        self.last = sql
    def fetchall(self):
        if 'RETURNING' in self.last:
            return self.conn.claimable
        if 'config.folders' in self.last:
            return [{'id': 'fold', 'name': 'Contoso'}]
        if 'config.clusters' in self.last:
            return [{'id': 'c1', 'cluster_name': 'Prod'}]
        return []


class _Conn:
    def __init__(self, rows):
        self.claimable, self.sql, self.commits = rows, [], 0
    def cursor(self): return _Cur(self)
    def commit(self): self.commits += 1
    def rollback(self): pass
    def close(self): pass


@pytest.fixture
def mail(monkeypatch):
    sent = []
    monkeypatch.setattr(vm.mailer, 'configured', lambda: True)
    monkeypatch.setattr(vm.mailer, 'link_base', lambda h=None: 'https://hcm.example')
    monkeypatch.setattr(vm.mailer, 'deliver', lambda to, kind, lang=None, by=None, **v: sent.append((to, kind, v)) or {'sent': True, 'error': None})
    monkeypatch.setattr(vm.access_db, 'load_tree', tree)
    monkeypatch.setattr(vm, '_mailable_candidates', lambda: [(u('cust@x'), [g('node', 'cust')]), (u('root@x'), [g('root')])])
    return sent


def test_run_once_mails_the_closest_admin_and_records_it(mail):
    row = {'id': 7, 'kind': 'failed', 'folder_id': 'fold', 'cluster_id': 'c1', 'vm_name': 'dc01', 'seq': 4,
           'detail': 'disk.qcow2: checksum differs', 'created_at': datetime.now(timezone.utc)}
    conn = _Conn([row])
    assert vm.run_once(conn) == 1
    assert [(to, kind) for to, kind, _ in mail] == [('cust@x', 'verify_failed')]
    claim_sql = conn.sql[0][0]
    assert 'FOR UPDATE SKIP LOCKED' in claim_sql and 'mail_claimed_at IS NULL' in claim_sql, 'claimed once across workers'
    rec = [p for s, p in conn.sql if s.startswith('UPDATE backup.verify_alerts SET mailed_at')]
    assert rec and rec[0][0] is True and rec[0][1] == 'cust@x: sent' and rec[0][2] == [7]


def test_without_mail_set_up_the_alerts_are_marked_and_not_sent_later(mail, monkeypatch):
    monkeypatch.setattr(vm.mailer, 'configured', lambda: False)
    conn = _Conn([{'id': 8, 'kind': 'overdue', 'folder_id': 'fold', 'cluster_id': 'c1', 'vm_name': 'x', 'seq': None,
                   'detail': '', 'created_at': None}])
    assert vm.run_once(conn) == 0 and mail == []
    rec = [p for s, p in conn.sql if s.startswith('UPDATE backup.verify_alerts SET mailed_at')]
    assert rec[0][1] == 'mail is not set up'


def test_nothing_to_claim_sends_nothing(mail):
    assert vm.run_once(_Conn([])) == 0 and mail == []
