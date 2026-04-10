"""
test_06_tunables.py
Covers: T1 (DB seed), T2 (sentinel tunables API), T3 (dashboard proxy).

config.tunables columns: cluster_id, key, value, type, updated_at
(no is_live_reload column in DB — live_reload flag is only in the API response)
"""
from __future__ import annotations

import os

import pytest
import requests
from helpers import db_all, db_scalar


pytestmark = pytest.mark.usefixtures("sentinel_up")

_TUNABLES = "/api/v1/tunables"
_DASH_TUNABLES = "/api/sentinel/tunables"

EXPECTED_LIVE_KEYS = {
    "SENTINEL_CPU_OVER_PROVISIONED_THRESHOLD",
    "SENTINEL_CPU_UNDER_PROVISIONED_AVG_THRESHOLD",
    "SENTINEL_CPU_UNDER_PROVISIONED_P95_THRESHOLD",
    "SENTINEL_ANALYSIS_WINDOW_DAYS",
    "SENTINEL_RECURRING_SPIKE_MIN_OCCURRENCES",
    "SENTINEL_ANOMALY_FILTER_ENABLED",
    "SENTINEL_FORECAST_30D_ENABLED",
    "SENTINEL_FORECAST_90D_ENABLED",
    "SENTINEL_FORECAST_180D_ENABLED",
    "SENTINEL_FORECAST_365D_ENABLED",
    "SENTINEL_RETENTION_MONTHS",
    "SENTINEL_OUTCOME_TRACKER_ENABLED",
    "SENTINEL_OUTCOME_CHECK_DELAY_HOURS",
    "SENTINEL_WEBHOOK_ENABLED",
}


# ---------------------------------------------------------------------------
# T1 — DB seeding
# ---------------------------------------------------------------------------

class TestTunablesDb:
    def test_t1_1_tunables_seeded(self, db_conn):
        """T1-1: 14 live-reload SENTINEL_* keys are seeded in config.tunables."""
        rows = db_all(
            db_conn,
            "SELECT key FROM config.tunables WHERE key LIKE 'SENTINEL%'",
        )
        keys = {r[0] for r in rows}
        missing = EXPECTED_LIVE_KEYS - keys
        assert not missing, f"Missing tunable keys: {missing}"

    def test_t1_3_live_reload_flagged_via_api(self, api_base, auth_headers):
        """T1-3: all 14 expected keys report live_reload=true in the API response."""
        r = requests.get(f"{api_base}{_TUNABLES}", headers=auth_headers, timeout=10)
        assert r.status_code == 200
        live_keys = {
            item["key"] for item in r.json()
            if item.get("live_reload") is True
        }
        missing = EXPECTED_LIVE_KEYS - live_keys
        assert not missing, f"Keys missing live_reload=true in API: {missing}"


# ---------------------------------------------------------------------------
# T2 — Sentinel tunables API
# ---------------------------------------------------------------------------

