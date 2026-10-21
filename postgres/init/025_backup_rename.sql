-- =============================================================================
-- 025_backup_rename.sql
-- The Exports module becomes Backup: schema exports -> backup, shares -> targets,
-- schedules -> plans. Idempotent: every step checks what exists first.
-- =============================================================================

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.schemata WHERE schema_name = 'exports')
       AND NOT EXISTS (SELECT 1 FROM information_schema.schemata WHERE schema_name = 'backup') THEN
        ALTER SCHEMA exports RENAME TO backup;
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema = 'backup' AND table_name = 'shares')
       AND NOT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema = 'backup' AND table_name = 'targets') THEN
        ALTER TABLE backup.shares RENAME TO targets;
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema = 'backup' AND table_name = 'schedules')
       AND NOT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema = 'backup' AND table_name = 'plans') THEN
        ALTER TABLE backup.schedules RENAME TO plans;
    END IF;
END $$;
