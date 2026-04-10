-- =============================================================================
-- 008_audit_event_types.sql
-- Extend the recommendation_events.event_type check constraint to include
-- Phase 2 event types: 'cluster_health' and 'resize_recommendation'.
--
-- PostgreSQL does not support ALTER CONSTRAINT on a CHECK, so we drop and
-- recreate it.  The operation is safe on existing rows because all existing
-- rows already satisfy the wider constraint.
--
-- Idempotent: safe to run against a database that already has the wider
-- constraint (the DROP will fail gracefully if the old name no longer exists).
-- =============================================================================

ALTER TABLE audit.recommendation_events
    DROP CONSTRAINT IF EXISTS recommendation_events_event_type_check;

ALTER TABLE audit.recommendation_events
    ADD CONSTRAINT recommendation_events_event_type_check
        CHECK (event_type IN (
            'forecast_snapshot',
            'right_sizing',
            'resource_exhaustion',
            'cluster_health',
            'resize_recommendation'
        ));
