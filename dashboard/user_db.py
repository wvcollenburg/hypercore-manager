"""
user_db.py — Dashboard user store
==================================
Manages dashboard users in the config.users PostgreSQL table.
Handles password hashing (werkzeug/scrypt), TOTP secrets, and role management.

Connection: same PG_HOST / PG_PORT / PG_DB / PG_USER / PG_PASSWORD env vars
used by config_db.py.

Call order at startup:
    user_db.ensure_table()         — idempotent CREATE TABLE IF NOT EXISTS
    user_db.seed_admin_from_env()  — seeds admin from SC_DASHBOARD_USER / SC_DASHBOARD_PASSWORD
                                     only when the table is empty
"""

import base64
import io
import logging
import os

import psycopg2
import psycopg2.extras
import pyotp
import qrcode
import qrcode.image.pil
from werkzeug.security import check_password_hash, generate_password_hash

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# connection
# ---------------------------------------------------------------------------

def _connect():
    return psycopg2.connect(
        host=os.getenv('PG_HOST', 'postgres'),
        port=int(os.getenv('PG_PORT', '5432')),
        dbname=os.getenv('PG_DB', 'hypercore_balancer'),
        user=os.getenv('PG_USER', 'sentinel'),
        password=os.getenv('PG_PASSWORD', ''),
        connect_timeout=5,
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


# ---------------------------------------------------------------------------
# schema bootstrap
# ---------------------------------------------------------------------------

def ensure_table() -> None:
    """
    Create config.users if it doesn't exist. Safe to call on every startup,
    including concurrently from multiple gunicorn workers — the advisory lock
    prevents race conditions without requiring a DBA.
    """
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                # advisory lock scoped to this connection — only one worker
                # runs the DDL at a time; others wait and then no-op
                cur.execute('SELECT pg_advisory_xact_lock(987654321)')
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS config.users (
                        user_id       UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
                        username      TEXT        NOT NULL UNIQUE,
                        password_hash TEXT        NOT NULL,
                        role          TEXT        NOT NULL DEFAULT 'user'
                                      CHECK (role IN ('admin', 'user')),
                        otp_secret    TEXT,
                        otp_enabled   BOOLEAN     NOT NULL DEFAULT FALSE,
                        created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """)
                # columns added after the first release; mirrors postgres/init/019_community_ack.sql
                cur.execute("""
                    ALTER TABLE config.users
                        ADD COLUMN IF NOT EXISTS community_ack BOOLEAN NOT NULL DEFAULT FALSE
                """)
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning('user_db: could not create users table: %s', exc)


def seed_admin_from_env() -> None:
    """
    If the users table is empty AND SC_DASHBOARD_USER / SC_DASHBOARD_PASSWORD
    are both set, create the initial admin account from those values.
    No-op if users already exist or env vars are absent.
    """
    username = os.getenv('SC_DASHBOARD_USER', '').strip()
    password = os.getenv('SC_DASHBOARD_PASSWORD', '').strip()
    if not username or not password:
        return
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute('SELECT COUNT(*) AS n FROM config.users')
                row = cur.fetchone()
                if row and row['n'] > 0:
                    return
                cur.execute(
                    """INSERT INTO config.users (username, password_hash, role)
                       VALUES (%s, %s, 'admin')
                       ON CONFLICT (username) DO NOTHING""",
                    (username, generate_password_hash(password)),
                )
            conn.commit()
            logger.info('user_db: seeded initial admin user "%s" from env', username)
        finally:
            conn.close()
    except Exception as exc:
        logger.warning('user_db: could not seed admin from env: %s', exc)


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def has_any_users() -> bool | None:
    """True when at least one user row exists (auth is enabled), False when the table is empty,
    None when the database could not be asked. Unknown is not "no users": the caller keeps
    authentication on until the database answers."""
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute('SELECT 1 FROM config.users LIMIT 1')
                return cur.fetchone() is not None
        finally:
            conn.close()
    except Exception as exc:
        logger.warning('user_db: has_any_users() DB error, answer unknown: %s', exc)
        return None


def get_by_username(username: str) -> dict | None:
    """Return full user row (including password_hash) or None."""
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute('SELECT * FROM config.users WHERE username = %s', (username,))
                row = cur.fetchone()
                return dict(row) if row else None
        finally:
            conn.close()
    except Exception as exc:
        logger.warning('user_db: get_by_username(%s) error: %s', username, exc)
        return None


def get_by_login(login: str) -> dict | None:
    """The account a login form names: an email address in any capitals, or the main admin's username."""
    login = (login or '').strip()
    if not login:
        return None
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute('SELECT * FROM config.users WHERE username = %s', (login,))
                row = cur.fetchone()
                if not row:
                    cur.execute('SELECT * FROM config.users WHERE lower(username) = lower(%s)', (login,))
                    rows = cur.fetchall()
                    row = rows[0] if len(rows) == 1 else None
                return dict(row) if row else None
        finally:
            conn.close()
    except Exception as exc:
        logger.warning('user_db: get_by_login error: %s', exc)
        return None


def get_all() -> list:
    """Return all users without password_hash, ordered by created_at."""
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT user_id, username, role, otp_enabled, created_at, updated_at
                      FROM config.users
                     ORDER BY created_at
                """)
                return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()
    except Exception as exc:
        logger.warning('user_db: get_all() error: %s', exc)
        return []


def create_user(username: str, password: str, role: str = 'user') -> None:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO config.users (username, password_hash, role)
                   VALUES (%s, %s, %s)""",
                (username, generate_password_hash(password), role),
            )
        conn.commit()
    finally:
        conn.close()


def delete_user(username: str) -> None:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute('DELETE FROM config.users WHERE username = %s', (username,))
        conn.commit()
    finally:
        conn.close()


def set_password(username: str, new_password: str) -> None:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE config.users
                      SET password_hash = %s, updated_at = NOW()
                    WHERE username = %s""",
                (generate_password_hash(new_password), username),
            )
        conn.commit()
    finally:
        conn.close()


def verify_password(username: str, password: str) -> bool:
    user = get_by_username(username)
    if not user:
        return False
    return check_password_hash(user['password_hash'], password)


def get_or_create_otp_secret(username: str) -> str:
    """
    Return the pending OTP secret for this user. If none exists yet, generate
    and persist one. The secret is NOT yet active until enable_otp() is called.
    """
    user = get_by_username(username)
    if not user:
        raise ValueError(f'Unknown user: {username}')
    if user['otp_secret']:
        return user['otp_secret']
    secret = pyotp.random_base32()
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                'UPDATE config.users SET otp_secret = %s, updated_at = NOW() WHERE username = %s',
                (secret, username),
            )
        conn.commit()
    finally:
        conn.close()
    return secret


def totp_counter(secret: str, code: str, now: float | None = None) -> int | None:
    """The time step (counter) a TOTP code belongs to, within one step either side of now; None when it fits none."""
    import hmac
    import time as _time
    code = (code or '').strip()
    if not code.isdigit() or len(code) > 10:
        return None
    totp = pyotp.TOTP(secret)
    step = int((now if now is not None else _time.time()) // totp.interval)
    for counter in (step, step - 1, step + 1):
        if hmac.compare_digest(str(totp.generate_otp(counter)), code):
            return counter
    return None


def _claim_otp_counter(username: str, counter: int) -> bool:
    """Record `counter` as the user's last accepted TOTP step, only when it is newer than the last one: a code
    is accepted once, also when four workers see it at the same moment. Before migration 057 (no column) every
    valid code is accepted, as before."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            try:
                cur.execute('''UPDATE config.users SET otp_last_counter = %s
                                WHERE username = %s AND (otp_last_counter IS NULL OR otp_last_counter < %s)
                                RETURNING 1''', (counter, username, counter))
                ok = cur.fetchone() is not None
            except psycopg2.Error as exc:
                if getattr(exc, 'pgcode', None) != '42703':
                    raise
                conn.rollback()
                logger.warning('user_db: otp_last_counter missing (migration 057 not applied yet): codes not single-use')
                return True
        conn.commit()
        return ok
    finally:
        conn.close()


def verify_otp_code(username: str, code: str) -> bool:
    """Verify a TOTP code during login (OTP must already be enabled for the user). A code is used once: a code
    whose time step is not newer than the last accepted one is refused (a code seen over a shoulder or in a
    proxy log cannot be replayed within its window)."""
    user = get_by_username(username)
    if not user or not user['otp_enabled'] or not user['otp_secret']:
        return False
    counter = totp_counter(user['otp_secret'], code)
    if counter is None:
        return False
    return _claim_otp_counter(username, counter)


def enable_otp(username: str, code: str) -> bool:
    """Verify the TOTP code against the pending secret and enable OTP. Returns True on success."""
    user = get_by_username(username)
    if not user or not user['otp_secret']:
        return False
    counter = totp_counter(user['otp_secret'], code)
    if counter is None:
        return False
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                'UPDATE config.users SET otp_enabled = TRUE, updated_at = NOW() WHERE username = %s',
                (username,),
            )
        conn.commit()
    finally:
        conn.close()
    _claim_otp_counter(username, counter)           # the code used to switch it on does not also sign in
    return True


