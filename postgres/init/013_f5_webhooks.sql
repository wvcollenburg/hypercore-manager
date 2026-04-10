-- =============================================================================
-- 013_f5_webhooks.sql
-- F5: Webhook/alert push notification delivery log.
--
-- audit.webhook_deliveries tracks every outbound webhook attempt so the
-- dispatcher can retry failures and dead-letter events that exhaust retries.
--
-- Lifecycle:
--   pending    — queued, not yet attempted (implicit; no row = not attempted)
--   sent       — HTTP 2xx received; terminal success
--   failed     — non-2xx or connection error; eligible for retry
--   dead_letter — max retries exhausted; requires manual intervention
--
-- One row is written per attempt.  The most recent row per
-- recommendation_event_id determines current status.
--
-- Idempotent: safe to run against an existing database.
-- =============================================================================

CREATE TABLE IF NOT EXISTS audit.webhook_deliveries (
    id                       BIGSERIAL   PRIMARY KEY,
    recommendation_event_id  BIGINT      NOT NULL
                                 REFERENCES audit.recommendation_events (id),
    cluster_id               UUID        NOT NULL,
    webhook_url              TEXT        NOT NULL,
    status                   TEXT        NOT NULL
                                 CHECK (status IN ('sent', 'failed', 'dead_letter')),
    attempt_count            INTEGER     NOT NULL DEFAULT 1,
    attempted_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    next_retry_at            TIMESTAMPTZ,             -- NULL when sent or dead_letter
    response_code            INTEGER,                 -- HTTP status code (NULL on connection error)
    response_body            TEXT,                    -- first 2000 chars of response
    error_message            TEXT                     -- exception message on connection error
);

-- Efficient lookup for the dispatcher: find events that need delivery or retry.
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_event
    ON audit.webhook_deliveries (recommendation_event_id, attempted_at DESC);

CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_retry
    ON audit.webhook_deliveries (next_retry_at)
    WHERE status = 'failed';
