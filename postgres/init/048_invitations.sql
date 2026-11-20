-- Invitations (plans/PLAN-roles.md, "Accounts", E3).
--
-- An administrator of a layer (or above) invites an email address onto that layer: with a role on a scope
-- (like an assignment) or into a group. The link in the mail is the proof that the person controls the
-- address: a new address sets its password through it, an existing account accepts after logging in.
-- An 'activate' invitation gives an account from before E1 (no address yet) its address and password.
--
-- Valid 48 hours, single use, only the SHA-256 of the token is stored. Whoever sends an invitation never
-- learns whether the address already had an account.

CREATE TABLE IF NOT EXISTS config.invitations (
    invitation_id   UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    kind            TEXT        NOT NULL DEFAULT 'access' CHECK (kind IN ('access', 'activate')),
    email           TEXT        NOT NULL,                 -- lowercased
    display_name    TEXT,                                 -- suggested by the inviter, the person may change it
    user_id         UUID        REFERENCES config.users (user_id) ON DELETE CASCADE,      -- 'activate' only
    role_id         UUID        REFERENCES config.roles (role_id) ON DELETE CASCADE,
    group_id        UUID        REFERENCES config.groups (group_id) ON DELETE CASCADE,
    scope_kind      TEXT        CHECK (scope_kind IN ('root', 'node', 'cluster', 'tag', 'vm')),
    node_id         UUID        REFERENCES config.folders (folder_id) ON DELETE CASCADE,
    cluster_id      UUID        REFERENCES config.clusters (cluster_id) ON DELETE CASCADE,
    tag             TEXT,
    vm_uuid         TEXT,
    vm_name         TEXT,
    lang            TEXT,
    token_hash      TEXT        NOT NULL UNIQUE,
    state           TEXT        NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'accepted', 'withdrawn')),
    invited_by      TEXT,
    invited_by_id   UUID        REFERENCES config.users (user_id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ NOT NULL,
    accepted_at     TIMESTAMPTZ,
    accepted_user   UUID        REFERENCES config.users (user_id) ON DELETE SET NULL,
    CHECK (
        (kind = 'activate' AND user_id IS NOT NULL AND role_id IS NULL AND group_id IS NULL) OR
        (kind = 'access' AND (role_id IS NULL) <> (group_id IS NULL)
                         AND (role_id IS NULL OR scope_kind IS NOT NULL))
    )
);
CREATE INDEX IF NOT EXISTS ix_invitations_email ON config.invitations (email);
CREATE INDEX IF NOT EXISTS ix_invitations_state ON config.invitations (state, expires_at);
