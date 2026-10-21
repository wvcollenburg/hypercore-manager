-- =============================================================================
-- 026_backup_incremental.sql
-- Incremental backups through the backup proxy: proxies per cluster, chains and
-- points per plan, plan modes and retention, extra run columns, learned VM
-- definition templates. Idempotent.
-- =============================================================================

-- plans: mode and retention knobs
ALTER TABLE backup.plans ADD COLUMN IF NOT EXISTS mode            TEXT        NOT NULL DEFAULT 'export';
ALTER TABLE backup.plans ADD COLUMN IF NOT EXISTS full_every_n    INTEGER     NOT NULL DEFAULT 14;
ALTER TABLE backup.plans ADD COLUMN IF NOT EXISTS retention_days  INTEGER;
ALTER TABLE backup.plans ADD COLUMN IF NOT EXISTS retired_at      TIMESTAMPTZ;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_backup_plans_mode') THEN
        ALTER TABLE backup.plans ADD CONSTRAINT ck_backup_plans_mode CHECK (mode IN ('export', 'incremental'));
    END IF;
END $$;
-- the destination is optional now: shares/targets are assigned per folder, legacy plans keep theirs
ALTER TABLE backup.plans ALTER COLUMN path_uri_base DROP NOT NULL;

-- proxies: one or more per cluster; the self-proxy of the stack is registered like any other
CREATE TABLE IF NOT EXISTS backup.proxies (
    id               BIGSERIAL   PRIMARY KEY,
    cluster_id       UUID        NOT NULL REFERENCES config.clusters (cluster_id) ON DELETE CASCADE,
    name             TEXT        NOT NULL,
    url              TEXT        NOT NULL,                 -- http://proxy:8083 or https://10.0.0.5:8083
    token_enc        TEXT,                                 -- Fernet-encrypted bearer token
    kind             TEXT        NOT NULL DEFAULT 'remote', -- self | remote
    vm_uuid          TEXT,                                 -- the proxy's own VM on that cluster (clone target)
    tls_fingerprint  TEXT,                                 -- sha256 of the pinned certificate (https, self-signed)
    tls_verify       BOOLEAN     NOT NULL DEFAULT FALSE,   -- verify with the system CA store instead (customer cert)
    enabled          BOOLEAN     NOT NULL DEFAULT TRUE,
    status           TEXT,                                 -- ok | error | unknown
    status_detail    TEXT,
    last_seen_at     TIMESTAMPTZ,
    version          TEXT,
    capabilities     JSONB,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (cluster_id, url)
);

-- chains: one active chain per plan (a new full starts a new chain; old ones retire and expire)
CREATE TABLE IF NOT EXISTS backup.chains (
    id                   BIGSERIAL   PRIMARY KEY,
    plan_id              BIGINT      REFERENCES backup.plans (id) ON DELETE SET NULL,
    cluster_id           UUID        NOT NULL REFERENCES config.clusters (cluster_id) ON DELETE CASCADE,
    vm_uuid              TEXT        NOT NULL,
    vm_name              TEXT        NOT NULL,
    target_id            BIGINT      REFERENCES backup.targets (id) ON DELETE SET NULL,
    folder_slug          TEXT        NOT NULL,
    chain_dir            TEXT        NOT NULL,              -- relative to the target root: <folder>/<vm>_<uuid8>.chain
    status               TEXT        NOT NULL DEFAULT 'active',   -- active | retired | deleted
    anchor_snapshot_uuid TEXT,                              -- HyperCore snapshot that is the CBT reference
    last_serials         JSONB,                             -- device uuid -> serial at the last point
    next_seq             INTEGER     NOT NULL DEFAULT 0,
    runs_since_full      INTEGER     NOT NULL DEFAULT 0,
    active_clones        JSONB,                             -- clone uuids on the proxy during a run (swept on restart)
    retention_points     INTEGER,
    retention_days       INTEGER,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_backup_chains_plan ON backup.chains (plan_id, id DESC);
CREATE INDEX IF NOT EXISTS ix_backup_chains_vm   ON backup.chains (cluster_id, vm_uuid);

CREATE TABLE IF NOT EXISTS backup.points (
    id             BIGSERIAL   PRIMARY KEY,
    chain_id       BIGINT      NOT NULL REFERENCES backup.chains (id) ON DELETE CASCADE,
    seq            INTEGER     NOT NULL,
    kind           TEXT        NOT NULL,                    -- full | delta
    dir            TEXT        NOT NULL,                    -- base | delta-0001 | ...
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    serials        JSONB,                                   -- device uuid -> serial
    blocks         BIGINT,
    bytes_written  BIGINT,
    duration_s     REAL,
    files          JSONB,                                   -- [{uuid,type,file,size,sha256}]
    definition     JSONB,                                   -- VM record subset incl. NICs/MACs, tags, boot order
    run_id         BIGINT,
    status         TEXT        NOT NULL DEFAULT 'ok',       -- ok | pruned
    verified_at    TIMESTAMPTZ,
    UNIQUE (chain_id, seq)
);

-- runs: what kind of run it was and what it produced
ALTER TABLE backup.runs ADD COLUMN IF NOT EXISTS mode           TEXT;
ALTER TABLE backup.runs ADD COLUMN IF NOT EXISTS point_kind     TEXT;
ALTER TABLE backup.runs ADD COLUMN IF NOT EXISTS chain_id       BIGINT;
ALTER TABLE backup.runs ADD COLUMN IF NOT EXISTS point_id       BIGINT;
ALTER TABLE backup.runs ADD COLUMN IF NOT EXISTS proxy_id       BIGINT;
ALTER TABLE backup.runs ADD COLUMN IF NOT EXISTS blocks_changed BIGINT;
ALTER TABLE backup.runs ADD COLUMN IF NOT EXISTS bytes_written  BIGINT;

-- learned VM definition templates for machine types the shipped set does not cover
CREATE TABLE IF NOT EXISTS backup.os_templates (
    machine_type   TEXT        NOT NULL,
    os_profile     TEXT        NOT NULL,                    -- windows | other
    template       TEXT        NOT NULL,
    source         TEXT        NOT NULL DEFAULT 'learned',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (machine_type, os_profile)
);
