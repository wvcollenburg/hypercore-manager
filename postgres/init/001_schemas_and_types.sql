-- =============================================================================
-- 001_schemas_and_types.sql
-- Schemas and shared enum types for SC//Foresight (Sentinel).
-- Idempotent: safe to run against an existing database.
-- =============================================================================

-- Schemas ─────────────────────────────────────────────────────────────────────

CREATE SCHEMA IF NOT EXISTS config;
CREATE SCHEMA IF NOT EXISTS recommendations;
CREATE SCHEMA IF NOT EXISTS audit;

-- Shared enum types ───────────────────────────────────────────────────────────
-- CREATE TYPE does not support IF NOT EXISTS; use DO blocks instead.

DO $$ BEGIN
    CREATE TYPE public.severity_level AS ENUM (
        'INFO',
        'WARNING',
        'CRITICAL',
        'EMERGENCY'
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- Five independent resource dimensions tracked across the cluster.
-- RAM is allocation-only (HyperCore never overcommits RAM).
-- Network at node level is a VM rollup (no native node endpoint in HyperCore API).
DO $$ BEGIN
    CREATE TYPE public.resource_dimension AS ENUM (
        'cpu',
        'ram',
        'storage_capacity',
        'storage_latency',
        'network'
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
