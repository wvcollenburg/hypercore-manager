-- =============================================================================
-- 031_backup_point_quiesced.sql
-- Whether the anchor snapshot behind a restore point was quiesced by the guest (TRUE), taken
-- crash-consistent (FALSE) or unknown (NULL, points made before this column). Idempotent.
-- =============================================================================
ALTER TABLE backup.points ADD COLUMN IF NOT EXISTS quiesced BOOLEAN;
