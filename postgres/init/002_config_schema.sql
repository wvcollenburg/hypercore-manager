-- =============================================================================
-- 002_config_schema.sql
-- Configuration schema: cluster registry, threshold overrides, module toggles,
-- and live-tunable settings store.
-- Idempotent: safe to run against an existing database.
-- =============================================================================

CREATE TABLE IF NOT EXISTS config.clusters (
    cluster_id      UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    cluster_name    TEXT        NOT NULL,
    hc_host         TEXT,
    registered_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active       BOOLEAN     NOT NULL DEFAULT TRUE
);

-- Per-cluster threshold overrides (Phase 1: env vars only; activates Phase 2+)
CREATE TABLE IF NOT EXISTS config.threshold_overrides (
    id              SERIAL      PRIMARY KEY,
    cluster_id      UUID        NOT NULL REFERENCES config.clusters (cluster_id),
    config_key      TEXT        NOT NULL,
    config_value    TEXT        NOT NULL,
    description     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (cluster_id, config_key)
);

-- Per-cluster module toggles (Phase 1: env vars only; activates Phase 2+)
-- Known module names: forecast_30d, forecast_90d, rightsizing_c1,
-- rightsizing_c2, seasonality_b8, exhaustion_b6
CREATE TABLE IF NOT EXISTS config.module_toggles (
    id              SERIAL      PRIMARY KEY,
    cluster_id      UUID        NOT NULL REFERENCES config.clusters (cluster_id),
    module_name     TEXT        NOT NULL,
    enabled         BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (cluster_id, module_name)
);

-- Live-editable key/value settings used by Balancer, Collector, and Dashboard.
-- Replaces the former SQLite tunables.db on the sc-config shared volume.
-- Phase 1: seeded from env vars on first service startup; database wins after that.
CREATE TABLE IF NOT EXISTS config.tunables (
    cluster_id  UUID        NOT NULL REFERENCES config.clusters (cluster_id),
    key         TEXT        NOT NULL,
    value       TEXT        NOT NULL,
    type        TEXT        NOT NULL,   -- 'bool' | 'int' | 'float' | 'list' | 'str'
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (cluster_id, key)
);

-- Default cluster seed for Phase 1 single-cluster deployment.
-- cluster_id matches DEFAULT_CLUSTER_ID in config_db.py.
INSERT INTO config.clusters (cluster_id, cluster_name)
VALUES ('00000000-0000-0000-0000-000000000001', 'default')
ON CONFLICT DO NOTHING;
