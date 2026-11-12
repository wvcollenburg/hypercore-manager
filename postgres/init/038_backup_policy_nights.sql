-- =============================================================================
-- 038_backup_policy_nights.sql
-- The plan of one night of a backup schedule: per member the slot, planned start and predicted
-- finish the planner chose when the window opened (learned members are placed backwards from the
-- preferred finish minus the buffer; members still learning start at the first allowed minute).
-- Runs keep the planned start and predicted finish they were given, so predictions can be
-- compared with what happened. Idempotent.
-- =============================================================================
CREATE TABLE IF NOT EXISTS backup.policy_nights (
    policy_id         BIGINT      NOT NULL REFERENCES backup.policies (id) ON DELETE CASCADE,
    occurrence        DATE        NOT NULL,
    plan_id           BIGINT      NOT NULL REFERENCES backup.plans (id) ON DELETE CASCADE,
    cluster_id        UUID        NOT NULL,
    slot              INTEGER     NOT NULL,
    planned_start     TIMESTAMPTZ NOT NULL,
    predicted_finish  TIMESTAMPTZ NOT NULL,
    predicted_seconds REAL        NOT NULL,
    learned           BOOLEAN     NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (policy_id, occurrence, plan_id)
);

ALTER TABLE backup.runs ADD COLUMN IF NOT EXISTS planned_start    TIMESTAMPTZ;
ALTER TABLE backup.runs ADD COLUMN IF NOT EXISTS predicted_finish TIMESTAMPTZ;
