-- =============================================================================
-- 011_c7_outcome_tracking.sql
-- C7: Right-sizing outcome tracking.
--
-- Adds three columns to audit.recommendation_events so the outcome_tracker
-- can record whether a VM was actually resized after a recommendation.
--
-- acted_on_at          — when the outcome was detected (NULL = not yet checked)
-- action_outcome        — what happened: resized_up, resized_down, no_action
-- post_action_snapshot — JSONB snapshot of VM spec at outcome-check time
--
-- Idempotent: safe to run against an existing database.
-- =============================================================================

ALTER TABLE audit.recommendation_events
    ADD COLUMN IF NOT EXISTS acted_on_at          TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS action_outcome        TEXT
                                 CHECK (action_outcome IN (
                                     'resized_up',
                                     'resized_down',
                                     'no_action'
                                 )),
    ADD COLUMN IF NOT EXISTS post_action_snapshot  JSONB;

-- Index to efficiently find unresolved right_sizing events.
-- The outcome_tracker queries: event_type = 'right_sizing' AND acted_on_at IS NULL
-- AND emitted_at < NOW() - interval.
CREATE INDEX IF NOT EXISTS idx_rec_events_outcome_pending
    ON audit.recommendation_events (cluster_id, emitted_at)
    WHERE event_type = 'right_sizing' AND acted_on_at IS NULL;
