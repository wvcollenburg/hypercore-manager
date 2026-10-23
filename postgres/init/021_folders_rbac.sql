-- =============================================================================
-- 021_folders_rbac.sql
-- Folders group clusters in the dashboard rail and carry access rights:
-- admins see every cluster; users see the clusters in folders they are members of.
-- A cluster without a folder is visible to admins only.
-- Idempotent: safe to run against an existing database.
-- =============================================================================

CREATE TABLE IF NOT EXISTS config.folders (
    folder_id   UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT        NOT NULL,
    colour      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_folders_name ON config.folders (lower(name));

ALTER TABLE config.clusters
    ADD COLUMN IF NOT EXISTS folder_id UUID REFERENCES config.folders (folder_id) ON DELETE SET NULL;

CREATE TABLE IF NOT EXISTS config.folder_members (
    folder_id   UUID        NOT NULL REFERENCES config.folders (folder_id) ON DELETE CASCADE,
    user_id     UUID        NOT NULL REFERENCES config.users (user_id) ON DELETE CASCADE,
    granted_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (folder_id, user_id)
);