# ---------------------------------------------------------------------------
# failed sign-ins (config.login_failures, migration 057)
# ---------------------------------------------------------------------------
# Counted in the database: the dashboard runs four gunicorn workers, and the session is a cookie the client
# holds (a counter in it can be reset by sending an older cookie). `login` is what was typed (lowercased), so
# an unknown address is limited exactly like a known one and the answer never tells which it is.
LOGIN_WINDOW_MINUTES = 15
MAX_FAILURES_PER_LOGIN = 10
MAX_FAILURES_PER_CLIENT = 30         # an office behind one address shares it
MAX_OTP_FAILURES = 5                 # per password step: then the password again


def _login_key(login: str) -> str:
    return (login or '').strip().lower()[:254]


def _failures_query(sql: str, params: tuple, fetch: bool = False):
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                row = cur.fetchone() if fetch else None
            conn.commit()
            return row
        finally:
            conn.close()
    except psycopg2.Error as exc:
        if getattr(exc, 'pgcode', None) == '42P01':           # before migration 057: no limit yet
            logger.warning('user_db: config.login_failures missing (migration 057 not applied yet)')
            return None
        raise


def login_locked(login: str, client: str) -> bool:
    """Too many failed sign-ins in the window, for this login or from this client address."""
    row = _failures_query(
        f'''SELECT COUNT(*) FILTER (WHERE login = %s) AS by_login, COUNT(*) FILTER (WHERE client = %s) AS by_client
              FROM config.login_failures WHERE at > NOW() - INTERVAL '{LOGIN_WINDOW_MINUTES} minutes'
               AND (login = %s OR client = %s)''',
        (_login_key(login), client or '', _login_key(login), client or ''), fetch=True)
    if not row:
        return False
    return row['by_login'] >= MAX_FAILURES_PER_LOGIN or row['by_client'] >= MAX_FAILURES_PER_CLIENT


