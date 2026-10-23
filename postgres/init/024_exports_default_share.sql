-- =============================================================================
-- 024_exports_default_share.sql
-- One share may be the default for clusters that are in no folder; their exports
-- land in <share>/no_folder/. Idempotent.
-- =============================================================================

ALTER TABLE exports.shares
    ADD COLUMN IF NOT EXISTS default_for_unfiled BOOLEAN NOT NULL DEFAULT FALSE;

CREATE UNIQUE INDEX IF NOT EXISTS ux_exports_shares_default
    ON exports.shares (default_for_unfiled) WHERE default_for_unfiled;
