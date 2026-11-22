-- =============================================================================
-- 057_login_limits.sql
-- Sign-in attempts are limited (1.8.1). The dashboard runs four gunicorn workers and its session is a
-- cookie the client holds, so the counts live here:
--   config.login_failures: one row per failed password or two-factor code; `login` is what was typed
--     (lowercased), so an unknown address is limited like a known one; rows older than a day are removed
--     by the dashboard as it writes.
--   config.users.otp_last_counter: the time step of the last accepted TOTP code; a code is accepted once.
-- Idempotent.
-- =============================================================================
CREATE TABLE IF NOT EXISTS config.login_failures (
    id      BIGSERIAL   PRIMARY KEY,
    at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    login   TEXT        NOT NULL,
    client  TEXT        NOT NULL DEFAULT '',
    step    TEXT        NOT NULL CHECK (step IN ('password', 'otp'))
);
CREATE INDEX IF NOT EXISTS ix_login_failures_login  ON config.login_failures (login, at);
CREATE INDEX IF NOT EXISTS ix_login_failures_client ON config.login_failures (client, at);
CREATE INDEX IF NOT EXISTS ix_login_failures_at     ON config.login_failures (at);

ALTER TABLE config.users ADD COLUMN IF NOT EXISTS otp_last_counter BIGINT;
