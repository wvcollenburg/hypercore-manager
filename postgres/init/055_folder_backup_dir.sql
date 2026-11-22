-- =============================================================================
-- 055_folder_backup_dir.sql
-- The directory a folder's backups and exports live in on its target, stored once on the folder.
--
-- Until 1.8.0 the backup service derived it from the folder NAME every time (backup/jobs.py
-- tenant_slug). Folder names are unique per parent only since 044, so two customers with a folder
-- "Production" on one target shared <target>/Production/ (import browse, import and export retention
-- saw each other's exports), and renaming a folder moved where its exports were looked for.
--
-- Every folder that exists now keeps exactly the directory it used so far (the old slug: the name with
-- every run of characters other than A-Z a-z 0-9 . _ - turned into one underscore, underscores trimmed
-- at both ends, 'vm' when nothing is left), so no existing data moves. A folder created after this
-- migration has NULL here; the backup service gives it <slug>-<first 8 chars of folder_id> on first use.
-- A rename never changes the column. Two existing folders that already shared a directory keep
-- sharing it: data already on the target cannot be split automatically.
-- Idempotent: the UPDATE only fills rows that have no directory yet.
-- =============================================================================
ALTER TABLE config.folders ADD COLUMN IF NOT EXISTS backup_dir TEXT;

UPDATE config.folders
   SET backup_dir = COALESCE(NULLIF(btrim(regexp_replace(name,
                       '[^ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-]+', '_', 'g'), '_'), ''), 'vm')
 WHERE backup_dir IS NULL;
