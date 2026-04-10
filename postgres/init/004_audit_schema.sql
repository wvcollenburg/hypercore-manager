-- =============================================================================
-- 004_audit_schema.sql
-- Audit schema: immutable event log and human action tracking.
-- Audit records are kept indefinitely — no expires_at, no cleanup.
-- Idempotent: safe to run against an existing database.
-- =============================================================================

-- Immutable log of every recommendation emitted by Sentinel.
-- cluster_id is denormalised (no FK) so records survive cluster deregistration.
-- payload_snapshot is a full copy at emission time — self-contained even after
-- recommendations.* rows expire and are cleaned up.
CREATE TABLE IF NOT EXISTS audit.recommendation_events (
    id                  BIGSERIAL   PRIMARY KEY,
    cluster_id          UUID        NOT NULL,
    event_type          TEXT        NOT NULL
                            CHECK (event_type IN (
                                'forecast_snapshot',
                                'right_sizing',
                                'resource_exhaustion'
                            )),
    source_id           BIGINT      NOT NULL,
    source_table        TEXT        NOT NULL,
    severity            public.severity_level NOT NULL,
    emitted_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    payload_snapshot    JSONB       NOT NULL
);

-- Human actions on recommendations (Phase 2+ populates this).
-- Table exists now so no future migration is needed.
CREATE TABLE IF NOT EXISTS audit.action_log (
    id                          BIGSERIAL   PRIMARY KEY,
    cluster_id                  UUID        NOT NULL,
    recommendation_event_id     BIGINT      NOT NULL
                                    REFERENCES audit.recommendation_events (id),
    action_taken                TEXT        NOT NULL,
    action_by                   TEXT,
    action_at                   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notes                       TEXT
);
