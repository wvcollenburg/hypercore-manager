-- =============================================================================
-- 045_guest_console.sql
-- One-time console access for an outside engineer (plans/PLAN-roles.md, R4).
--
-- A grant gives one guest the console of one VM within a time window, and nothing else. The guest
-- gets a link (a long random token) and, through another channel, a 6-digit code; both are needed.
-- Only hashes are stored. Five wrong codes lock the grant. A grant can start as a request (someone
-- with guest_console.request asks, someone with guest_console.grant approves).
-- console.sessions learns two more sources: 'guest' (the outsider's session) and 'watch' (a granter
-- watching it read-only). Idempotent.
-- =============================================================================
CREATE TABLE IF NOT EXISTS console.guest_grants (
    id                  BIGSERIAL   PRIMARY KEY,
    cluster_id          UUID        NOT NULL REFERENCES config.clusters (cluster_id) ON DELETE CASCADE,
    vm_uuid             TEXT        NOT NULL,
    vm_name             TEXT,
    label               TEXT        NOT NULL DEFAULT 'Console',      -- the only thing the guest sees besides the console
    guest_name          TEXT        NOT NULL,
    guest_company       TEXT,
    reason              TEXT,
    valid_from          TIMESTAMPTZ NOT NULL,
    valid_until         TIMESTAMPTZ NOT NULL,
    max_minutes         INTEGER     NOT NULL DEFAULT 120 CHECK (max_minutes BETWEEN 5 AND 720),
    max_sessions        INTEGER     NOT NULL DEFAULT 1 CHECK (max_sessions BETWEEN 1 AND 20),
    sessions_used       INTEGER     NOT NULL DEFAULT 0,
    allow_open_session  BOOLEAN     NOT NULL DEFAULT FALSE,           -- connect even onto a logged-in console
    token_hash          TEXT        UNIQUE,                           -- sha256 of the link token (set when granted)
    code_hash           TEXT,                                         -- hash of the 6-digit code
    guest_nonce_hash    TEXT,                                         -- the one browser that redeemed it
    failed_codes        INTEGER     NOT NULL DEFAULT 0,
    status              TEXT        NOT NULL DEFAULT 'requested'
                                    CHECK (status IN ('requested', 'active', 'locked', 'revoked', 'rejected')),
    requested_by        TEXT,
    granted_by          TEXT,
    revoked_by          TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    granted_at          TIMESTAMPTZ,
    revoked_at          TIMESTAMPTZ,
    last_redeemed_at    TIMESTAMPTZ,
    last_redeemed_ip    TEXT,
    CHECK (valid_until > valid_from)
);
CREATE INDEX IF NOT EXISTS ix_guest_grants_cluster ON console.guest_grants (cluster_id, vm_uuid);
CREATE INDEX IF NOT EXISTS ix_guest_grants_status ON console.guest_grants (status, valid_until);

ALTER TABLE console.sessions ADD COLUMN IF NOT EXISTS guest_grant_id BIGINT;
ALTER TABLE console.sessions DROP CONSTRAINT IF EXISTS sessions_source_check;
ALTER TABLE console.sessions ADD CONSTRAINT sessions_source_check CHECK (source IN ('user', 'system', 'guest', 'watch'));
