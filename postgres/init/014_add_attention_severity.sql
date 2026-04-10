-- =============================================================================
-- 014_add_attention_severity.sql
-- Adds 'ATTENTION' to the public.severity_level enum.
-- ATTENTION sits between WARNING and INFO — exhaustion is real but not imminent
-- (181–365 days away).
-- Idempotent: safe to run against an existing database.
-- =============================================================================

DO $$
BEGIN
    ALTER TYPE public.severity_level ADD VALUE IF NOT EXISTS 'ATTENTION' AFTER 'WARNING';
EXCEPTION WHEN others THEN NULL;
END $$;
