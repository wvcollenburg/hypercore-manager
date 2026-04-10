-- =============================================================================
-- 006_retention.sql
-- Retention cleanup function for the 12-month rolling window on
-- recommendations.* tables.
--
-- Audit tables are kept indefinitely — this function never touches them.
--
-- Usage: called by the Sentinel process on a scheduled basis (e.g. nightly).
-- Returns one summary row per table showing how many rows were deleted.
-- =============================================================================

CREATE OR REPLACE FUNCTION public.cleanup_expired_recommendations()
RETURNS TABLE (
    table_name  TEXT,
    rows_deleted BIGINT
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_deleted BIGINT;
    v_cutoff  TIMESTAMPTZ := NOW();
BEGIN
    -- forecast_snapshots ---------------------------------------------------
    DELETE FROM recommendations.forecast_snapshots
    WHERE expires_at < v_cutoff;
    GET DIAGNOSTICS v_deleted = ROW_COUNT;
    RETURN QUERY SELECT 'recommendations.forecast_snapshots'::TEXT, v_deleted;

    -- right_sizing ---------------------------------------------------------
    DELETE FROM recommendations.right_sizing
    WHERE expires_at < v_cutoff;
    GET DIAGNOSTICS v_deleted = ROW_COUNT;
    RETURN QUERY SELECT 'recommendations.right_sizing'::TEXT, v_deleted;

    -- resource_exhaustion --------------------------------------------------
    DELETE FROM recommendations.resource_exhaustion
    WHERE expires_at < v_cutoff;
    GET DIAGNOSTICS v_deleted = ROW_COUNT;
    RETURN QUERY SELECT 'recommendations.resource_exhaustion'::TEXT, v_deleted;
END;
$$;

COMMENT ON FUNCTION public.cleanup_expired_recommendations() IS
    'Deletes rows from recommendations.* tables where expires_at < NOW(). '
    'Audit tables are intentionally excluded. Call nightly from Sentinel.';
