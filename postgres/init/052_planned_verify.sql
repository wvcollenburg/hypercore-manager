-- =============================================================================
-- 052_planned_verify.sql
-- Planned chain verification per folder (plans/PLAN-chain-verify.md, 2026-11-21).
--
-- Replaces the hard-coded Sunday 05:00 verify. Every restore point is re-hashed once per interval, point by
-- point, as a proxy job that takes the proxy's lock; production proxies only inside the folder's hours and
-- outside backup work, proxies on a cluster marked as DR around the clock. A failed or overdue point raises
-- an alert row; the dashboard mails it to the closest admin and shows it on the cluster card. Idempotent.
-- =============================================================================

-- per folder; no row = the defaults. folder_id NULL = the unfiled clusters.
CREATE TABLE IF NOT EXISTS backup.verify_settings (
    id                    SERIAL      PRIMARY KEY,
    folder_id             UUID        REFERENCES config.folders (folder_id) ON DELETE CASCADE,
    interval_days         INTEGER     NOT NULL DEFAULT 7 CHECK (interval_days BETWEEN 0 AND 90),   -- 0 = off
    hours_from            TEXT        NOT NULL DEFAULT '01:00',                                    -- local to each cluster
    hours_until           TEXT        NOT NULL DEFAULT '06:00',
    dr_during_backups     BOOLEAN     NOT NULL DEFAULT FALSE,   -- DR proxies keep verifying while backups write the share
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by            TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_verify_settings_folder
    ON backup.verify_settings (COALESCE(folder_id, '00000000-0000-0000-0000-000000000000'::uuid));

-- a cluster that is normally passive (the folder's DR cluster): its proxies verify around the clock
ALTER TABLE config.clusters ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'production';
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'clusters_role_check') THEN
        ALTER TABLE config.clusters ADD CONSTRAINT clusters_role_check CHECK (role IN ('production', 'dr'));
    END IF;
END $$;

-- last verification attempt of a point, passed or not (verified_at stays the last pass)
ALTER TABLE backup.points ADD COLUMN IF NOT EXISTS verify_checked_at TIMESTAMPTZ;
UPDATE backup.points SET verify_checked_at = verified_at WHERE verify_checked_at IS NULL AND verified_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_backup_points_verify ON backup.points (verify_checked_at NULLS FIRST) WHERE status = 'ok';

-- one row per point verification the scheduler ran (the history on the Runs tab)
CREATE TABLE IF NOT EXISTS backup.verify_runs (
    id            BIGSERIAL   PRIMARY KEY,
    folder_id     UUID,
    cluster_id    UUID,                    -- the chain's cluster
    proxy_id      BIGINT,
    proxy_name    TEXT,
    proxy_role    TEXT,                    -- production | dr: where it ran
    chain_id      BIGINT,
    point_id      BIGINT,
    vm_name       TEXT,
    seq           INTEGER,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at   TIMESTAMPTZ,
    status        TEXT        NOT NULL DEFAULT 'running',   -- running | ok | failed | error | stopped
    bytes         BIGINT,
    duration_s    REAL,
    message       TEXT
);
CREATE INDEX IF NOT EXISTS ix_verify_runs_folder ON backup.verify_runs (folder_id, id DESC);
CREATE INDEX IF NOT EXISTS ix_verify_runs_cluster ON backup.verify_runs (cluster_id, id DESC);

-- failed and overdue verifications: the cluster card shows the open ones, the dashboard mails each once
CREATE TABLE IF NOT EXISTS backup.verify_alerts (
    id            BIGSERIAL   PRIMARY KEY,
    kind          TEXT        NOT NULL CHECK (kind IN ('failed', 'overdue')),
    folder_id     UUID,
    cluster_id    UUID,
    chain_id      BIGINT,
    point_id      BIGINT,
    vm_name       TEXT,
    seq           INTEGER,
    detail        TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at   TIMESTAMPTZ,             -- passed again / verified again / data deleted
    mail_claimed_at TIMESTAMPTZ,           -- a dashboard worker took it
    mailed_at     TIMESTAMPTZ,
    mail_result   TEXT                     -- who it went to, or why it did not
);
CREATE INDEX IF NOT EXISTS ix_verify_alerts_open ON backup.verify_alerts (cluster_id) WHERE resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_verify_alerts_mail ON backup.verify_alerts (created_at) WHERE mail_claimed_at IS NULL;
-- one open 'failed' alert per point (failing again raises no second one), one open 'overdue' alert per chain
CREATE UNIQUE INDEX IF NOT EXISTS ux_verify_alerts_open_failed
    ON backup.verify_alerts (point_id) WHERE resolved_at IS NULL AND kind = 'failed';
CREATE UNIQUE INDEX IF NOT EXISTS ux_verify_alerts_open_overdue
    ON backup.verify_alerts (chain_id) WHERE resolved_at IS NULL AND kind = 'overdue';
