"""
migrations.py — Auto-apply pending Postgres schema migrations.

Background
----------
The `postgres/init/*.sql` files run only on a *fresh* Postgres data directory
(via the standard `/docker-entrypoint-initdb.d/` mechanism).  On any upgrade
of an existing install they are not re-executed, so a new numbered script
that ships with a release silently fails to apply until an operator runs it
by hand.  This module closes that gap: every migration filename that has
been successfully applied is recorded in `public.schema_migrations`, and the
runner applies anything not yet recorded.

Idempotency contract
--------------------
Every script under `postgres/init/` is required to be idempotent — they use
`CREATE TABLE IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`, `DO $$ ... $$`
blocks for enum types, and similar guards.  This is what makes it safe for
the runner to encounter an existing install and "reapply" all 1..N scripts
on its first run after deployment of the runner itself: the scripts no-op
on objects that already exist and the runner records each one as applied.

Failure handling
----------------
Each migration runs in its own transaction.  A failure on one migration
rolls *that* migration back, raises, and stops the runner — earlier ones
remain applied and recorded.  A subsequent call resumes from the failed
script.  This is intentional: we don't try to silently skip past a script
that errors, because the error usually means the SQL itself is broken (not
"already there"), and continuing past it would leave the schema partially
upgraded.
"""

from __future__ import annotations

import logging
import os
from typing import Iterable

log = logging.getLogger('dashboard.migrations')

# arbitrary constant shared by every dashboard worker; distinct from user_db's lock id
ADVISORY_LOCK_ID = 987654322


def apply_pending(conn, init_dir: str) -> list[str]:
    """
    Apply any `*.sql` files in `init_dir` that aren't already recorded in
    `public.schema_migrations`.  Returns the list of filenames newly
    applied (in execution order).  The connection's autocommit setting is
    not changed; this function manages its own transaction boundaries.
    """
    if not os.path.isdir(init_dir):
        log.warning('Migration directory not found: %s', init_dir)
        return []

    cur = conn.cursor()

    # several gunicorn workers start this runner at the same moment; a session-level
    # advisory lock makes them take turns so only one applies and records each file
    cur.execute('SELECT pg_advisory_lock(%s)', [ADVISORY_LOCK_ID])
    try:
        cur.execute('''
            CREATE TABLE IF NOT EXISTS public.schema_migrations (
                filename     TEXT PRIMARY KEY,
                applied_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        ''')
        conn.commit()

        cur.execute('SELECT filename FROM public.schema_migrations')
        applied: set[str] = {row['filename'] for row in cur.fetchall()}

        files: Iterable[str] = sorted(
            f for f in os.listdir(init_dir)
            if f.endswith('.sql') and not f.startswith('.')
        )

        newly_applied: list[str] = []
        for f in files:
            if f in applied:
                continue

            path = os.path.join(init_dir, f)
            with open(path, encoding='utf-8') as fh:
                sql = fh.read()

            log.info('Applying migration %s', f)
            try:
                cur.execute(sql)
                # ON CONFLICT: belt and braces for a runner that did not hold the lock
                cur.execute(
                    'INSERT INTO public.schema_migrations (filename) VALUES (%s) '
                    'ON CONFLICT (filename) DO NOTHING',
                    [f],
                )
                conn.commit()
                newly_applied.append(f)
            except Exception:
                conn.rollback()
                log.exception('Migration failed: %s', f)
                raise
    finally:
        cur.execute('SELECT pg_advisory_unlock(%s)', [ADVISORY_LOCK_ID])
        conn.commit()

    return newly_applied
