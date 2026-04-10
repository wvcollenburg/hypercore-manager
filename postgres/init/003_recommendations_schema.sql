-- =============================================================================
-- 003_recommendations_schema.sql
-- Recommendations schema: forecast snapshots, VM right-sizing, resource
-- exhaustion estimates.
-- Idempotent: safe to run against an existing database.
--
-- expires_at uses DEFAULT rather than GENERATED ALWAYS AS because
-- TIMESTAMPTZ arithmetic is timezone-dependent and PostgreSQL rejects
-- it as a generated column expression (not immutable).
-- The writer never supplies expires_at explicitly, so the DEFAULT fires
-- on every INSERT and gives the correct 12-month retention window.
-- =============================================================================

-- Forecast snapshots: one row per Sentinel analysis run, per resource
-- dimension, per horizon. Never updated in place.
CREATE TABLE IF NOT EXISTS recommendations.forecast_snapshots (
    id                      BIGSERIAL   PRIMARY KEY,
    cluster_id              UUID        NOT NULL REFERENCES config.clusters (cluster_id),
    node_uuid               UUID,
    resource_dimension      public.resource_dimension   NOT NULL,
    forecast_horizon_days   INTEGER     NOT NULL CHECK (forecast_horizon_days > 0),
    snapshot_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    current_usage           DOUBLE PRECISION,
    current_capacity        DOUBLE PRECISION,
    forecast_value          DOUBLE PRECISION,
    confidence_score        DOUBLE PRECISION CHECK (confidence_score BETWEEN 0.0 AND 1.0),
    driving_metric          TEXT,
    exhaustion_date         DATE,
    severity                public.severity_level       NOT NULL,
    model_metadata          JSONB,
    expires_at              TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '12 months')
);

-- Per-VM right-sizing recommendations. Captures all three C2 signal scores.
CREATE TABLE IF NOT EXISTS recommendations.right_sizing (
    id                      BIGSERIAL   PRIMARY KEY,
    cluster_id              UUID        NOT NULL REFERENCES config.clusters (cluster_id),
    vm_uuid                 UUID        NOT NULL,
    vm_name                 TEXT        NOT NULL,
    node_uuid               UUID        NOT NULL,
    sizing_direction        TEXT        NOT NULL
                                CHECK (sizing_direction IN ('over_provisioned', 'under_provisioned')),
    severity                public.severity_level NOT NULL,
    analysis_window_days    INTEGER     NOT NULL,
    cpu_signal_score        DOUBLE PRECISION CHECK (cpu_signal_score        BETWEEN 0.0 AND 1.0),
    cpu_avg_usage_pct       DOUBLE PRECISION,
    cpu_p95_usage_pct       DOUBLE PRECISION,
    latency_signal_score    DOUBLE PRECISION CHECK (latency_signal_score    BETWEEN 0.0 AND 1.0),
    spike_signal_score      DOUBLE PRECISION CHECK (spike_signal_score      BETWEEN 0.0 AND 1.0),
    composite_score         DOUBLE PRECISION NOT NULL CHECK (composite_score BETWEEN 0.0 AND 1.0),
    signal_detail           JSONB,
    detected_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at              TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '12 months')
);

-- Fast-access exhaustion estimates derived from forecast_snapshots.
CREATE TABLE IF NOT EXISTS recommendations.resource_exhaustion (
    id                      BIGSERIAL   PRIMARY KEY,
    cluster_id              UUID        NOT NULL REFERENCES config.clusters (cluster_id),
    node_uuid               UUID,
    resource_dimension      public.resource_dimension   NOT NULL,
    days_until_exhaustion   INTEGER,
    exhaustion_date         DATE,
    current_usage_pct       DOUBLE PRECISION,
    growth_rate_per_day     DOUBLE PRECISION,
    severity                public.severity_level       NOT NULL,
    calculated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at              TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '12 months')
);
