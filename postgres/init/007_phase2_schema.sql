-- =============================================================================
-- 007_phase2_schema.sql — Phase 2 schema additions
-- =============================================================================
-- New tables:
--   recommendations.cluster_health_scores  — D1-D4, D6 cluster intelligence
--   recommendations.resize_recommendations — C3-C6 concrete resize advice
--
-- Also replaces the retention cleanup function to include the new tables.
-- =============================================================================


-- ─────────────────────────────────────────────────────────────────────────────
-- D1-D4, D6 — Cluster health scores
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS recommendations.cluster_health_scores (
    id                   BIGSERIAL PRIMARY KEY,
    cluster_id           UUID NOT NULL REFERENCES config.clusters(cluster_id),

    -- D1: aggregate cluster health (0–100, higher is healthier)
    health_score         DOUBLE PRECISION NOT NULL,

    -- D2: resource dimension that is the primary bottleneck
    bottleneck_resource  TEXT,

    -- D3: annualised growth rate per dimension (% per year, keyed by dimension name)
    growth_rates         JSONB NOT NULL DEFAULT '{}',

    -- D6: resource efficiency index (0–1: 1 = fully efficient, 0 = heavily wasteful)
    efficiency_index     DOUBLE PRECISION,

    -- per-dimension component health scores (dimension → 0–100)
    component_scores     JSONB NOT NULL DEFAULT '{}',

    -- D4: number of registered clusters (multi-cluster readiness marker)
    cluster_count        INTEGER NOT NULL DEFAULT 1,

    severity             severity_level NOT NULL DEFAULT 'INFO',
    calculated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at           TIMESTAMPTZ NOT NULL DEFAULT NOW() + INTERVAL '12 months'
);

CREATE INDEX IF NOT EXISTS idx_cluster_health_cluster_time
    ON recommendations.cluster_health_scores (cluster_id, calculated_at DESC);


-- ─────────────────────────────────────────────────────────────────────────────
-- C3-C6 — Concrete resize recommendations
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS recommendations.resize_recommendations (
    id                       BIGSERIAL PRIMARY KEY,
    cluster_id               UUID NOT NULL REFERENCES config.clusters(cluster_id),
    vm_uuid                  UUID NOT NULL,
    vm_name                  TEXT NOT NULL,
    node_uuid                UUID NOT NULL,
    sizing_direction         TEXT NOT NULL
        CHECK (sizing_direction IN ('over_provisioned', 'under_provisioned')),

    -- C3: concrete CPU change recommendation.
    --     Fraction: -0.50 = reduce current allocation by 50%; +0.25 = increase by 25%.
    suggested_cpu_change_pct DOUBLE PRECISION,
    --     Suggested steady-state average CPU% after the resize.
    target_cpu_avg_pct       DOUBLE PRECISION,

    -- C4: extended confidence score (0–1), composite + data-age adjustment
    confidence_score         DOUBLE PRECISION NOT NULL
        CHECK (confidence_score BETWEEN 0 AND 1),

    -- C5: CPU percentage points recovered by acting on this recommendation.
    --     Only populated for over-provisioned VMs; NULL for under-provisioned.
    capacity_reclaim_cpu_pct DOUBLE PRECISION,

    -- C6: operational risk of acting on this recommendation
    risk_level               TEXT NOT NULL
        CHECK (risk_level IN ('LOW', 'MEDIUM', 'HIGH')),
    risk_detail              JSONB NOT NULL DEFAULT '{}',

    -- FK to the Phase 1 right_sizing row that triggered this advice
    right_sizing_id          BIGINT REFERENCES recommendations.right_sizing(id),

    severity                 severity_level NOT NULL DEFAULT 'INFO',
    detected_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at               TIMESTAMPTZ NOT NULL DEFAULT NOW() + INTERVAL '12 months'
);

CREATE INDEX IF NOT EXISTS idx_resize_recs_cluster_vm
    ON recommendations.resize_recommendations (cluster_id, vm_uuid, detected_at DESC);

CREATE INDEX IF NOT EXISTS idx_resize_recs_severity
    ON recommendations.resize_recommendations (cluster_id, severity, detected_at DESC);


-- ─────────────────────────────────────────────────────────────────────────────
-- Replace the retention cleanup function to include Phase 2 tables
-- ─────────────────────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION public.cleanup_expired_recommendations()
RETURNS TABLE (table_name TEXT, rows_deleted BIGINT)
LANGUAGE plpgsql AS $$
BEGIN
    DELETE FROM recommendations.forecast_snapshots  WHERE expires_at < NOW();
    GET DIAGNOSTICS rows_deleted = ROW_COUNT;
    table_name := 'recommendations.forecast_snapshots'; RETURN NEXT;

    DELETE FROM recommendations.right_sizing        WHERE expires_at < NOW();
    GET DIAGNOSTICS rows_deleted = ROW_COUNT;
    table_name := 'recommendations.right_sizing'; RETURN NEXT;

    DELETE FROM recommendations.resource_exhaustion WHERE expires_at < NOW();
    GET DIAGNOSTICS rows_deleted = ROW_COUNT;
    table_name := 'recommendations.resource_exhaustion'; RETURN NEXT;

    DELETE FROM recommendations.cluster_health_scores WHERE expires_at < NOW();
    GET DIAGNOSTICS rows_deleted = ROW_COUNT;
    table_name := 'recommendations.cluster_health_scores'; RETURN NEXT;

    DELETE FROM recommendations.resize_recommendations WHERE expires_at < NOW();
    GET DIAGNOSTICS rows_deleted = ROW_COUNT;
    table_name := 'recommendations.resize_recommendations'; RETURN NEXT;
END;
$$;
