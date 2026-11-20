-- Self-service (plans/PLAN-roles.md, "Accounts", E4): forgot password and changing one's own address.
--
-- config.account_tokens: one-time links mailed to a person. 'reset' (to the account's address, 60 minutes)
-- sets a new password; 'email' (to the NEW address, 60 minutes) confirms an address change. Only the token's
-- SHA-256 is stored.
--
-- config.user_aliases: the addresses an account used before. Log rows keep the address used at the time;
-- the aliases let "who may be named to whom" still recognise them as that person.

CREATE TABLE IF NOT EXISTS config.account_tokens (
    token_hash  TEXT        PRIMARY KEY,
    kind        TEXT        NOT NULL CHECK (kind IN ('reset', 'email')),
    user_id     UUID        NOT NULL REFERENCES config.users (user_id) ON DELETE CASCADE,
    new_email   TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at  TIMESTAMPTZ NOT NULL,
    used_at     TIMESTAMPTZ,
    CHECK ((kind = 'email') = (new_email IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS ix_account_tokens_user ON config.account_tokens (user_id, kind, created_at DESC);

CREATE TABLE IF NOT EXISTS config.user_aliases (
    user_id     UUID        NOT NULL REFERENCES config.users (user_id) ON DELETE CASCADE,
    address     TEXT        NOT NULL,
    until       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, address)
);
