-- =============================================================================
-- 032_backup_point_verify.sql
-- Outcome of the last checksum verification of a restore point: NULL when the last check passed
-- (or none ran yet), otherwise the files that did not match their manifest. Set by the weekly
-- verify, the manual Verify button and the integrity check a restore runs first. Idempotent.
-- =============================================================================
ALTER TABLE backup.points ADD COLUMN IF NOT EXISTS verify_error TEXT;
