-- =============================================================================
-- 022_exports_schema.sql
-- Exports module: scheduled VM exports to SMB/NFS with retention pruning, and VM
-- imports. Every row belongs to a registry cluster (config.clusters). Share
-- passwords are Fernet-encrypted with HCM_SECRET_KEY like cluster credentials.
-- Idempotent: safe to run against an existing database.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS exports;

CREATE TABLE IF NOT EXISTS exports.schedules (
    id             BIGSERIAL   PRIMARY KEY,
    cluster_id     UUID        NOT NULL REFERENCES config.clusters (cluster_id) ON DELETE CASCADE,
    vm_uuid        TEXT        NOT NULL,
    vm_name        TEXT        NOT NULL,
    path_uri_base  TEXT        NOT NULL,          -- credential-free, e.g. smb://nas.local/share/path
    smb_user       TEXT,                           -- optional; share username (NULL for NFS)
    smb_pass_enc   TEXT,                           -- optional; Fernet-encrypted share password
    prune_path     TEXT,                           -- optional local mount for NFS pruning
    retention      INTEGER     NOT NULL DEFAULT 7,
    run_time       TEXT        NOT NULL DEFAULT '03:00',
    compress       BOOLEAN     NOT NULL DEFAULT FALSE,
    enabled        BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (cluster_id, vm_uuid)
);

CREATE TABLE IF NOT EXISTS exports.runs (
    id             BIGSERIAL   PRIMARY KEY,
    schedule_id    BIGINT      NOT NULL REFERENCES exports.schedules (id) ON DELETE CASCADE,
    started_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at    TIMESTAMPTZ,
    status         TEXT        NOT NULL DEFAULT 'RUNNING',   -- RUNNING | COMPLETE | ERROR
    task_tag       TEXT,
    export_path    TEXT,
    message        TEXT
);
CREATE INDEX IF NOT EXISTS ix_exports_runs_schedule ON exports.runs (schedule_id, id DESC);

CREATE TABLE IF NOT EXISTS exports.imports (
    id             BIGSERIAL   PRIMARY KEY,
    cluster_id     UUID        NOT NULL REFERENCES config.clusters (cluster_id) ON DELETE CASCADE,
    source_uri     TEXT        NOT NULL,           -- credential-free, for display
    smb_user       TEXT,
    smb_pass_enc   TEXT,
    target_name    TEXT,                           -- requested name on the target (NULL = keep original)
    status         TEXT        NOT NULL DEFAULT 'RUNNING',   -- RUNNING | COMPLETE | ERROR
    task_tag       TEXT,
    created_uuid   TEXT,                           -- new VM's uuid once the import completes
    message        TEXT,
    started_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_exports_imports_cluster ON exports.imports (cluster_id, id DESC);
