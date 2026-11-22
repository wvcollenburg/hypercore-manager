"""A guest grant's counters are changed in one statement (1.8.1).

The wrong-code counter and sessions_used were read, increased in Python and written back: four workers
answering at once could each count from the same old value (more code guesses than MAX_FAILED, more
sessions than max_sessions).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


class FakeDB:
    def __init__(self, grant):
        self.grant, self.sql = grant, []

    def connect(self):
        db = self

        class Cur:
            def __enter__(s): return s
            def __exit__(s, *a): return False

            def execute(s, sql, params=None):
                db.sql.append(' '.join(sql.split()))
                s.last = sql

            def fetchone(s):
                if 'FROM console.guest_grants WHERE token_hash' in s.last:
                    return dict(db.grant)
                if 'failed_codes = COALESCE(failed_codes, 0) + 1' in s.last:
                    db.grant['failed_codes'] = (db.grant['failed_codes'] or 0) + 1
                    return {'failed_codes': db.grant['failed_codes']}
                if 'sessions_used = sessions_used + 1' in s.last:
                    if db.grant['sessions_used'] >= db.grant['max_sessions']:
                        return None
                    db.grant['sessions_used'] += 1
                    return {'sessions_used': db.grant['sessions_used']}
                return None

        class Conn:
            def cursor(s): return Cur()
            def commit(s): pass
            def rollback(s): pass
            def close(s): pass
        return Conn()


@pytest.fixture
def guest(dash, monkeypatch):
    now = datetime.now(timezone.utc)
    grant = {'id': 7, 'cluster_id': 'c1', 'vm_uuid': 'v1', 'status': 'active', 'valid_from': now - timedelta(hours=1),
             'valid_until': now + timedelta(hours=1), 'code_hash': 'pbkdf2:sha256:1$x$y', 'failed_codes': 0,
             'label': 'Console', 'guest_name': 'Bob', 'guest_company': None, 'sessions_used': 0, 'max_sessions': 1}
    db = FakeDB(grant)
    monkeypatch.setattr(dash.access_db, '_connect', db.connect)
    return dash.app.test_client(), db


def test_a_wrong_code_is_counted_in_the_database(guest):
    client, db = guest
    r = client.post('/guest/' + 'T' * 40, data={'code': '123456'})
    assert r.status_code == 401
    assert any('failed_codes = COALESCE(failed_codes, 0) + 1' in s and 'RETURNING failed_codes' in s for s in db.sql)
    assert not any(s.startswith('UPDATE console.guest_grants SET failed_codes = %s') for s in db.sql)
