-- Dashboard users
-- This migration is also applied at runtime by user_db.ensure_table(), so existing
-- deployments pick it up without a full postgres container rebuild.

CREATE TABLE IF NOT EXISTS config.users (
    user_id       UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    username      TEXT        NOT NULL UNIQUE,
    password_hash TEXT        NOT NULL,
    role          TEXT        NOT NULL DEFAULT 'user' CHECK (role IN ('admin', 'user')),
    otp_secret    TEXT,
    otp_enabled   BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
