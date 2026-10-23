-- =============================================================================
-- 028_backup_deployments.sql
-- Proxy VM deployments started from the dashboard (cloud-init). Idempotent.
-- =============================================================================

CREATE TABLE IF NOT EXISTS backup.deployments (
    id            BIGSERIAL   PRIMARY KEY,
    cluster_id    UUID        NOT NULL REFERENCES config.clusters (cluster_id) ON DELETE CASCADE,
    name          TEXT        NOT NULL,
    params        JSONB       NOT NULL,          -- sizes, vlan, ip, image, proxy image (never the token)
    status        TEXT        NOT NULL DEFAULT 'RUNNING',   -- RUNNING | COMPLETE | ERROR
    message       TEXT,
    vm_uuid       TEXT,
    proxy_id      BIGINT,
    requested_by  TEXT,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_backup_deployments_cluster ON backup.deployments (cluster_id, id DESC);
