-- =============================================================================
-- 027_backup_restores.sql
-- Restores from backup chains: one row per requested restore, with the policy
-- that was applied and the VM it produced. Idempotent.
-- =============================================================================

CREATE TABLE IF NOT EXISTS backup.restores (
    id              BIGSERIAL   PRIMARY KEY,
    kind            TEXT        NOT NULL DEFAULT 'vm',          -- vm | files
    point_id        BIGINT      REFERENCES backup.points (id) ON DELETE SET NULL,
    chain_id        BIGINT      REFERENCES backup.chains (id) ON DELETE SET NULL,
    cluster_id      UUID        NOT NULL REFERENCES config.clusters (cluster_id) ON DELETE CASCADE,
    vm_name         TEXT,                                       -- name of the backed-up VM
    target_name     TEXT,                                       -- requested name of the restored VM
    params          JSONB,                                      -- mac_policy, start, nic_connected, ...
    status          TEXT        NOT NULL DEFAULT 'RUNNING',     -- RUNNING | COMPLETE | ERROR
    message         TEXT,
    created_uuid    TEXT,                                       -- the restored VM
    requested_by    TEXT,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_backup_restores_cluster ON backup.restores (cluster_id, id DESC);
