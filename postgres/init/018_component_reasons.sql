-- =============================================================================
-- 018_component_reasons.sql — Per-dimension reason for the component score
--
-- The Overview dashboard's component breakdown shows a 0–100 score per
-- dimension, but the *why* of a low score (HA-headroom vs forecast severity)
-- was opaque.  This column carries a small structured record per dimension
-- explaining what the score is based on, e.g.:
--
--   { "ram": {"kind": "ha", "headroom_pct": -8.0, "broken": true},
--     "cpu": {"kind": "forecast", "severity": "INFO"} }
--
-- The dashboard maps the kind/values to a translated explanation under each
-- dimension's bar.  Existing reads degrade gracefully — older rows have an
-- empty default and the UI just hides the explanation line.
-- =============================================================================

ALTER TABLE recommendations.cluster_health_scores
    ADD COLUMN IF NOT EXISTS component_reasons JSONB NOT NULL DEFAULT '{}';
