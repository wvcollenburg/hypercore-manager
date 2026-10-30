-- =============================================================================
-- 036_backup_policies.sql
-- Backup schedules shared by many VMs ("policies"; backup.schedules is the old name of
-- backup.plans, so it is not reused). A policy belongs to a folder (NULL = the clusters without a
-- folder) and holds one kind of member: incremental backups or exports. An RFC 5545 rule picks the
-- occurrence days; window_open / preferred_finish / blackouts are read in the policy's time zone.
-- A plan with policy_id set takes its timing and settings from the policy; its own run_time is
-- ignored. Runs started for a policy carry the policy and the occurrence (the local day the window
-- opened), and a notice ('blackout', 'late') when they ran into a blackout or past the preferred
-- finish. Idempotent.
-- =============================================================================
CREATE TABLE IF NOT EXISTS backup.policies (
    id                BIGSERIAL   PRIMARY KEY,
    folder_id         UUID        REFERENCES config.folders (folder_id) ON DELETE SET NULL,
    name              TEXT        NOT NULL,
    kind              TEXT        NOT NULL CHECK (kind IN ('incremental', 'export')),
    enabled           BOOLEAN     NOT NULL DEFAULT TRUE,
    timezone          TEXT        NOT NULL DEFAULT 'UTC',
    rrule             TEXT        NOT NULL DEFAULT 'FREQ=DAILY',
    starts_on         DATE        NOT NULL DEFAULT CURRENT_DATE,     -- anchors INTERVAL rules ("every 2nd week")
    window_open       TEXT        NOT NULL DEFAULT '22:00',
    preferred_finish  TEXT        NOT NULL DEFAULT '06:00',
    buffer_minutes    INTEGER     NOT NULL DEFAULT 60,
    blackouts         JSONB       NOT NULL DEFAULT '[]'::jsonb,       -- [{days:[mon..sun], from:'07:00', to:'19:00'}]
    retention         INTEGER     NOT NULL DEFAULT 14,
    retention_days    INTEGER,
    full_every_n      INTEGER     NOT NULL DEFAULT 14,
    compress          BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_backup_policies_folder ON backup.policies (folder_id);

ALTER TABLE backup.plans ADD COLUMN IF NOT EXISTS policy_id BIGINT REFERENCES backup.policies (id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS ix_backup_plans_policy ON backup.plans (policy_id);

ALTER TABLE backup.runs ADD COLUMN IF NOT EXISTS policy_id  BIGINT;
ALTER TABLE backup.runs ADD COLUMN IF NOT EXISTS occurrence DATE;
ALTER TABLE backup.runs ADD COLUMN IF NOT EXISTS notice     TEXT;
CREATE INDEX IF NOT EXISTS ix_backup_runs_policy ON backup.runs (policy_id, occurrence);
