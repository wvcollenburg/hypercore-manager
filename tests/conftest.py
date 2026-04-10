"""
conftest.py — shared fixtures for the SC//Foresight automated test suite.

Loads .env from the project root. All connection parameters can be overridden
with TEST_* env vars so the suite works both locally and in CI.

    TEST_SENTINEL_API   — default http://localhost:8081
    TEST_DASHBOARD_API  — default http://localhost:5000
    TEST_PG_HOST        — default localhost  (container uses 'postgres')
"""

from __future__ import annotations

# Tell pytest to ignore stale temp files the cache provider leaves on network drives
collect_ignore_glob = ["pytest-cache-files-*"]

import os
from pathlib import Path

import psycopg2
import pytest
import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Load .env from project root (one level up from this tests/ directory)
# ---------------------------------------------------------------------------
load_dotenv(Path(__file__).parent.parent / ".env", override=False)

# ---------------------------------------------------------------------------
# Connection coordinates
# ---------------------------------------------------------------------------
SENTINEL_API  = os.getenv("TEST_SENTINEL_API",  "http://localhost:8081")
# Port 8080 (health) is not exposed to host — accessed via docker exec in tests
HEALTH_CONTAINER = os.getenv("TEST_HEALTH_CONTAINER", "sc_sentinel")
DASHBOARD_API = os.getenv("TEST_DASHBOARD_API", "http://localhost:5000")
TOKEN         = os.getenv("SENTINEL_API_TOKEN", "")

PG_HOST = os.getenv("TEST_PG_HOST",       "localhost")
PG_PORT = int(os.getenv("SENTINEL_PG_PORT", "5432"))
PG_DB   = os.getenv("SENTINEL_PG_DB",     "hypercore_balancer")
PG_USER = os.getenv("SENTINEL_PG_USER",   "sentinel")
PG_PASS = os.getenv("SENTINEL_PG_PASSWORD", "")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def api_base() -> str:
    return SENTINEL_API


@pytest.fixture(scope="session")
def health_container() -> str:
    """Name of the sentinel Docker container for health endpoint access via docker exec."""
    return HEALTH_CONTAINER


@pytest.fixture(scope="session")
def dashboard_base() -> str:
    return DASHBOARD_API


@pytest.fixture(scope="session")
def token() -> str:
    return TOKEN


@pytest.fixture(scope="session")
def auth_headers() -> dict:
    """Authorization header, or empty dict when running in dev/no-auth mode."""
    if TOKEN:
        return {"Authorization": f"Bearer {TOKEN}"}
    return {}


@pytest.fixture(scope="session")
def bad_headers() -> dict:
    return {"Authorization": "Bearer definitely-wrong-token-xyz"}


@pytest.fixture(scope="session")
def sentinel_up(api_base):
    """Skip every test in a module if the sentinel stack is unreachable.
    Probes port 8081 (API) because port 8080 (health) is container-internal only."""
    try:
        r = requests.get(f"{api_base}/api/v1/summary", timeout=5)
        # 200 or 401 both confirm the container is alive
        assert r.status_code in (200, 401)
    except Exception as exc:
        pytest.skip(f"Sentinel API unreachable on {api_base}: {exc}")


@pytest.fixture(scope="session")
def db_conn():
    """psycopg2 connection to PostgreSQL (host-side; uses localhost, not container name)."""
    try:
        conn = psycopg2.connect(
            host=PG_HOST, port=PG_PORT,
            dbname=PG_DB, user=PG_USER, password=PG_PASS,
            connect_timeout=5,
        )
        conn.autocommit = True
        yield conn
        conn.close()
    except Exception as exc:
        pytest.skip(f"PostgreSQL unreachable: {exc}")


@pytest.fixture(scope="session")
def has_data(db_conn) -> bool:
    """True once sentinel has completed at least one analysis cycle."""
    with db_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM recommendations.forecast_snapshots")
        return cur.fetchone()[0] > 0
