"""
test_07_whatif.py
Covers: D5 — What-If scenario modelling.

Actual API response shape (verified against live API):
  {
    "scenario": "add_vms",
    "count": 5,
    "baseline": { "health_score": ..., "bottleneck": ..., "exhaustion_dates": {...}, ... },
    "simulated": { "health_score": ..., ... },
    "delta": { "health_score": ..., "exhaustion_gained_days": {...}, ... },
    "assumptions": { "all_dimensions": "..." }
  }

Valid scenarios: "add_vms", "add_nodes"  (not "remove_vms")
"""
from __future__ import annotations

import os

import pytest
import requests


pytestmark = pytest.mark.usefixtures("sentinel_up")

_WHATIF = "/api/v1/whatif"
_DASH_WHATIF = "/api/sentinel/whatif"


class TestWhatIfApi:
    """D5-4: POST /api/v1/whatif directly."""

    def test_d5_4_add_vms(self, api_base, auth_headers):
        """D5-4a: add_vms scenario returns baseline, simulated, delta."""
        r = requests.post(
            f"{api_base}{_WHATIF}",
            headers={**auth_headers, "Content-Type": "application/json"},
            json={"scenario": "add_vms", "count": 5},
            timeout=15,
        )
        assert r.status_code == 200, f"what-if add_vms failed: {r.text}"
        body = r.json()
        for field in ("baseline", "simulated", "delta"):
            assert field in body, f"Missing field: {field}"
        assert "health_score" in body["baseline"], "baseline.health_score missing"
        assert "health_score" in body["simulated"], "simulated.health_score missing"

    def test_d5_4_add_nodes_scenario(self, api_base, auth_headers):
        """D5-4b: add_nodes scenario returns improved or maintained health score."""
        r = requests.post(
            f"{api_base}{_WHATIF}",
            headers={**auth_headers, "Content-Type": "application/json"},
            json={"scenario": "add_nodes", "count": 1},
            timeout=15,
        )
        assert r.status_code == 200, f"what-if add_nodes failed: {r.text}"
        body = r.json()
        assert body["simulated"]["health_score"] >= body["baseline"]["health_score"], \
            "Adding nodes should improve or maintain health score"

    def test_d5_4_invalid_scenario_returns_error(self, api_base, auth_headers):
        """D5-4c: unknown scenario name returns 4xx."""
        r = requests.post(
            f"{api_base}{_WHATIF}",
            headers={**auth_headers, "Content-Type": "application/json"},
            json={"scenario": "teleport_vms", "count": 1},
            timeout=10,
        )
        assert r.status_code in (400, 422), \
            f"Expected 400/422 for invalid scenario, got {r.status_code}"

    def test_d5_4_add_vms_worsens_health(self, api_base, auth_headers):
        """D5-3 (add variant): adding VMs should worsen or maintain health score."""
        r = requests.post(
            f"{api_base}{_WHATIF}",
            headers={**auth_headers, "Content-Type": "application/json"},
            json={"scenario": "add_vms", "count": 10},
            timeout=15,
        )
        assert r.status_code == 200
        body = r.json()
        # Adding load should not improve health
        assert body["simulated"]["health_score"] <= body["baseline"]["health_score"], \
            "Adding 10 VMs should not improve health score"


class TestWhatIfDashboardProxy:
    """D5-5: POST /api/sentinel/whatif via dashboard proxy."""

    def test_d5_5_dashboard_proxy(self, dashboard_base):
        """D5-5: dashboard proxy forwards what-if request to sentinel."""
        user = os.getenv("SC_DASHBOARD_USER", "admin")
        pwd  = os.getenv("SC_DASHBOARD_PASSWORD", "")
        r = requests.post(
            f"{dashboard_base}{_DASH_WHATIF}",
            auth=(user, pwd) if pwd else None,
            json={"scenario": "add_vms", "count": 3},
            timeout=15,
        )
        if r.status_code == 401:
            pytest.skip("Dashboard credentials not set")
        assert r.status_code == 200, \
            f"Dashboard what-if proxy returned {r.status_code}: {r.text}"
        body = r.json()
        assert "baseline" in body
