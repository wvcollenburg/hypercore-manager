-- =============================================================================
-- 010_forecast_entity_label.sql
-- Add entity_label to forecast_snapshots for human-readable display.
--
-- Stores the most meaningful identifier for the forecasted entity:
--   CPU / RAM / Storage : node peer_id  (e.g. "node-1")
--   Storage latency     : VSD name      (e.g. "my-vsd")
--   Network             : VM name       (e.g. "DC02")
--
-- Idempotent: ADD COLUMN IF NOT EXISTS is safe to re-run.
-- =============================================================================

ALTER TABLE recommendations.forecast_snapshots
    ADD COLUMN IF NOT EXISTS entity_label TEXT;