class TestTunablesApi:
    def test_t2_1_get_returns_all_keys(self, api_base, auth_headers):
        """T2-1: GET /api/v1/tunables returns array with key, value, default, type, live_reload."""
        r = requests.get(f"{api_base}{_TUNABLES}", headers=auth_headers, timeout=10)
        assert r.status_code == 200
        items = r.json()
        assert isinstance(items, list) and items, "Empty or non-list response"
        sample = items[0]
        for field in ("key", "value", "default", "type", "live_reload"):
            assert field in sample, f"Field '{field}' missing from tunable object"

    def test_t2_2_put_valid_threshold(self, api_base, auth_headers, db_conn):
        """T2-2: PUT with a valid threshold update returns 200 and persists to DB."""
        rows = db_all(db_conn,
                      "SELECT value FROM config.tunables "
                      "WHERE key='SENTINEL_CPU_OVER_PROVISIONED_THRESHOLD'")
        original = float(rows[0][0]) if rows else 20.0

        new_val = 25.0 if original != 25.0 else 21.0
        r = requests.put(
            f"{api_base}{_TUNABLES}",
            headers={**auth_headers, "Content-Type": "application/json"},
            json={"SENTINEL_CPU_OVER_PROVISIONED_THRESHOLD": new_val},
            timeout=10,
        )
        assert r.status_code == 200, f"PUT failed: {r.text}"

        rows = db_all(db_conn,
                      "SELECT value FROM config.tunables "
                      "WHERE key='SENTINEL_CPU_OVER_PROVISIONED_THRESHOLD'")
        assert rows and float(rows[0][0]) == new_val

        # Restore
        requests.put(
            f"{api_base}{_TUNABLES}",
            headers={**auth_headers, "Content-Type": "application/json"},
            json={"SENTINEL_CPU_OVER_PROVISIONED_THRESHOLD": original},
            timeout=10,
        )

    def test_t2_3_put_out_of_range_returns_400(self, api_base, auth_headers):
        """T2-3: PUT threshold=150 (out of 0–100 range) → 400."""
        r = requests.put(
            f"{api_base}{_TUNABLES}",
            headers={**auth_headers, "Content-Type": "application/json"},
            json={"SENTINEL_CPU_OVER_PROVISIONED_THRESHOLD": 150},
            timeout=10,
        )
        assert r.status_code == 400, f"Expected 400 for out-of-range, got {r.status_code}"

    def test_t2_4_put_wrong_type_returns_400(self, api_base, auth_headers):
        """T2-4: PUT with string value for a float field → 400."""
        r = requests.put(
            f"{api_base}{_TUNABLES}",
            headers={**auth_headers, "Content-Type": "application/json"},
            json={"SENTINEL_CPU_OVER_PROVISIONED_THRESHOLD": "not-a-number"},
            timeout=10,
        )
        assert r.status_code == 400, f"Expected 400 for wrong type, got {r.status_code}"

    def test_t2_5_put_window_days_zero_returns_400(self, api_base, auth_headers):
        """T2-5: PUT SENTINEL_ANALYSIS_WINDOW_DAYS=0 → 400 (window ≥ 1 required)."""
        r = requests.put(
            f"{api_base}{_TUNABLES}",
            headers={**auth_headers, "Content-Type": "application/json"},
            json={"SENTINEL_ANALYSIS_WINDOW_DAYS": 0},
            timeout=10,
        )
        assert r.status_code == 400, f"Expected 400 for window_days=0, got {r.status_code}"


# ---------------------------------------------------------------------------
# T3 — Dashboard proxy
# ---------------------------------------------------------------------------

class TestTunablesDashboardProxy:
    def _dash_auth(self):
        user = os.getenv("SC_DASHBOARD_USER", "admin")
        pwd  = os.getenv("SC_DASHBOARD_PASSWORD", "")
        return (user, pwd) if pwd else None

    def test_t3_1_dashboard_get_proxies_authenticated(self, dashboard_base):
        """T3-1: GET /api/sentinel/tunables returns sentinel tunables payload via dashboard."""
        auth = self._dash_auth()
        r = requests.get(
            f"{dashboard_base}{_DASH_TUNABLES}",
            auth=auth,
            timeout=10,
        )
        if r.status_code == 401:
            pytest.skip("Dashboard credentials not set (SC_DASHBOARD_PASSWORD empty)")
        assert r.status_code == 200, f"Dashboard tunables proxy returned {r.status_code}"
        items = r.json()
        assert isinstance(items, list), "Expected a list from tunables proxy"

    def test_t3_2_dashboard_put_proxy(self, dashboard_base, api_base, auth_headers, db_conn):
        """T3-2: PUT /api/sentinel/tunables via dashboard has same validation as direct API."""
        auth = self._dash_auth()
        # Send out-of-range value — should get 400 back (validates same as direct API)
        r = requests.put(
            f"{dashboard_base}{_DASH_TUNABLES}",
            auth=auth,
            json={"SENTINEL_CPU_OVER_PROVISIONED_THRESHOLD": 200},
            timeout=10,
        )
        if r.status_code == 401:
            pytest.skip("Dashboard credentials not set")
        assert r.status_code == 400, \
            f"Dashboard proxy should reject out-of-range value, got {r.status_code}"
