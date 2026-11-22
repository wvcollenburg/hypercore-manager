-- =============================================================================
-- 058_session_generation.sql
-- Sessions can be ended (1.8.1). A dashboard session is a signed cookie; it carries the generation of its
-- account at sign-in, and every request compares it with config.users.session_gen. A password change or
-- reset, an administrator setting the password, two-factor switched off or reset, and an address change
-- raise the generation, which ends every other session of that account at its next request.
-- Idempotent.
-- =============================================================================
ALTER TABLE config.users ADD COLUMN IF NOT EXISTS session_gen INTEGER NOT NULL DEFAULT 0;
