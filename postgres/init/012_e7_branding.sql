-- =============================================================================
-- 012_e7_branding.sql
-- E7: Report branding and customization.
--
-- Stores per-cluster branding configuration for PDF reports:
--   company_name    — displayed on the cover page and footer
--   accent_color    — hex colour for headings (default: SC//Foresight blue)
--   logo_b64        — base64-encoded logo image (PNG or JPG)
--   logo_mime_type  — MIME type of the logo ('image/png' or 'image/jpeg')
--
-- One row per cluster; UPSERT on save.
-- Idempotent: safe to run against an existing database.
-- =============================================================================

CREATE TABLE IF NOT EXISTS config.branding (
    cluster_id      UUID        PRIMARY KEY
                                    REFERENCES config.clusters (cluster_id),
    company_name    TEXT,
    accent_color    TEXT        NOT NULL DEFAULT '#3b82f6',
    logo_b64        TEXT,
    logo_mime_type  TEXT        NOT NULL DEFAULT 'image/png',
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
