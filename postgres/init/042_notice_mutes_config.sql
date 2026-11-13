-- =============================================================================
-- 042_notice_mutes_config.sql
-- The mutes of cluster notices move from backup.notice_mutes (040) to config.notice_mutes: the
-- warning badges on the cluster card now carry HyperCore conditions and open console sessions as
-- well as the backup service's notices, so the table is no longer the backup module's.
-- Keys: '<backup notice key>' (e.g. a terminated update), 'condition:<name>:<node uuid>',
-- 'console:<vm uuid>'. Rows are copied over once; the old table is dropped. Idempotent.
-- =============================================================================
CREATE TABLE IF NOT EXISTS config.notice_mutes (
    cluster_id  TEXT        NOT NULL,
    key         TEXT        NOT NULL,
    muted_until TIMESTAMPTZ NOT NULL,
    muted_by    TEXT,
    muted_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (cluster_id, key)
);

DO $$
BEGIN
    IF to_regclass('backup.notice_mutes') IS NOT NULL THEN
        INSERT INTO config.notice_mutes (cluster_id, key, muted_until, muted_by, muted_at)
        SELECT cluster_id, key, muted_until, muted_by, muted_at FROM backup.notice_mutes
        ON CONFLICT (cluster_id, key) DO NOTHING;
        DROP TABLE backup.notice_mutes;
    END IF;
END $$;
