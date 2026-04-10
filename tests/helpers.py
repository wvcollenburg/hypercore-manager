"""helpers.py — shared utilities for the test suite."""
from __future__ import annotations

import hashlib
import hmac
import json


def db_one(conn, sql: str, params=None):
    """Return the first row of a query, or None."""
    with conn.cursor() as cur:
        if params is not None:
            cur.execute(sql, params)
        else:
            cur.execute(sql)
        return cur.fetchone()


def db_scalar(conn, sql: str, params=None):
    """Return a single scalar value."""
    row = db_one(conn, sql, params)
    return row[0] if row else None


def db_all(conn, sql: str, params=None):
    """Return all rows."""
    with conn.cursor() as cur:
        if params is not None:
            cur.execute(sql, params)
        else:
            cur.execute(sql)
        return cur.fetchall()


def verify_hmac(secret: str, body: bytes, signature_header: str) -> bool:
    """
    Verify X-Sentinel-Signature: sha256=<hex>
    against HMAC-SHA256(secret, body).
    """
    if not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(
        secret.encode(), body, hashlib.sha256
    ).hexdigest()
    received = signature_header[len("sha256="):]
    return hmac.compare_digest(expected, received)


def assert_json_fields(data: dict, fields: list[str]):
    """Assert every field name exists in a dict (top-level)."""
    missing = [f for f in fields if f not in data]
    assert not missing, f"Missing fields: {missing}"
