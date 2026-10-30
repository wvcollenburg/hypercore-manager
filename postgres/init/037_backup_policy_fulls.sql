-- =============================================================================
-- 037_backup_policy_fulls.sql
-- Full backups on a rule inside a backup schedule: full_rrule picks the days whose occurrence
-- takes a full (NULL = every full_every_n runs, as before), full_kind says how: 'real' reads the
-- VM again, 'synthetic' runs the normal incremental and then builds a new chain's base from it on
-- the target (points.synthetic). A synthetic full that fails its chain verification sets
-- plans.force_full so the next run reads the VM in full. runs.synth_seconds is the time the
-- synthetic full took, kept apart from the copy and the overhead. Idempotent.
-- =============================================================================
ALTER TABLE backup.policies ADD COLUMN IF NOT EXISTS full_rrule TEXT;
ALTER TABLE backup.policies ADD COLUMN IF NOT EXISTS full_kind  TEXT NOT NULL DEFAULT 'real';
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_backup_policies_full_kind') THEN
        ALTER TABLE backup.policies ADD CONSTRAINT ck_backup_policies_full_kind CHECK (full_kind IN ('real', 'synthetic'));
    END IF;
END $$;
ALTER TABLE backup.plans  ADD COLUMN IF NOT EXISTS force_full    BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE backup.points ADD COLUMN IF NOT EXISTS synthetic     BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE backup.runs   ADD COLUMN IF NOT EXISTS synth_seconds REAL;
