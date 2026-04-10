"""
test_01_infra.py
Covers: §1.3 health (via docker exec — port 8080 not exposed to host),
        §1.4 API reachable (port 8081), G6 health endpoint, G7 structured logging.
"""
from __future__ import annotations

import json
import subprocess

import pytest
import requests


pytestmark = pytest.mark.usefixtures("sentinel_up")


def _docker_exec_python(container: str, code: str, timeout: int = 10) -> subprocess.CompletedProcess:
    """Run a Python one-liner inside a Docker container."""
    return subprocess.run(
        ["docker", "exec", container, "python", "-c", code],
        capture_output=True, text=True, timeout=timeout,
    )


def _health_get(container: str, path: str) -> dict:
    """GET http://localhost:8080{path} from inside the sentinel container."""
    code = (
        "import urllib.request, json; "
        f"r = urllib.request.urlopen('http://localhost:8080{path}', timeout=5); "
        "print(r.read().decode())"
    )
    result = _docker_exec_python(container, code)
    if result.returncode != 0:
        raise RuntimeError(f"Health request failed: {result.stderr}")
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# §1.3 / G6 — health endpoint (via docker exec)
# ---------------------------------------------------------------------------

class TestHealthEndpoint:
    """G6 — health check endpoint (§1.3). Port 8080 is container-internal; accessed via docker exec."""

    def test_g6_1_health_ok(self, health_container):
        """G6-1 / §1.3: GET /health returns {"status":"ok"}."""
        body = _health_get(health_container, "/health")
        assert body.get("status") == "ok"

    def test_g6_2_health_detail(self, health_container):
        """G6-2 / §1.3: GET /health/detail returns service status and timestamps."""
        body = _health_get(health_container, "/health/detail")
        assert "status" in body
        has_collection = "collection_last_ok" in body or "last_collection_at" in body
        has_analysis   = "trend_last_ok" in body or "last_analysis_at" in body
        assert has_collection, f"No collection timestamp in health/detail: {list(body)}"
        assert has_analysis,   f"No analysis/trend timestamp in health/detail: {list(body)}"

    def test_health_detail_has_timestamps(self, health_container):
        """§1.3: /health/detail includes collection and trend/analysis timestamps."""
        body = _health_get(health_container, "/health/detail")
        assert (
            "collection_last_ok" in body or "last_collection_at" in body
        ), f"Missing collection timestamp: {list(body)}"
        assert (
            "trend_last_ok" in body or "last_analysis_at" in body
        ), f"Missing analysis/trend timestamp: {list(body)}"


# ---------------------------------------------------------------------------
# §1.4 — API reachable (port 8081, host-mapped)
# ---------------------------------------------------------------------------

class TestApiReachable:
    def test_s1_4_api_reachable(self, api_base, auth_headers):
        """§1.4: GET /api/v1/summary returns 200."""
        r = requests.get(f"{api_base}/api/v1/summary", headers=auth_headers, timeout=5)
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# §1.5 — DB reachable
# ---------------------------------------------------------------------------

class TestDbMigration:
    def test_s1_5_postgres_reachable(self, db_conn):
        """§1.5: PostgreSQL responds to a basic query."""
        with db_conn.cursor() as cur:
            cur.execute("SELECT 1")
            assert cur.fetchone()[0] == 1


# ---------------------------------------------------------------------------
# G7 — structured logging
# ---------------------------------------------------------------------------

class TestStructuredLogging:
    def _get_recent_logs(self, container: str, n: int = 20) -> list[str]:
        result = subprocess.run(
            ["docker", "logs", container, "--tail", str(n)],
            capture_output=True, text=True, timeout=10,
        )
        lines = (result.stdout + result.stderr).splitlines()
        return [l for l in lines if l.strip()]

    def test_g7_1_logs_are_json(self, health_container):
        """G7-1: every recent log line from sc_sentinel is valid JSON."""
        lines = self._get_recent_logs(health_container, 20)
        if not lines:
            pytest.skip("No log lines returned")
        invalid = [l for l in lines if not _is_json(l)]
        assert not invalid, f"Non-JSON log lines:\n" + "\n".join(invalid[:3])

    def test_g7_2_logs_have_required_fields(self, health_container):
        """G7-2: log lines include level and message fields."""
        lines = self._get_recent_logs(health_container, 20)
        parsed = [json.loads(l) for l in lines if _is_json(l)]
        if not parsed:
            pytest.skip("No parseable JSON log lines")
        for obj in parsed[:5]:
            assert "level" in obj or "levelname" in obj, f"No level field: {obj}"
            assert "message" in obj or "msg" in obj, f"No message field: {obj}"


def _is_json(s: str) -> bool:
    try:
        json.loads(s)
        return True
    except json.JSONDecodeError:
        return False
