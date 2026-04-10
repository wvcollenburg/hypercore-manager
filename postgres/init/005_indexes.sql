-- =============================================================================
-- 005_indexes.sql
-- Performance indexes for Sentinel query patterns.
-- Idempotent: all indexes use CREATE INDEX IF NOT EXISTS.
-- =============================================================================

-- recommendations.forecast_snapshots
CREATE INDEX IF NOT EXISTS idx_forecast_cluster_dim_at
    ON recommendations.forecast_snapshots (cluster_id, resource_dimension, forecast_horizon_days, snapshot_at DESC);

CREATE INDEX IF NOT EXISTS idx_forecast_node
    ON recommendations.forecast_snapshots (cluster_id, node_uuid, resource_dimension)
    WHERE node_uuid IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_forecast_expires
    ON recommendations.forecast_snapshots (expires_at);

CREATE INDEX IF NOT EXISTS idx_forecast_severity
    ON recommendations.forecast_snapshots (cluster_id, severity, snapshot_at DESC);

-- recommendations.right_sizing
CREATE INDEX IF NOT EXISTS idx_rightsizing_vm
    ON recommendations.right_sizing (cluster_id, vm_uuid, detected_at DESC);

CREATE INDEX IF NOT EXISTS idx_rightsizing_node
    ON recommendations.right_sizing (cluster_id, node_uuid, detected_at DESC);

CREATE INDEX IF NOT EXISTS idx_rightsizing_direction_severity
    ON recommendations.right_sizing (cluster_id, sizing_direction, severity, detected_at DESC);

CREATE INDEX IF NOT EXISTS idx_rightsizing_expires
    ON recommendations.right_sizing (expires_at);

-- recommendations.resource_exhaustion
CREATE INDEX IF NOT EXISTS idx_exhaustion_cluster_dim_at
    ON recommendations.resource_exhaustion (cluster_id, resource_dimension, calculated_at DESC);

CREATE INDEX IF NOT EXISTS idx_exhaustion_severity
    ON recommendations.resource_exhaustion (cluster_id, severity, calculated_at DESC);

CREATE INDEX IF NOT EXISTS idx_exhaustion_expires
    ON recommendations.resource_exhaustion (expires_at);

-- audit.recommendation_events
CREATE INDEX IF NOT EXISTS idx_audit_events_cluster_at
    ON audit.recommendation_events (cluster_id, emitted_at DESC);

CREATE INDEX IF NOT EXISTS idx_audit_events_source
    ON audit.recommendation_events (source_table, source_id);

CREATE INDEX IF NOT EXISTS idx_audit_events_severity
    ON audit.recommendation_events (cluster_id, severity, emitted_at DESC);

-- audit.action_log
CREATE INDEX IF NOT EXISTS idx_action_log_event
    ON audit.action_log (recommendation_event_id);

CREATE INDEX IF NOT EXISTS idx_action_log_cluster_at
    ON audit.action_log (cluster_id, action_at DESC);
