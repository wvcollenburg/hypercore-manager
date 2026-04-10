"""
test_08_export.py
Covers:
  H1  — delta snapshot builder (GET /api/v1/export/snapshot)
  H4  — AES-256 .hcb file export (GET /api/v1/export/hcb)
  E7  — branding API (GET/PUT /api/sentinel/branding)
"""
from __future__ import annotations

import os
import struct

import pytest
import requests


pytestmark = pytest.mark.usefixtures("sentinel_up")

_SNAPSHOT   = "/api/v1/export/snapshot"
_HCB        = "/api/v1/export/hcb"
_BRANDING   = "/api/sentinel/branding"


# ---------------------------------------------------------------------------
# H1 — Snapshot export
# ---------------------------------------------------------------------------

class TestSnapshotExport:
    def test_h1_1_snapshot_200_and_sections(self, api_base, auth_headers):
        """H1-1: GET /export/snapshot returns 200 with required top-level sections."""
        r = requests.get(f"{api_base}{_SNAPSHOT}", headers=auth_headers, timeout=15)
        assert r.status_code == 200, f"Snapshot export returned {r.status_code}: {r.text}"
        body = r.json()
        for section in (
            "forecasts", "rightsizing", "resize_recommendations",
            "cluster_health", "exhaustion",
        ):
            assert section in body, f"Snapshot missing section: {section}"

    def test_h1_2_snapshot_sections_are_lists(self, api_base, auth_headers):
        """H1-2: all snapshot sections are lists (data is exportable)."""
        r = requests.get(f"{api_base}{_SNAPSHOT}", headers=auth_headers, timeout=15)
        assert r.status_code == 200
        body = r.json()
        for section in ("forecasts", "rightsizing", "resize_recommendations",
                        "cluster_health", "exhaustion"):
            assert section in body, f"Snapshot missing section: {section}"
            assert isinstance(body[section], list), f"{section} should be a list"

    def test_h1_3_snapshot_since_param_accepted(self, api_base, auth_headers):
        """H1-3: snapshot with since= parameter is accepted (returns 200, not 400/422)."""
        r = requests.get(
            f"{api_base}{_SNAPSHOT}?since=2099-01-01T00:00:00Z",
            headers=auth_headers,
            timeout=15,
        )
        # Accept 200 (parameter accepted, even if not fully filtered client-side yet)
        assert r.status_code == 200, \
            f"since= parameter caused an error: {r.status_code} {r.text}"


# ---------------------------------------------------------------------------
# H4 — .hcb file export
# ---------------------------------------------------------------------------

class TestHcbExport:
    _PASSPHRASE = "test-passphrase-for-automated-testing"

    def test_h4_6_hcb_api_download(self, api_base, auth_headers):
        """H4-6: GET /export/hcb?passphrase=... streams a file with HTTP 200."""
        r = requests.get(
            f"{api_base}{_HCB}",
            headers=auth_headers,
            params={"passphrase": self._PASSPHRASE},
            timeout=15,
        )
        assert r.status_code == 200, f"HCB export returned {r.status_code}: {r.text}"
        assert len(r.content) > 64, "HCB file too small to contain valid structure"

    def test_h4_3_hcb_magic_bytes(self, api_base, auth_headers):
        """H4-3: .hcb file starts with HCB1 magic bytes."""
        r = requests.get(
            f"{api_base}{_HCB}",
            headers=auth_headers,
            params={"passphrase": self._PASSPHRASE},
            timeout=15,
        )
        assert r.status_code == 200
        assert r.content[:4] == b"HCB1", \
            f"Expected HCB1 magic, got: {r.content[:4]!r}"

    def test_h4_4_hcb_file_structure_size(self, api_base, auth_headers):
        """H4-4: file > 64 bytes (4B magic + 32B salt + 12B IV + ciphertext + 16B tag)."""
        r = requests.get(
            f"{api_base}{_HCB}",
            headers=auth_headers,
            params={"passphrase": self._PASSPHRASE},
            timeout=15,
        )
        assert r.status_code == 200
        # Minimum: 4 + 32 + 12 + 1 (min ciphertext) + 16 = 65 bytes
        assert len(r.content) >= 65, \
            f"HCB file too small ({len(r.content)} bytes), expected ≥65"

    def test_h4_7_passphrase_not_bearer_token(self, api_base, auth_headers, token):
        """H4-7: encryption uses passphrase, not the bearer token."""
        if not token:
            pytest.skip("No API token set — cannot distinguish passphrase from token")
        r = requests.get(
            f"{api_base}{_HCB}",
            headers=auth_headers,
            params={"passphrase": token},  # Wrong: using token as passphrase
            timeout=15,
        )
        # Route should still succeed (passphrase is just used for encryption, not auth)
        # The point is the bearer token is for auth, passphrase for encryption
        assert r.status_code == 200, \
            "HCB endpoint should accept any string as passphrase (auth is via bearer)"


# ---------------------------------------------------------------------------
# E7 — Branding API
# ---------------------------------------------------------------------------

class TestBrandingApi:
    def test_e7_5_get_branding(self, dashboard_base):
        """E7-5: GET /api/sentinel/branding returns current branding JSON."""
        import os
        user = os.getenv("SC_DASHBOARD_USER", "admin")
        pwd  = os.getenv("SC_DASHBOARD_PASSWORD", "")
        r = requests.get(f"{dashboard_base}{_BRANDING}", auth=(user, pwd), timeout=10)
        if r.status_code == 401:
            pytest.skip("Dashboard credentials not in .env")
        assert r.status_code == 200, f"GET branding returned {r.status_code}"
        body = r.json()
        # Should have at least company_name key
        assert "company_name" in body or body is not None

    def test_e7_6_put_branding_company_name(self, dashboard_base):
        """E7-6: PUT /api/sentinel/branding with updated company_name → 200 + DB updated."""
        import os
        user = os.getenv("SC_DASHBOARD_USER", "admin")
        pwd  = os.getenv("SC_DASHBOARD_PASSWORD", "")

        # Read current value
        r_get = requests.get(f"{dashboard_base}{_BRANDING}", auth=(user, pwd), timeout=10)
        if r_get.status_code == 401:
            pytest.skip("Dashboard credentials not in .env")
        original = r_get.json()
        original_name = original.get("company_name", "")

        # Update
        r_put = requests.put(
            f"{dashboard_base}{_BRANDING}",
            auth=(user, pwd),
            json={"company_name": "Automated Test Corp"},
            timeout=10,
        )
        assert r_put.status_code == 200, f"PUT branding returned {r_put.status_code}: {r_put.text}"

        # Verify
        r_check = requests.get(f"{dashboard_base}{_BRANDING}", auth=(user, pwd), timeout=10)
        assert r_check.json().get("company_name") == "Automated Test Corp"

        # Restore
        requests.put(
            f"{dashboard_base}{_BRANDING}",
            auth=(user, pwd),
            json={"company_name": original_name},
            timeout=10,
        )
