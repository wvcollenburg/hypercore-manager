-- =============================================================================
-- 040_backup_notice_mutes.sql
-- Cluster notices (a terminated HyperCore update, shown as a warning on the cluster card) can be
-- muted for a number of days. A mute is per cluster and per notice key; the key names the specific
-- event (e.g. the build a terminated update aimed at), so a new event shows again. Shared by every
-- user. Idempotent.
-- =============================================================================
CREATE TABLE IF NOT EXISTS backup.notice_mutes (
    cluster_id  TEXT        NOT NULL,
    key         TEXT        NOT NULL,
    muted_until TIMESTAMPTZ NOT NULL,
    muted_by    TEXT,
    muted_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (cluster_id, key)
);
