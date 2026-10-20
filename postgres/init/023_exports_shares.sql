-- =============================================================================
-- 023_exports_shares.sql
-- Shares are managed by admins and assigned to folders. A schedule or import then
-- needs no destination or credentials: the cluster's folder decides the share and
-- the subfolder on it, so users export and import without touching SMB details.
-- Idempotent: safe to run against an existing database.
-- =============================================================================

CREATE TABLE IF NOT EXISTS exports.shares (
    id            BIGSERIAL   PRIMARY KEY,
    name          TEXT        NOT NULL,
    uri_base      TEXT        NOT NULL,          -- smb://nas.local/share/path or nfs://nas.local/export
    username      TEXT,                          -- SMB only
    password_enc  TEXT,                          -- Fernet-encrypted (HCM_SECRET_KEY)
    prune_path    TEXT,                          -- NFS: bind mount of the same folder in the exports container
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_exports_shares_name ON exports.shares (lower(name));

ALTER TABLE config.folders
    ADD COLUMN IF NOT EXISTS share_id BIGINT REFERENCES exports.shares (id) ON DELETE SET NULL;

-- schedules created against a folder share carry no destination of their own
ALTER TABLE exports.schedules ALTER COLUMN path_uri_base DROP NOT NULL;