def otp_failures_since(login: str, since_epoch: float) -> int:
    """Wrong codes for this login since its password step (the time in the session)."""
    row = _failures_query('''SELECT COUNT(*) AS n FROM config.login_failures
                              WHERE login = %s AND step = 'otp' AND at > to_timestamp(%s)''',
                          (_login_key(login), float(since_epoch or 0)), fetch=True)
    return int(row['n']) if row else 0


def record_login_failure(login: str, client: str, step: str) -> None:
    _failures_query(
        '''WITH old AS (DELETE FROM config.login_failures WHERE at < NOW() - INTERVAL '1 day')
           INSERT INTO config.login_failures (login, client, step) VALUES (%s, %s, %s)''',
        (_login_key(login), (client or '')[:100], step))


def clear_login_failures(login: str) -> None:
    """A complete sign-in: the typos before it no longer count against the account (the client's stay)."""
    _failures_query('DELETE FROM config.login_failures WHERE login = %s', (_login_key(login),))


def bump_session_gen(username: str) -> int | None:
    """End every session of this account: each session carries the generation it signed in with, and every
    request compares it with this column (migration 058). Returns the new generation, for the caller's own
    session to keep going; None before migration 058 or for an unknown account."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            try:
                cur.execute('UPDATE config.users SET session_gen = session_gen + 1 WHERE username = %s RETURNING session_gen',
                            (username,))
                row = cur.fetchone()
            except psycopg2.Error as exc:
                if getattr(exc, 'pgcode', None) != '42703':
                    raise
                conn.rollback()
                logger.warning('user_db: session_gen missing (migration 058 not applied yet): sessions not ended')
                return None
        conn.commit()
    finally:
        conn.close()
    import access_db
    access_db.invalidate()                          # every worker re-reads the user's state at its next request
    return int(row['session_gen']) if row else None


def set_community_ack(username: str) -> None:
    """Record that *username* has acknowledged the community-support notice."""
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    'UPDATE config.users SET community_ack = TRUE, updated_at = NOW() WHERE username = %s',
                    (username,),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning('user_db: set_community_ack(%s) error: %s', username, exc)


def disable_otp(username: str) -> None:
    """Disable OTP and discard the secret."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE config.users
                      SET otp_enabled = FALSE, otp_secret = NULL, updated_at = NOW()
                    WHERE username = %s""",
                (username,),
            )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# OTP setup helper
# ---------------------------------------------------------------------------

def build_otp_setup(username: str, issuer: str = 'HyperCore Dashboard') -> dict:
    """
    Generate (or retrieve) the OTP secret for a user and return a dict with:
      secret      — the raw base32 secret (for manual entry)
      uri         — otpauth:// provisioning URI
      qr_data_url — data:image/png;base64,... PNG QR code (dark theme colours)
    """
    secret = get_or_create_otp_secret(username)
    uri = pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=issuer)

    qr = qrcode.QRCode(box_size=6, border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(uri)
    qr.make(fit=True)
    img = qr.make_image(fill_color='#f4f7fa', back_color='#151c27')
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    data_url = 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode()

    return {'secret': secret, 'uri': uri, 'qr_data_url': data_url}
