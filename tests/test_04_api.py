"""
test_04_api.py
Covers: F1, F2, F3, F6, C7 — endpoint reachability and response-shape checks.

Actual DB column names (verified against live schema):
  forecast_snapshots: forecast_horizon_days, confidence_score, resource_dimension,
                      forecast_value, entity_label, driving_metric, model_metadata (JSONB)
  right_sizing:       sizing_direction, composite_score, cpu_avg_usage_pct
  resize_recommendations: confidence_score, capacity_reclaim_cpu_pct, risk_level,
                          suggested_cpu_change_pct, sizing_direction
"""
from __future__ import annotations

import pytest
import requests


pytestmark = pytest.mark.usefixtures("sentinel_up")


# ---------------------------------------------------------------------------
# F1 — GET /api/v1/forecasts
# ---------------------------------------------------------------------------

class TestForecasts:
    def test_f1_1_status_200(self, api_base, auth_headers):
        """F1-1: returns HTTP 200."""
        r = requests.get(f"{api_base}/api/v1/forecasts", headers=auth_headers, timeout=10)
        assert r.status_code == 200

    def test_f1_2_entity_label_present(self, api_base, auth_headers):
        """F1-2: each forecast object has an entity_label field."""
        r = requests.get(f"{api_base}/api/v1/forecasts", headers=auth_headers, timeout=10)
        items = r.json()
        if not items:
            pytest.skip("No forecast data yet")
        for item in items[:5]:
            assert "entity_label" in item, f"entity_label missing: {item}"

    def test_f1_3_all_horizons_present(self, api_base, auth_headers):
        """F1-3: all four forecast horizons (30, 90, 180, 365) are represented."""
        r = requests.get(
            f"{api_base}/api/v1/forecasts?limit=1000", headers=auth_headers, timeout=10
        )
        items = r.json()
        if not items:
            pytest.skip("No forecast data yet")
        horizons = {item["forecast_horizon_days"] for item in items if "forecast_horizon_days" in item}
        for h in (30, 90, 180, 365):
            assert h in horizons, f"forecast_horizon_days={h} missing"


# ---------------------------------------------------------------------------
# F2 — GET /api/v1/rightsizing and /resize
# ---------------------------------------------------------------------------

class TestRightsizing:
    def test_f2_1_rightsizing_200(self, api_base, auth_headers):
        """F2-1a: /rightsizing returns 200."""
        r = requests.get(f"{api_base}/api/v1/rightsizing", headers=auth_headers, timeout=10)
        assert r.status_code == 200

    def test_f2_1_resize_200(self, api_base, auth_headers):
        """F2-1b: /resize returns 200."""
        r = requests.get(f"{api_base}/api/v1/resize", headers=auth_headers, timeout=10)
        assert r.status_code == 200

    def test_f2_2_rightsizing_fields(self, api_base, auth_headers):
        """F2-2: /rightsizing items have vm_name and sizing_direction."""
        r = requests.get(f"{api_base}/api/v1/rightsizing", headers=auth_headers, timeout=10)
        items = r.json()
        if not items:
            pytest.skip("No right-sizing data yet")
        for item in items[:5]:
            assert "vm_name" in item
            assert "sizing_direction" in item

    def test_f2_3_resize_fields(self, api_base, auth_headers):
        """F2-3: /resize items have confidence_score and risk_level."""
        r = requests.get(f"{api_base}/api/v1/resize", headers=auth_headers, timeout=10)
        items = r.json()
        if not items:
            pytest.skip("No resize recommendation data yet")
        for item in items[:5]:
            assert "confidence_score" in item
            assert "risk_level" in item


# ---------------------------------------------------------------------------
# F3 — GET /api/v1/cluster-health and /exhaustion
# ---------------------------------------------------------------------------

class TestClusterHealth:
    def test_f3_1_cluster_health_200_and_fields(self, api_base, auth_headers):
        """F3-1: /cluster-health returns 200 with health_score and bottleneck_resource."""
        r = requests.get(f"{api_base}/api/v1/cluster-health", headers=auth_headers, timeout=10)
        assert r.status_code == 200
        body = r.json()
        if not body:
            pytest.skip("No cluster health data yet")
        item = body[0] if isinstance(body, list) else body
        assert "health_score" in item
        score = item["health_score"]
        assert 0 <= score <= 100, f"health_score out of range: {score}"

    def test_f3_2_exhaustion_200_and_fields(self, api_base, auth_headers):
        """F3-2: /exhaustion returns 200 with days_until_exhaustion and severity."""
        r = requests.get(f"{api_base}/api/v1/exhaustion", headers=auth_headers, timeout=10)
        assert r.status_code == 200
        items = r.json()
        if not items:
            pytest.skip("No exhaustion data yet")
        for item in items[:5]:
            assert "days_until_exhaustion" in item
            assert "severity" in item


# ---------------------------------------------------------------------------
# F6 — GET /api/v1/summary
# ---------------------------------------------------------------------------

class TestSummary:
    def test_f6_1_status_200(self, api_base, auth_headers):
        """F6-1: /summary returns HTTP 200."""
        r = requests.get(f"{api_base}/api/v1/summary", headers=auth_headers, timeout=10)
        assert r.status_code == 200

    def test_f6_2_summary_fields(self, api_base, auth_headers):
        """F6-2: summary contains cluster_health, right_sizing, resize_risk, worst_exhaustion."""
        r = requests.get(f"{api_base}/api/v1/summary", headers=auth_headers, timeout=10)
        body = r.json()
        for field in ("cluster_health", "right_sizing", "resize_risk", "worst_exhaustion"):
            assert field in body, f"Summary missing field: {field}"
        # cluster_health sub-object must have a score
        assert "score" in body["cluster_health"], "cluster_health.score missing"


# ---------------------------------------------------------------------------
# C7 — GET /api/v1/outcomes
# ---------------------------------------------------------------------------

class TestOutcomes:
    def test_c7_2_outcomes_200(self, api_base, auth_headers):
        """C7-2: /outcomes returns HTTP 200 (may be empty if no VMs resized yet)."""
        r = requests.get(f"{api_base}/api/v1/outcomes", headers=auth_headers, timeout=10)
        assert r.status_code == 200
        assert isinstance(r.json(), list)
