-- =============================================================================
-- 033_cluster_timezone.sql
-- The cluster's time zone (IANA name, read from HyperCore's /TimeZone on every probe) so plan
-- run times mean "local to the cluster". timezone_override = TRUE keeps an admin-typed zone
-- instead of the cluster's. NULL timezone = the stack's TZ. Idempotent.
-- =============================================================================
ALTER TABLE config.clusters ADD COLUMN IF NOT EXISTS timezone          TEXT;
ALTER TABLE config.clusters ADD COLUMN IF NOT EXISTS timezone_override BOOLEAN NOT NULL DEFAULT FALSE;
