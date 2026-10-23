-- =============================================================================
-- 029_cluster_api_version.sql
-- HyperCore REST v2 switch per cluster and the task feed the collector keeps when v2 is in use.
-- Idempotent.
-- =============================================================================

ALTER TABLE config.clusters ADD COLUMN IF NOT EXISTS api_version       TEXT NOT NULL DEFAULT 'auto';  -- auto | v1 | v2
ALTER TABLE config.clusters ADD COLUMN IF NOT EXISTS api_v2_available  BOOLEAN;                       -- NULL = not probed yet
ALTER TABLE config.clusters ADD COLUMN IF NOT EXISTS hc_version        TEXT;

DO $$ BEGIN
    ALTER TABLE config.clusters ADD CONSTRAINT clusters_api_version_chk CHECK (api_version IN ('auto', 'v1', 'v2'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- Tasks seen through GET /rest/v2/TaskTag: the active ones plus the recently finished, one row
-- per (cluster, task tag). The collector upserts them every poll; the dashboard reads them.
CREATE TABLE IF NOT EXISTS config.cluster_tasks (
    cluster_id    UUID        NOT NULL REFERENCES config.clusters (cluster_id) ON DELETE CASCADE,
    task_tag      TEXT        NOT NULL,
    kind          TEXT        NOT NULL,          -- move | export | import | snapshot | clone | start | stop | create | delete | other
    description   TEXT        NOT NULL,          -- human text with the parameters filled in
    vm_name       TEXT,
    object_uuid   TEXT,
    state         TEXT        NOT NULL,          -- QUEUED | RUNNING | COMPLETE | ERROR
    progress      INTEGER     NOT NULL DEFAULT 0,
    node_uuids    JSONB       NOT NULL DEFAULT '[]'::jsonb,
    params        JSONB       NOT NULL DEFAULT '[]'::jsonb,
    created_at    TIMESTAMPTZ,
    modified_at   TIMESTAMPTZ,
    completed_at  TIMESTAMPTZ,
    seen_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (cluster_id, task_tag)
);
CREATE INDEX IF NOT EXISTS ix_cluster_tasks_state ON config.cluster_tasks (cluster_id, state, modified_at DESC);
