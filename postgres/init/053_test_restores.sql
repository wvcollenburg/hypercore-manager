-- =============================================================================
-- 053_test_restores.sql
-- Test restores (plans/PLAN-test-restores.md, T1 + T2, 2026-11-21).
--
-- A test plan belongs to a folder: which VMs (fixed, and/or N at random from the folder's backed-up VMs),
-- how often, which level (boot: NICs disconnected; network: NICs on an isolated test VLAN plus ping and port
-- checks run from the proxy's test NIC). Each test restores the newest point as a new VM on the folder's DR
-- cluster (else the VM's own cluster), waits for a sign of life (guest agent, else the console screen),
-- records the verdict with a screenshot, and deletes the VM. Idempotent.
-- =============================================================================

CREATE TABLE IF NOT EXISTS backup.test_plans (
    id                SERIAL      PRIMARY KEY,
    folder_id         UUID        REFERENCES config.folders (folder_id) ON DELETE CASCADE,   -- NULL: unfiled clusters
    name              TEXT        NOT NULL,
    enabled           BOOLEAN     NOT NULL DEFAULT TRUE,
    interval_days     INTEGER     NOT NULL DEFAULT 7 CHECK (interval_days BETWEEN 1 AND 90),
    hours_from        TEXT        NOT NULL DEFAULT '01:00',      -- for a test on a production cluster
    hours_until       TEXT        NOT NULL DEFAULT '06:00',
    random_count      INTEGER     NOT NULL DEFAULT 0 CHECK (random_count BETWEEN 0 AND 50),
    rest_weeks        INTEGER     NOT NULL DEFAULT 4 CHECK (rest_weeks BETWEEN 1 AND 52),
    level             TEXT        NOT NULL DEFAULT 'boot' CHECK (level IN ('boot', 'network')),
    test_vlan         INTEGER     CHECK (test_vlan BETWEEN 0 AND 4094),
    probe_address     TEXT,                                      -- the proxy's address on the test VLAN, e.g. 172.16.0.250/25
    default_ports     INTEGER[]   NOT NULL DEFAULT '{}',
    timeout_minutes   INTEGER     NOT NULL DEFAULT 10 CHECK (timeout_minutes BETWEEN 2 AND 120),
    keep_failed_hours INTEGER     CHECK (keep_failed_hours BETWEEN 1 AND 168),   -- NULL: off (delete at once)
    byte_budget_gb    INTEGER     CHECK (byte_budget_gb > 0),                    -- NULL: off (no limit)
    last_run_at       TIMESTAMPTZ,                                -- when the last round was queued
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by        TEXT
);
CREATE INDEX IF NOT EXISTS ix_test_plans_folder ON backup.test_plans (folder_id);

-- fixed members and exclusions of a plan (the random pool is every backed-up VM of the folder minus these)
CREATE TABLE IF NOT EXISTS backup.test_plan_vms (
    plan_id     INTEGER  NOT NULL REFERENCES backup.test_plans (id) ON DELETE CASCADE,
    cluster_id  UUID     NOT NULL,
    vm_uuid     TEXT     NOT NULL,
    vm_name     TEXT,
    mode        TEXT     NOT NULL CHECK (mode IN ('fixed', 'exclude')),
    ports       INTEGER[],                                        -- network level: this VM's ports (NULL: the plan's)
    PRIMARY KEY (plan_id, cluster_id, vm_uuid)
);

-- one row per VM test
CREATE TABLE IF NOT EXISTS backup.test_runs (
    id                 BIGSERIAL   PRIMARY KEY,
    plan_id            INTEGER     REFERENCES backup.test_plans (id) ON DELETE SET NULL,
    folder_id          UUID,
    round_at           TIMESTAMPTZ,                               -- the round (plan's last_run_at) it belongs to
    cluster_id         UUID        NOT NULL,                      -- the VM's own cluster
    vm_uuid            TEXT        NOT NULL,
    vm_name            TEXT,
    picked             TEXT,                                      -- fixed | random
    level              TEXT        NOT NULL DEFAULT 'boot',
    status             TEXT        NOT NULL DEFAULT 'queued',     -- queued | running | passed | failed | skipped | error
    verdict            TEXT,                                      -- agent | screen | ... (how it passed or failed)
    agent              TEXT,                                      -- the source VM's guest agent: available | unavailable | unknown
    point_id           BIGINT,
    point_created_at   TIMESTAMPTZ,
    target_cluster_id  UUID,
    proxy_id           BIGINT,
    restore_id         BIGINT,
    restored_vm_uuid   TEXT,
    bytes              BIGINT,
    checks             JSONB,                                     -- network level: [{ip, ping, ports: {port: ok}}]
    screenshot         BYTEA,
    detail             TEXT,
    queued_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at         TIMESTAMPTZ,
    restored_at        TIMESTAMPTZ,
    booted_at          TIMESTAMPTZ,
    finished_at        TIMESTAMPTZ,
    keep_until         TIMESTAMPTZ,                               -- a failed VM kept for inspection until then
    cleaned_at         TIMESTAMPTZ                                -- the test VM was deleted
);
CREATE INDEX IF NOT EXISTS ix_test_runs_vm ON backup.test_runs (cluster_id, vm_uuid, id DESC);
CREATE INDEX IF NOT EXISTS ix_test_runs_status ON backup.test_runs (status) WHERE status IN ('queued', 'running');
CREATE INDEX IF NOT EXISTS ix_test_runs_folder ON backup.test_runs (folder_id, id DESC);

-- alerts: test kinds next to the verification ones, per VM
ALTER TABLE backup.verify_alerts ADD COLUMN IF NOT EXISTS vm_uuid TEXT;
ALTER TABLE backup.verify_alerts DROP CONSTRAINT IF EXISTS verify_alerts_kind_check;
ALTER TABLE backup.verify_alerts ADD CONSTRAINT verify_alerts_kind_check
    CHECK (kind IN ('failed', 'overdue', 'test_failed', 'test_overdue'));
CREATE UNIQUE INDEX IF NOT EXISTS ux_verify_alerts_open_test
    ON backup.verify_alerts (cluster_id, vm_uuid, kind) WHERE resolved_at IS NULL AND kind IN ('test_failed', 'test_overdue');
