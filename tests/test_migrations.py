"""dashboard/migrations.py — the idempotent migration runner, driven by a fake connection."""
import pytest

import migrations


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=None):
        self.conn.executed.append((sql.strip(), params))
        if 'pg_advisory_lock' in sql:
            self.conn.lock_depth += 1
        elif 'pg_advisory_unlock' in sql:
            self.conn.lock_depth -= 1
        elif 'INSERT INTO public.schema_migrations' in sql:
            self.conn.pending_insert = params[0]
        elif sql.strip().startswith('SELECT filename'):
            self._rows = [{'filename': f} for f in sorted(self.conn.applied)]
        elif sql.strip().upper().startswith('CREATE TABLE IF NOT EXISTS PUBLIC.SCHEMA_MIGRATIONS'):
            pass
        else:
            # a migration body: fail when told to
            if self.conn.fail_on and self.conn.fail_on in sql:
                raise RuntimeError(f'boom in {self.conn.fail_on}')
            self.conn.bodies.append(sql.strip())

    def fetchall(self):
        return self._rows


class FakeConn:
    """Records SQL and models commit/rollback for the migration bookkeeping."""
    def __init__(self, applied=(), fail_on=None):
        self.applied = set(applied)
        self.executed = []
        self.bodies = []
        self.fail_on = fail_on
        self.pending_insert = None
        self.commits = 0
        self.rollbacks = 0
        self.lock_depth = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1
        if self.pending_insert:
            self.applied.add(self.pending_insert)
            self.pending_insert = None

    def rollback(self):
        self.rollbacks += 1
        self.pending_insert = None


@pytest.fixture
def init_dir(tmp_path):
    (tmp_path / '001_first.sql').write_text('CREATE TABLE IF NOT EXISTS a (x int);')
    (tmp_path / '002_second.sql').write_text('CREATE TABLE IF NOT EXISTS b (x int);')
    (tmp_path / '010_tenth.sql').write_text('CREATE TABLE IF NOT EXISTS j (x int);')
    (tmp_path / 'README.md').write_text('not sql')
    (tmp_path / '.hidden.sql').write_text('SELECT 1;')
    return tmp_path


def test_applies_all_in_lexical_order_on_fresh_db(init_dir):
    conn = FakeConn()
    applied = migrations.apply_pending(conn, str(init_dir))
    assert applied == ['001_first.sql', '002_second.sql', '010_tenth.sql']
    assert conn.applied == set(applied)
    assert [b for b in conn.bodies] == [
        'CREATE TABLE IF NOT EXISTS a (x int);',
        'CREATE TABLE IF NOT EXISTS b (x int);',
        'CREATE TABLE IF NOT EXISTS j (x int);',
    ]


def test_skips_already_recorded_files(init_dir):
    conn = FakeConn(applied={'001_first.sql', '002_second.sql'})
    applied = migrations.apply_pending(conn, str(init_dir))
    assert applied == ['010_tenth.sql']
    assert conn.bodies == ['CREATE TABLE IF NOT EXISTS j (x int);']


def test_second_run_is_a_noop(init_dir):
    conn = FakeConn()
    migrations.apply_pending(conn, str(init_dir))
    again = migrations.apply_pending(conn, str(init_dir))
    assert again == []


def test_failure_stops_at_failing_file_and_keeps_earlier(init_dir):
    conn = FakeConn(fail_on='b (x int)')
    with pytest.raises(RuntimeError):
        migrations.apply_pending(conn, str(init_dir))
    assert conn.applied == {'001_first.sql'}
    assert conn.rollbacks == 1
    # resuming after the fix picks up where it stopped
    conn.fail_on = None
    assert migrations.apply_pending(conn, str(init_dir)) == ['002_second.sql', '010_tenth.sql']


def test_runner_serialises_with_advisory_lock_and_tolerates_duplicates(init_dir):
    """Several gunicorn workers start the runner at once: it must take the advisory lock,
    release it even on failure, and record files with ON CONFLICT so a race never raises."""
    conn = FakeConn()
    migrations.apply_pending(conn, str(init_dir))
    sqls = [s for s, _ in conn.executed]
    assert any('pg_advisory_lock' in s for s in sqls)
    assert sqls.index(next(s for s in sqls if 'pg_advisory_lock' in s)) < \
           sqls.index(next(s for s in sqls if 'CREATE TABLE IF NOT EXISTS public.schema_migrations' in s)), \
           'lock must be taken before the bookkeeping table is created'
    assert conn.lock_depth == 0, 'lock released'
    assert all('ON CONFLICT (filename) DO NOTHING' in s for s in sqls if s.startswith('INSERT INTO public.schema_migrations'))

    failing = FakeConn(fail_on='b (x int)')
    with pytest.raises(RuntimeError):
        migrations.apply_pending(failing, str(init_dir))
    assert failing.lock_depth == 0, 'lock released on failure too'


def test_missing_directory_returns_empty(tmp_path):
    assert migrations.apply_pending(FakeConn(), str(tmp_path / 'nope')) == []


def test_repo_migrations_are_numbered_and_unique(repo):
    files = sorted(p.name for p in (repo / 'postgres' / 'init').glob('*.sql'))
    numbers = [f.split('_', 1)[0] for f in files]
    assert all(n.isdigit() and len(n) == 3 for n in numbers), files
    assert len(set(numbers)) == len(numbers), 'duplicate migration number'
    assert numbers == sorted(numbers)


def test_repo_migrations_look_idempotent(repo):
    """Every shipped migration must guard its DDL so re-running it is a no-op."""
    guards = ('IF NOT EXISTS', 'IF EXISTS', 'DO $$', 'ON CONFLICT', 'CREATE OR REPLACE')
    for p in (repo / 'postgres' / 'init').glob('*.sql'):
        sql = p.read_text().upper()
        assert any(g in sql for g in guards), f'{p.name} has no idempotency guard'
