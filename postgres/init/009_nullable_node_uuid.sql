-- =============================================================================
-- 009_nullable_node_uuid.sql
-- Allow node_uuid to be NULL in right_sizing and resize_recommendations.
--
-- VMs that are powered off or mid-migration have no current node assignment.
-- The collector returns an empty string for node_uuid in those cases; Sentinel
-- converts it to NULL before writing.  The NOT NULL constraint must be relaxed
-- to accept these rows.
--
-- forecast_snapshots.node_uuid is already nullable (network dimension rows
-- are cluster-level, not per-node).
--
-- Idempotent: DROP CONSTRAINT IF EXISTS / SET DATA TYPE are safe to re-run.
-- =============================================================================

ALTER TABLE recommendations.right_sizing
    ALTER COLUMN node_uuid DROP NOT NULL;

ALTER TABLE recommendations.resize_recommendations
    ALTER COLUMN node_uuid DROP NOT NULL;
