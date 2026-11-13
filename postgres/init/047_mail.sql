-- Mail (plans/PLAN-roles.md, "Accounts", E2): one SMTP relay for the whole installation.
--
-- Invitations, password resets and address changes are sent through it. Without it, or when it fails,
-- whoever sends an invitation gets the link to pass on themselves. The password is Fernet-encrypted with
-- HCM_SECRET_KEY, like the cluster passwords.

CREATE TABLE IF NOT EXISTS config.mail_settings (
    id            SMALLINT    PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    host          TEXT,
    port          INTEGER     NOT NULL DEFAULT 587 CHECK (port BETWEEN 1 AND 65535),
    security      TEXT        NOT NULL DEFAULT 'starttls' CHECK (security IN ('starttls', 'tls', 'none')),
    username      TEXT,
    password_enc  TEXT,
    sender        TEXT,                 -- the From address
    sender_name   TEXT,                 -- the From name (default: HyperCore Manager)
    public_url    TEXT,                 -- the address put in links (https://hcm.example.com)
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by    TEXT
);
