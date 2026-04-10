-- =============================================================================
-- 015_sharing_prefs.sql
-- Data-sharing preferences table for SC//Foresight-Central integration (H1/H2).
--
-- Stores the customer's opt-in configuration for pushing anonymised delta
-- snapshots to a remote SC//Foresight-Central viewer instance.
--
-- Anonymisation toggles let the customer select which fields are pseudonymised
-- before transmission.  The HMAC key is generated on first enable and never
-- changes, ensuring pseudonyms remain stable across pushes (timeseries coherence
-- is preserved in the viewer even when names are anonymised).
--
-- Idempotent: safe to run against an existing database.
--
-- Note on grants: the sentinel user is the database owner, so no explicit
-- GRANT statements are required — the owner already has full privileges on
-- every object it creates.
-- =============================================================================

CREATE TABLE IF NOT EXISTS config.sharing_prefs (
    id                  SERIAL PRIMARY KEY,
    cluster_id          TEXT NOT NULL DEFAULT 'default',

    -- Master enable switch.  Nothing is transmitted when FALSE.
    sharing_enabled     BOOLEAN NOT NULL DEFAULT FALSE,

    -- HTTPS URL of the SC//Foresight-Central viewer endpoint.
    viewer_url          TEXT,

    -- Long-lived bearer token issued by the viewer on enrollment.
    bearer_token        TEXT,

    -- Sync state tracking
    last_sync_at        TIMESTAMPTZ,        -- timestamp of the last successful push
    last_sync_status    TEXT,               -- 'ok' | 'error' | 'pending'
    last_sync_error     TEXT,               -- error message when last_sync_status = 'error'

    -- Anonymisation toggles (customer-selectable per field)
    anon_vm_names       BOOLEAN NOT NULL DEFAULT FALSE,
    anon_node_uuids     BOOLEAN NOT NULL DEFAULT FALSE,
    anon_cluster_name   BOOLEAN NOT NULL DEFAULT FALSE,

    -- HMAC-SHA256 key used to generate stable pseudonyms.
    -- Set once on first enable; intentionally never rotated so that viewer
    -- timeseries remain coherent across multiple pushes.
    anon_hmac_key       TEXT,

    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Seed the default row so Sentinel can read prefs immediately without
-- requiring a UI action first.
INSERT INTO config.sharing_prefs (cluster_id)
VALUES ('default')
ON CONFLICT DO NOTHING;
