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

def has_any_users() -> bool:
    """Return True if at least one user row exists (auth is enabled)."""
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute('SELECT 1 FROM config.users LIMIT 1')
                return cur.fetchone() is not None
        finally:
            conn.close()
    except Exception as exc:
        logger.warning('user_db: has_any_users() DB error, assuming no users: %s', exc)
        return False


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


def verify_otp_code(username: str, code: str) -> bool:
    """Verify a TOTP code during login (OTP must already be enabled for the user)."""
    user = get_by_username(username)
    if not user or not user['otp_enabled'] or not user['otp_secret']:
        return False
    return pyotp.TOTP(user['otp_secret']).verify(code, valid_window=1)


def enable_otp(username: str, code: str) -> bool:
    """Verify the TOTP code against the pending secret and enable OTP. Returns True on success."""
    user = get_by_username(username)
    if not user or not user['otp_secret']:
        return False
    if not pyotp.TOTP(user['otp_secret']).verify(code, valid_window=1):
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
    return True


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
