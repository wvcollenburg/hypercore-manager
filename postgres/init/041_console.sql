-- =============================================================================
-- 041_console.sql
-- VM console from HCM (backlog #1) and the open console session check (#2).
--
-- console.sessions   audit log of every console connection: interactive ones opened by a user
--                    in the browser and the automated screenshots (source 'system'). HyperCore
--                    does not log console access, so this is the only record of it.
-- console.check_settings  per cluster: is the open-session check on, how often, wake the screen
--                    before the grab or not, keep the last screenshot or only the verdict.
-- console.check_vm   per VM: the latest verdict of the check and since when a session is open;
--                    `excluded` VMs (kiosks, appliances meant to stay logged in) are never grabbed.
-- console.lock_refs  "learn locked state": reference screenshots of a VM's lock or login screen,
--                    as a perceptual hash plus a small thumbnail so an admin can see what was learnt.
-- Idempotent.
-- =============================================================================
CREATE SCHEMA IF NOT EXISTS console;

CREATE TABLE IF NOT EXISTS console.sessions (
    id          BIGSERIAL   PRIMARY KEY,
    cluster_id  TEXT        NOT NULL,
    vm_uuid     TEXT        NOT NULL,
    vm_name     TEXT,
    username    TEXT        NOT NULL,
    source      TEXT        NOT NULL DEFAULT 'user' CHECK (source IN ('user', 'system')),
    kind        TEXT        NOT NULL DEFAULT 'interactive' CHECK (kind IN ('interactive', 'screenshot')),
    client_ip   TEXT,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at    TIMESTAMPTZ,
    end_reason  TEXT,
    bytes_in    BIGINT      NOT NULL DEFAULT 0,
    bytes_out   BIGINT      NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS console_sessions_cluster_started ON console.sessions (cluster_id, started_at DESC);
CREATE INDEX IF NOT EXISTS console_sessions_vm ON console.sessions (vm_uuid, started_at DESC);

CREATE TABLE IF NOT EXISTS console.check_settings (
    cluster_id       TEXT        PRIMARY KEY,
    enabled          BOOLEAN     NOT NULL DEFAULT false,
    interval_minutes INTEGER     NOT NULL DEFAULT 30 CHECK (interval_minutes BETWEEN 5 AND 1440),
    wake             TEXT        NOT NULL DEFAULT 'none' CHECK (wake IN ('none', 'pointer', 'shift')),
    keep_images      BOOLEAN     NOT NULL DEFAULT false,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by       TEXT
);

CREATE TABLE IF NOT EXISTS console.check_vm (
    cluster_id     TEXT        NOT NULL,
    vm_uuid        TEXT        NOT NULL,
    vm_name        TEXT,
    state          TEXT        NOT NULL DEFAULT 'pending'
                       CHECK (state IN ('pending', 'locked', 'open', 'blank', 'unknown', 'error', 'stopped')),
    detail         TEXT,
    first_open_at  TIMESTAMPTZ,
    last_check_at  TIMESTAMPTZ,
    excluded       BOOLEAN     NOT NULL DEFAULT false,
    excluded_by    TEXT,
    image          BYTEA,
    image_at       TIMESTAMPTZ,
    PRIMARY KEY (cluster_id, vm_uuid)
);

CREATE TABLE IF NOT EXISTS console.lock_refs (
    id          BIGSERIAL   PRIMARY KEY,
    cluster_id  TEXT        NOT NULL,
    vm_uuid     TEXT        NOT NULL,
    hash        TEXT        NOT NULL,
    thumb       BYTEA,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by  TEXT
);
CREATE INDEX IF NOT EXISTS console_lock_refs_vm ON console.lock_refs (cluster_id, vm_uuid);
