-- =============================================================================
-- 035_backup_run_timing.sql
-- Where a run's time went, so backup durations can be learned: started_at -> finished_at also
-- holds the pre-flight wait and the wait for a free proxy. work_started_at = the cluster was clear
-- and the run began; lock_wait_seconds = waiting for the proxy; copy_seconds = the proxy's copy
-- (exports: the HyperCore export task); overhead_seconds = the rest of the active time (snapshot,
-- clones, maps, cleanup, retention). NULL on runs from before this migration. Idempotent.
-- =============================================================================
ALTER TABLE backup.runs ADD COLUMN IF NOT EXISTS work_started_at   TIMESTAMPTZ;
ALTER TABLE backup.runs ADD COLUMN IF NOT EXISTS lock_wait_seconds REAL;
ALTER TABLE backup.runs ADD COLUMN IF NOT EXISTS copy_seconds      REAL;
ALTER TABLE backup.runs ADD COLUMN IF NOT EXISTS overhead_seconds  REAL;
