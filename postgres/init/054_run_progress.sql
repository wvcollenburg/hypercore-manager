-- =============================================================================
-- 054_run_progress.sql
-- Structured progress for running work, so the dashboard can draw a bar instead of only a text line
-- (William 2026-11-21: "like balenaEtcher, green while copying, purple while verifying").
--   runs:       {phase: copy | check | checksum, done, total (MiB), device, devices}
--   restores:   {phase: verify | prepare | import | write | attach | finish, done, total (MiB)}
--   test_runs:  phase (queued -> restore -> boot -> network -> cleanup) and progress {started, timeout_s}
-- Idempotent.
-- =============================================================================
ALTER TABLE backup.runs      ADD COLUMN IF NOT EXISTS progress JSONB;
ALTER TABLE backup.restores  ADD COLUMN IF NOT EXISTS progress JSONB;
ALTER TABLE backup.test_runs ADD COLUMN IF NOT EXISTS phase    TEXT;
ALTER TABLE backup.test_runs ADD COLUMN IF NOT EXISTS progress JSONB;
