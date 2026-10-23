-- =============================================================================
-- 020_cluster_registry.sql
-- Clusters are configured in the dashboard, not in .env.
--
-- Extends config.clusters into a registry with encrypted HyperCore credentials.
-- The original single-cluster row (…0001) becomes the "Defaults" template: it is
-- never polled, and its config.tunables rows are the defaults copied to every
-- newly registered cluster.
--
-- Also converts config.sharing_prefs.cluster_id from TEXT ('default') to UUID so
-- sharing preferences are per cluster like everything else.
--
-- Idempotent: safe to run against an existing database.
-- =============================================================================

ALTER TABLE config.clusters
    ADD COLUMN IF NOT EXISTS hc_username      TEXT,
    ADD COLUMN IF NOT EXISTS hc_password_enc  TEXT,          -- Fernet(HCM_SECRET_KEY)
    ADD COLUMN IF NOT EXISTS verify_tls       BOOLEAN     NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS hc_cluster_uuid  TEXT,          -- HyperCore's own UUID (GET /Cluster)
    ADD COLUMN IF NOT EXISTS enabled          BOOLEAN     NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS is_template      BOOLEAN     NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS colour           TEXT,
    ADD COLUMN IF NOT EXISTS last_seen_at     TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_error       TEXT;

-- the legacy single-cluster row becomes the defaults template
UPDATE config.clusters
   SET is_template = TRUE,
       enabled     = FALSE,
       cluster_name = 'Defaults',
       updated_at  = NOW()
 WHERE cluster_id = '00000000-0000-0000-0000-000000000001'
   AND is_template = FALSE;

INSERT INTO config.clusters (cluster_id, cluster_name, is_template, enabled)
VALUES ('00000000-0000-0000-0000-000000000001', 'Defaults', TRUE, FALSE)
ON CONFLICT (cluster_id) DO NOTHING;

CREATE UNIQUE INDEX IF NOT EXISTS ux_clusters_name
    ON config.clusters (lower(cluster_name));

-- sharing_prefs: TEXT 'default' -> UUID per cluster
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = 'config' AND table_name = 'sharing_prefs'
           AND column_name = 'cluster_id' AND data_type = 'text'
    ) THEN
        UPDATE config.sharing_prefs
           SET cluster_id = '00000000-0000-0000-0000-000000000001'
         WHERE cluster_id = 'default' OR cluster_id !~ '^[0-9a-fA-F-]{36}$';
        ALTER TABLE config.sharing_prefs ALTER COLUMN cluster_id DROP DEFAULT;
        ALTER TABLE config.sharing_prefs
            ALTER COLUMN cluster_id TYPE UUID USING cluster_id::uuid;
        ALTER TABLE config.sharing_prefs
            ALTER COLUMN cluster_id SET DEFAULT '00000000-0000-0000-0000-000000000001';
    END IF;
END $$;

-- the 015 seed had no unique constraint, so initdb plus the migration runner left two
-- 'default' rows on some installs; keep the oldest per cluster before adding the index
DELETE FROM config.sharing_prefs a
 USING config.sharing_prefs b
 WHERE a.cluster_id = b.cluster_id AND a.id > b.id;

CREATE UNIQUE INDEX IF NOT EXISTS ux_sharing_prefs_cluster
    ON config.sharing_prefs (cluster_id);
