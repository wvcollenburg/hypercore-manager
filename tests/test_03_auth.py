"""
test_03_auth.py
Covers: F4 — Bearer token authentication (all four sub-cases).
"""
from __future__ import annotations

import os

import pytest
import requests


pytestmark = pytest.mark.usefixtures("sentinel_up")

_PROBE = "/api/v1/summary"


class TestBearerAuth:
    """F4: Bearer token authentication."""

    def test_f4_1_no_token_returns_401(self, api_base, token):
        """F4-1: request without any Authorization header → 401 (when token is set)."""
        if not token:
            pytest.skip("SENTINEL_API_TOKEN is empty — running in dev/no-auth mode")
        r = requests.get(f"{api_base}{_PROBE}", timeout=5)
        assert r.status_code == 401

    def test_f4_2_wrong_token_returns_401(self, api_base, token):
        """F4-2: request with wrong token → 401."""
        if not token:
            pytest.skip("SENTINEL_API_TOKEN is empty — running in dev/no-auth mode")
        r = requests.get(
            f"{api_base}{_PROBE}",
            headers={"Authorization": "Bearer wrong-token-xyz"},
            timeout=5,
        )
        assert r.status_code == 401

    def test_f4_3_correct_token_returns_200(self, api_base, auth_headers, token):
        """F4-3: request with correct token → 200."""
        if not token:
            pytest.skip("SENTINEL_API_TOKEN is empty — running in dev/no-auth mode")
        r = requests.get(f"{api_base}{_PROBE}", headers=auth_headers, timeout=5)
        assert r.status_code == 200

    def test_f4_4_dev_mode_no_auth_needed(self, api_base, token):
        """F4-4: when SENTINEL_API_TOKEN is empty, requests succeed without auth header."""
        if token:
            pytest.skip("SENTINEL_API_TOKEN is set — not in dev mode")
        r = requests.get(f"{api_base}{_PROBE}", timeout=5)
        assert r.status_code == 200
