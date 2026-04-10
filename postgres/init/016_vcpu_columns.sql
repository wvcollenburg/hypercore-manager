-- =============================================================================
-- 016_vcpu_columns.sql — Add current_vcpu / recommended_vcpu to right-sizing tables
--
-- The sentinel pipeline now reads num_vcpu from InfluxDB (vm_inventory
-- measurement) and propagates it through analysis so the viewer can display
-- actual vCPU counts alongside CPU-% signals.
--
-- right_sizing.current_vcpu         — vCPU count at time of analysis
-- resize_recommendations.current_vcpu    — same, carried through to advice
-- resize_recommendations.recommended_vcpu — computed target vCPU count
-- =============================================================================

ALTER TABLE recommendations.right_sizing
    ADD COLUMN IF NOT EXISTS current_vcpu INTEGER;

ALTER TABLE recommendations.resize_recommendations
    ADD COLUMN IF NOT EXISTS current_vcpu     INTEGER,
    ADD COLUMN IF NOT EXISTS recommended_vcpu INTEGER;
