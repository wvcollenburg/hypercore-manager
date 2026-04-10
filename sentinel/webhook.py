"""
webhook.py — F5: Outbound webhook/alert push dispatcher.

Fires HTTP POST requests to a configurable target URL whenever new
CRITICAL or EMERGENCY audit.recommendation_events rows appear that have
not yet been successfully delivered.

Signing
-------
Each request is signed with HMAC-SHA256 over the raw JSON body bytes.
The signature is sent as the 'X-Sentinel-Signature' header in the form:

    X-Sentinel-Signature: sha256=<hex_digest>

Recipients verify the signature using their copy of SENTINEL_WEBHOOK_SECRET.
If the secret is empty, the header is omitted (dev / unsigned mode).

Retry schedule
--------------
Attempt 1  — immediate
Attempt 2  — 5 minutes after first failure
Attempt 3  — 30 minutes after second failure
> max      — marked dead_letter; requires manual re-trigger or ops action

Delivery log
------------
Every attempt (success or failure) is recorded in audit.webhook_deliveries.
The dispatcher queries this table to determine what still needs to be sent
and when retries are due, making the loop stateless and crash-safe.

Payload shape
-------------
{
    "sentinel_version": "3",
    "event_id":         <int>,
    "event_type":       <str>,
    "cluster_id":       <str>,
    "severity":         "CRITICAL" | "EMERGENCY",
    "emitted_at":       <iso-8601>,
    "payload":          { ...recommendation snapshot... }
}
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import psycopg2.extras

import config as cfg_module
import db

log = logging.getLogger(__name__)

# Retry delays per attempt number (1-indexed: attempt 2, 3, …)
_RETRY_DELAYS = [timedelta(minutes=5), timedelta(minutes=30)]


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------

def _sign(body: bytes, secret: str) -> str | None:
    """Return 'sha256=<hex>' HMAC signature, or None if secret is empty."""
    if not secret:
        return None
    mac = hmac.new(secret.encode(), body, hashlib.sha256)
    return f'sha256={mac.hexdigest()}'


# ---------------------------------------------------------------------------
# HTTP delivery
# ---------------------------------------------------------------------------

def _post(url: str, payload: dict, secret: str, timeout: int) -> tuple[int, str]:
    """
    POST payload as JSON to url.
    Returns (http_status_code, response_body_snippet).
    Raises on connection/timeout errors (caller catches).
    """
    body = json.dumps(payload, default=str).encode('utf-8')

    headers = {
        'Content-Type': 'application/json',
        'User-Agent':   'SC//Foresight-Sentinel/3',
    }
    sig = _sign(body, secret)
    if sig:
        headers['X-Sentinel-Signature'] = sig

    req  = urllib.request.Request(url, data=body, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp_body = resp.read(2000).decode('utf-8', errors='replace')
            return resp.status, resp_body
    except urllib.error.HTTPError as exc:
        resp_body = exc.read(2000).decode('utf-8', errors='replace')
        return exc.code, resp_body


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def _build_payload(event: dict) -> dict:
    """Build the outbound webhook JSON payload from an audit event row."""
    emitted = event.get('emitted_at')
    return {
        'sentinel_version': '3',
        'event_id':         event.get('id'),
        'event_type':       event.get('event_type'),
        'cluster_id':       str(event.get('cluster_id') or ''),
        'severity':         event.get('severity'),
        'emitted_at':       emitted.isoformat() if hasattr(emitted, 'isoformat') else str(emitted),
        'payload':          event.get('payload_snapshot') or {},
    }


def _next_retry_at(attempt_count: int) -> datetime | None:
    """Return the datetime for the next retry, or None when max retries reached."""
    idx = attempt_count - 1   # 0-indexed into _RETRY_DELAYS
    if idx < len(_RETRY_DELAYS):
        return datetime.now(timezone.utc) + _RETRY_DELAYS[idx]
    return None   # signals dead_letter


def _record_delivery(
    conn,
    event_id:     int,
    cluster_id:   str,
    url:          str,
    status:       str,
    attempt_count: int,
    response_code: int | None,
    response_body: str | None,
    error_message: str | None,
    next_retry_at: datetime | None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            '''
            INSERT INTO audit.webhook_deliveries
                (recommendation_event_id, cluster_id, webhook_url,
                 status, attempt_count, attempted_at,
                 next_retry_at, response_code, response_body, error_message)
            VALUES (%s, %s, %s, %s, %s, NOW(), %s, %s, %s, %s)
            ''',
            [
                event_id, cluster_id, url,
                status, attempt_count,
                next_retry_at, response_code,
                (response_body or '')[:2000] if response_body else None,
                (error_message or '')[:500]  if error_message else None,
            ],
        )


def _dispatch_event(cfg: cfg_module.Config, event: dict, attempt_count: int) -> None:
    """Attempt to deliver one audit event and record the result."""
    event_id   = event['id']
    cluster_id = str(event.get('cluster_id') or '')
    payload    = _build_payload(event)

    try:
        code, body = _post(
            cfg.webhook_url, payload, cfg.webhook_secret, cfg.webhook_timeout_seconds
        )
    except Exception as exc:
        # Connection/timeout error
        next_retry = _next_retry_at(attempt_count)
        status     = 'dead_letter' if next_retry is None else 'failed'
        log.warning(
            'Webhook delivery failed (connection error)',
            extra={
                'event_id': event_id, 'attempt': attempt_count,
                'status': status, 'error': str(exc),
            },
        )
        try:
            with db.connect(cfg) as conn:
                _record_delivery(
                    conn, event_id, cluster_id, cfg.webhook_url,
                    status, attempt_count,
                    None, None, str(exc), next_retry,
                )
        except Exception:
            log.exception('Failed to record webhook delivery error for event_id=%s', event_id)
        return

    if 200 <= code < 300:
        log.info(
            'Webhook delivered',
            extra={'event_id': event_id, 'attempt': attempt_count, 'http_status': code},
        )
        try:
            with db.connect(cfg) as conn:
                _record_delivery(
                    conn, event_id, cluster_id, cfg.webhook_url,
                    'sent', attempt_count, code, body, None, None,
                )
        except Exception:
            log.exception('Failed to record webhook success for event_id=%s', event_id)
    else:
        next_retry = _next_retry_at(attempt_count)
        status     = 'dead_letter' if next_retry is None else 'failed'
        log.warning(
            'Webhook delivery rejected',
            extra={
                'event_id': event_id, 'attempt': attempt_count,
                'http_status': code, 'status': status,
            },
        )
        try:
            with db.connect(cfg) as conn:
                _record_delivery(
                    conn, event_id, cluster_id, cfg.webhook_url,
                    status, attempt_count, code, body, None, next_retry,
                )
        except Exception:
            log.exception('Failed to record webhook failure for event_id=%s', event_id)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def dispatch_pending(cfg: cfg_module.Config) -> None:
    """
    One pass of the webhook dispatcher:
      1. Find new CRITICAL/EMERGENCY audit events with no successful delivery.
      2. Find previously failed deliveries whose next_retry_at has passed.
      3. Attempt delivery for each; record results.
    """
    if not cfg.webhook_enabled or not cfg.webhook_url:
        log.debug('Webhook dispatcher skipped — not configured')
        return

    try:
        with db.connect(cfg) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:

                # ── New events: CRITICAL/EMERGENCY with no delivery row yet ──
                cur.execute(
                    '''
                    SELECT re.id, re.cluster_id, re.event_type, re.severity,
                           re.emitted_at, re.payload_snapshot
                    FROM audit.recommendation_events re
                    WHERE re.cluster_id   = %s
                      AND re.severity     IN ('CRITICAL', 'EMERGENCY')
                      AND re.emitted_at   > NOW() - INTERVAL '1 hour' * %s
                      AND NOT EXISTS (
                          SELECT 1 FROM audit.webhook_deliveries wd
                          WHERE wd.recommendation_event_id = re.id
                            AND wd.status = 'sent'
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM audit.webhook_deliveries wd
                          WHERE wd.recommendation_event_id = re.id
                            AND wd.status = 'dead_letter'
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM audit.webhook_deliveries wd
                          WHERE wd.recommendation_event_id = re.id
                            AND wd.status = 'failed'
                      )
                    ORDER BY re.emitted_at ASC
                    LIMIT 50
                    ''',
                    [cfg.cluster_id, cfg.webhook_lookback_hours],
                )
                new_events = [dict(r) for r in cur.fetchall()]

                # ── Retry queue: failed deliveries past their next_retry_at ──
                cur.execute(
                    '''
                    SELECT DISTINCT ON (wd.recommendation_event_id)
                           re.id, re.cluster_id, re.event_type, re.severity,
                           re.emitted_at, re.payload_snapshot,
                           wd.attempt_count
                    FROM audit.webhook_deliveries wd
                    JOIN audit.recommendation_events re
                         ON re.id = wd.recommendation_event_id
                    WHERE wd.status         = 'failed'
                      AND wd.next_retry_at  < NOW()
                      AND re.cluster_id     = %s
                      AND NOT EXISTS (
                          SELECT 1 FROM audit.webhook_deliveries s
                          WHERE s.recommendation_event_id = re.id
                            AND s.status IN ('sent', 'dead_letter')
                      )
                    ORDER BY wd.recommendation_event_id, wd.attempted_at DESC
                    LIMIT 50
                    ''',
                    [cfg.cluster_id],
                )
                retry_events = [dict(r) for r in cur.fetchall()]

    except Exception:
        log.exception('Webhook dispatcher: failed to load pending events')
        return

    total = len(new_events) + len(retry_events)
    if total == 0:
        log.debug('Webhook dispatcher: nothing to send')
        return

    log.info(
        'Webhook dispatcher: dispatching events',
        extra={'new': len(new_events), 'retries': len(retry_events)},
    )

    for event in new_events:
        _dispatch_event(cfg, event, attempt_count=1)

    for event in retry_events:
        prev_attempts = event.get('attempt_count', 1)
        _dispatch_event(cfg, event, attempt_count=prev_attempts + 1)
