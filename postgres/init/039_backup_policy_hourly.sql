-- =============================================================================
-- 039_backup_policy_hourly.sql
-- Hourly backup schedules and "run now". An occurrence was the local day its window opened; an
-- hourly schedule has many a day, so an occurrence is now the moment its window opened
-- (TIMESTAMPTZ). Existing rows are converted: the day at the schedule's window_open, in the
-- schedule's time zone. policies.manual_at is set by "run now" and cleared once every member ran
-- in that manual occurrence. Idempotent: the conversion only runs while the columns are DATE.
-- =============================================================================
ALTER TABLE backup.policies ADD COLUMN IF NOT EXISTS manual_at TIMESTAMPTZ;

DO $$
BEGIN
    IF (SELECT data_type FROM information_schema.columns
        WHERE table_schema = 'backup' AND table_name = 'runs' AND column_name = 'occurrence') = 'date' THEN
        ALTER TABLE backup.runs
            ALTER COLUMN occurrence TYPE TIMESTAMPTZ USING occurrence::timestamp AT TIME ZONE 'UTC';
        UPDATE backup.runs r
           SET occurrence = (((r.occurrence AT TIME ZONE 'UTC')::date + p.window_open::time) AT TIME ZONE p.timezone)
          FROM backup.policies p
         WHERE p.id = r.policy_id AND r.occurrence IS NOT NULL;
    END IF;
    IF (SELECT data_type FROM information_schema.columns
        WHERE table_schema = 'backup' AND table_name = 'policy_nights' AND column_name = 'occurrence') = 'date' THEN
        ALTER TABLE backup.policy_nights
            ALTER COLUMN occurrence TYPE TIMESTAMPTZ USING occurrence::timestamp AT TIME ZONE 'UTC';
        UPDATE backup.policy_nights n
           SET occurrence = (((n.occurrence AT TIME ZONE 'UTC')::date + p.window_open::time) AT TIME ZONE p.timezone)
          FROM backup.policies p
         WHERE p.id = n.policy_id;
    END IF;
END $$;
