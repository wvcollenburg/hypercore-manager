-- Accounts are email addresses (plans/PLAN-roles.md, "Accounts", E1).
--
-- The login name (config.users.username) is the person's email address, lowercased; only the main admin
-- (SC_DASHBOARD_USER in .env) keeps a plain username. The account belongs to the person, not to a tenant:
-- tenants hold that person's access (assignments, group memberships), so home_node_id goes.
--
-- Accounts from before this change without an address are disabled until a top-level admin enters one
-- (awaiting_email). That is done at dashboard start (access_db.require_email_addresses), because only the
-- running dashboard knows which account is the main admin.

ALTER TABLE config.users ADD COLUMN IF NOT EXISTS awaiting_email    BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE config.users ADD COLUMN IF NOT EXISTS email_verified_at TIMESTAMPTZ;
ALTER TABLE config.users DROP COLUMN IF EXISTS home_node_id;

-- one account per address, whatever its capitals (skipped, with a notice, when old names collide that way)
DO $$
BEGIN
    IF EXISTS (SELECT lower(username) FROM config.users GROUP BY lower(username) HAVING COUNT(*) > 1) THEN
        RAISE NOTICE 'config.users: usernames that differ only in capitals exist; ux_users_login not created';
    ELSE
        CREATE UNIQUE INDEX IF NOT EXISTS ux_users_login ON config.users (lower(username));
    END IF;
END $$;
