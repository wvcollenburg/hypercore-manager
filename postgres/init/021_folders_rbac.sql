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

-- (a unique index on lower(name) lived here until 1.8.0. Migration 044 drops it: folder names are unique
--  per parent since then. It must not come back: the dashboard's runner applies this file again on an
--  install whose database was created by initdb, after 044 has seeded two nodes named 'Default', and
--  failed here for good, which blocked every later migration and every in-app update.)

ALTER TABLE config.clusters
    ADD COLUMN IF NOT EXISTS folder_id UUID REFERENCES config.folders (folder_id) ON DELETE SET NULL;

CREATE TABLE IF NOT EXISTS config.folder_members (
    folder_id   UUID        NOT NULL REFERENCES config.folders (folder_id) ON DELETE CASCADE,
    user_id     UUID        NOT NULL REFERENCES config.users (user_id) ON DELETE CASCADE,
    granted_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (folder_id, user_id)
);
