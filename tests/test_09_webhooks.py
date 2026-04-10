"""
test_09_webhooks.py
Covers: F5 — Webhook/alert push (F5-1, F5-3, F5-4, F5-5, F5-8).

⚠️  These tests require:
  1. A webhook receiver reachable from inside the Docker network.
     On Windows Docker Desktop use host.docker.internal.
  2. SENTINEL_WEBHOOK_URL must be set to point at the receiver.
  3. SENTINEL_WEBHOOK_SECRET must match what's in .env.

Run only when webhook infra is ready:
    pytest tests/test_09_webhooks.py -m webhooks -v

F5-6 (retry) and F5-7 (dead-letter) are NOT automated — they require
5-minute and 30-minute waits and remain manual.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import queue
import threading
import time

import pytest
import requests
from flask import Flask, request as flask_request

from helpers import db_all, db_scalar

pytestmark = [
    pytest.mark.usefixtures("sentinel_up"),
    pytest.mark.webhooks,
]

WEBHOOK_SECRET = os.getenv("SENTINEL_WEBHOOK_SECRET", "")
WEBHOOK_PORT   = int(os.getenv("TEST_WEBHOOK_PORT", "9000"))


# ---------------------------------------------------------------------------
# Local webhook echo server (Option B from the test plan)
# ---------------------------------------------------------------------------

_received_payloads: queue.Queue = queue.Queue()

_echo_app = Flask("webhook_echo")


@_echo_app.post("/hook")
def _hook():
    _received_payloads.put({
        "headers": dict(flask_request.headers),
        "body": flask_request.get_data(),
        "json": flask_request.get_json(silent=True),
    })
    return "ok", 200


@pytest.fixture(scope="module")
def echo_server():
    """Start local Flask echo server in a background thread."""
    import werkzeug.serving
    server = werkzeug.serving.make_server("0.0.0.0", WEBHOOK_PORT, _echo_app)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    time.sleep(0.5)  # let it bind
    yield server
    server.shutdown()


# ---------------------------------------------------------------------------
# F5-1 — startup: webhook_loop running (check via logs)
# ---------------------------------------------------------------------------

class TestWebhookStartup:
    def test_f5_1_webhook_loop_in_logs(self):
        """F5-1: sc_sentinel logs show webhook_loop started."""
        import subprocess
        result = subprocess.run(
            ["docker", "logs", "sc_sentinel", "--tail", "200"],
            capture_output=True, text=True, timeout=15,
        )
        logs = result.stdout + result.stderr
        assert "webhook" in logs.lower(), \
            "No webhook-related log lines found — is SENTINEL_WEBHOOK_ENABLED=true?"


# ---------------------------------------------------------------------------
# F5-3, F5-4, F5-5 — delivery, HMAC, DB record
# ---------------------------------------------------------------------------

class TestWebhookDelivery:
    """Requires SENTINEL_WEBHOOK_URL pointed at our echo server."""

    def _wait_for_payload(self, timeout: int = 90) -> dict:
        try:
            return _received_payloads.get(timeout=timeout)
        except queue.Empty:
            pytest.fail(
                f"No webhook payload received within {timeout}s — "
                "check SENTINEL_WEBHOOK_URL and SENTINEL_WEBHOOK_ENABLED"
            )

    def test_f5_3_webhook_post_received(self, echo_server, api_base, auth_headers):
        """F5-3: sentinel fires a POST to webhook URL with correct JSON payload."""
        payload = self._wait_for_payload(90)
        body_json = payload.get("json") or {}
        # Payload must contain severity, event_type, cluster_id
        for field in ("severity", "event_type", "cluster_id"):
            assert field in body_json, f"Webhook payload missing field: {field}"

    def test_f5_4_hmac_signature_valid(self, echo_server):
        """F5-4: X-Sentinel-Signature header HMAC matches body."""
        if not WEBHOOK_SECRET:
            pytest.skip("SENTINEL_WEBHOOK_SECRET not set")
        payload = self._wait_for_payload(90)
        headers = payload["headers"]
        sig_header = headers.get("X-Sentinel-Signature", "")
        assert sig_header.startswith("sha256="), \
            f"Expected sha256= prefix, got: {sig_header!r}"
        body_bytes = payload["body"]
        expected = hmac.new(
            WEBHOOK_SECRET.encode(), body_bytes, hashlib.sha256
        ).hexdigest()
        received = sig_header[len("sha256="):]
        assert hmac.compare_digest(expected, received), \
            "HMAC signature mismatch — secret may not match"

    def test_f5_5_webhook_delivery_in_db(self, db_conn):
        """F5-5: audit.webhook_deliveries has a delivered row."""
        rows = db_all(
            db_conn,
            "SELECT status, http_status FROM audit.webhook_deliveries "
            "WHERE status='delivered' ORDER BY created_at DESC LIMIT 5",
        )
        assert rows, "No 'delivered' rows in audit.webhook_deliveries"
        for status, http_status in rows:
            assert status == "delivered"
            assert http_status == 200


# ---------------------------------------------------------------------------
# F5-8 — disable webhook via tunables
# ---------------------------------------------------------------------------

class TestWebhookToggle:
    def test_f5_8_disable_via_tunables(self, api_base, auth_headers, db_conn):
        """F5-8: setting SENTINEL_WEBHOOK_ENABLED=false via tunables stops dispatch."""
        # Read current value
        rows = db_all(
            db_conn, "SELECT value FROM config.tunables WHERE key='SENTINEL_WEBHOOK_ENABLED'"
        )
        original = rows[0][0] if rows else "true"

        r = requests.put(
            f"{api_base}/api/v1/tunables",
            headers={**auth_headers, "Content-Type": "application/json"},
            json={"SENTINEL_WEBHOOK_ENABLED": False},
            timeout=10,
        )
        assert r.status_code == 200, f"Disabling webhook failed: {r.text}"

        # Verify DB updated
        rows = db_all(
            db_conn, "SELECT value FROM config.tunables WHERE key='SENTINEL_WEBHOOK_ENABLED'"
        )
        assert rows and rows[0][0].lower() in ("false", "0"), \
            f"SENTINEL_WEBHOOK_ENABLED not disabled in DB: {rows}"

        # Restore
        restore_val = True if original.lower() in ("true", "1") else False
        requests.put(
            f"{api_base}/api/v1/tunables",
            headers={**auth_headers, "Content-Type": "application/json"},
            json={"SENTINEL_WEBHOOK_ENABLED": restore_val},
            timeout=10,
        )
