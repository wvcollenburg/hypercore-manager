-- =============================================================================
-- 044_rbac.sql
-- Roles and tenants (plans/PLAN-roles.md).
--
-- config.folders becomes the tenant tree: MSP > customer > folder (a cluster still hangs under a
-- folder). Existing folders move under one default MSP and one default customer, which the UI hides
-- while they are the only ones (a single-company install looks as before).
--
-- A role is a named set of permissions (the built-in ones carry no stored set: their permissions
-- live in code, dashboard/rbac.py). A group is a set of users. An assignment gives a user or a
-- group a role on a scope: the whole install (root), a tree node, a cluster, a VM tag on a cluster,
-- or one VM. config.access_audit records every change to any of it.
--
-- The upgrade: every administrator becomes Admin on the whole install; every other account is
-- disabled and marked awaiting_access until an administrator assigns its access (the upgrade
-- modal). That step runs once (config.rbac_state), so re-running this file changes nothing.
-- Idempotent.
-- =============================================================================

-- ── the tree ─────────────────────────────────────────────────────────────────
ALTER TABLE config.folders ADD COLUMN IF NOT EXISTS parent_id UUID REFERENCES config.folders (folder_id) ON DELETE RESTRICT;
ALTER TABLE config.folders ADD COLUMN IF NOT EXISTS level TEXT NOT NULL DEFAULT 'folder';
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'folders_level_check') THEN
        ALTER TABLE config.folders ADD CONSTRAINT folders_level_check CHECK (level IN ('msp', 'customer', 'folder'));
    END IF;
END $$;
-- names are unique per parent, not install-wide (two MSPs will both have "Production")
DROP INDEX IF EXISTS config.ux_folders_name;
CREATE UNIQUE INDEX IF NOT EXISTS ux_folders_parent_name
    ON config.folders (COALESCE(parent_id, '00000000-0000-0000-0000-000000000000'::uuid), lower(name));
CREATE INDEX IF NOT EXISTS ix_folders_parent ON config.folders (parent_id);

DO $$
DECLARE
    msp  UUID;
    cust UUID;
BEGIN
    SELECT folder_id INTO msp FROM config.folders WHERE level = 'msp' ORDER BY created_at LIMIT 1;
    IF msp IS NULL THEN
        INSERT INTO config.folders (name, level) VALUES ('Default', 'msp') RETURNING folder_id INTO msp;
    END IF;
    SELECT folder_id INTO cust FROM config.folders WHERE level = 'customer' ORDER BY created_at LIMIT 1;
    IF cust IS NULL THEN
        INSERT INTO config.folders (name, level, parent_id) VALUES ('Default', 'customer', msp) RETURNING folder_id INTO cust;
    END IF;
    UPDATE config.folders SET parent_id = cust WHERE level = 'folder' AND parent_id IS NULL;
END $$;

-- ── roles ────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS config.roles (
    role_id      UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    name         TEXT        NOT NULL,
    description  TEXT,
    node_id      UUID        REFERENCES config.folders (folder_id) ON DELETE CASCADE,   -- NULL: defined at root
    builtin_key  TEXT        UNIQUE,                                                    -- set for the read-only templates
    permissions  TEXT[]      NOT NULL DEFAULT '{}',                                     -- custom roles only
    copied_from  TEXT,
    created_by   TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_roles_node_name
    ON config.roles (COALESCE(node_id, '00000000-0000-0000-0000-000000000000'::uuid), lower(name));

INSERT INTO config.roles (name, builtin_key, description) VALUES
    ('Admin',          'admin',          'Everything at its scope: VMs, backups, settings, users and roles below it.'),
    ('Engineer',       'engineer',       'Full VM control (console, power, edit, snapshots, clone, create, delete); sees backups.'),
    ('App Manager',    'app_manager',    'Sees VMs and their details and opens their console; changes nothing.'),
    ('Backup Manager', 'backup_manager', 'Every backup action: schedules, runs, file browse and download, file and full restores, verification.'),
    ('Viewer',         'viewer',         'Sees clusters, VMs and backups; changes nothing.')
ON CONFLICT (builtin_key) DO NOTHING;

-- ── groups ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS config.groups (
    group_id     UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    name         TEXT        NOT NULL,
    description  TEXT,
    node_id      UUID        REFERENCES config.folders (folder_id) ON DELETE CASCADE,   -- NULL: defined at root
    created_by   TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_groups_node_name
    ON config.groups (COALESCE(node_id, '00000000-0000-0000-0000-000000000000'::uuid), lower(name));

CREATE TABLE IF NOT EXISTS config.group_members (
    group_id   UUID        NOT NULL REFERENCES config.groups (group_id) ON DELETE CASCADE,
    user_id    UUID        NOT NULL REFERENCES config.users (user_id) ON DELETE CASCADE,
    added_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (group_id, user_id)
);

-- ── assignments ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS config.assignments (
    assignment_id UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       UUID        REFERENCES config.users (user_id) ON DELETE CASCADE,
    group_id      UUID        REFERENCES config.groups (group_id) ON DELETE CASCADE,
    role_id       UUID        NOT NULL REFERENCES config.roles (role_id) ON DELETE RESTRICT,
    scope_kind    TEXT        NOT NULL CHECK (scope_kind IN ('root', 'node', 'cluster', 'tag', 'vm')),
    node_id       UUID        REFERENCES config.folders (folder_id) ON DELETE CASCADE,
    cluster_id    UUID        REFERENCES config.clusters (cluster_id) ON DELETE CASCADE,
    tag           TEXT,
    vm_uuid       TEXT,
    vm_name       TEXT,                                    -- for display; the VM may be renamed
    created_by    TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK ((user_id IS NULL) <> (group_id IS NULL)),
    CHECK (
        (scope_kind = 'root'    AND node_id IS NULL AND cluster_id IS NULL) OR
        (scope_kind = 'node'    AND node_id IS NOT NULL AND cluster_id IS NULL) OR
        (scope_kind = 'cluster' AND cluster_id IS NOT NULL AND tag IS NULL AND vm_uuid IS NULL) OR
        (scope_kind = 'tag'     AND cluster_id IS NOT NULL AND tag IS NOT NULL) OR
        (scope_kind = 'vm'      AND cluster_id IS NOT NULL AND vm_uuid IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS ix_assignments_user ON config.assignments (user_id);
CREATE INDEX IF NOT EXISTS ix_assignments_group ON config.assignments (group_id);
CREATE INDEX IF NOT EXISTS ix_assignments_cluster ON config.assignments (cluster_id);

-- ── users ────────────────────────────────────────────────────────────────────
ALTER TABLE config.users ADD COLUMN IF NOT EXISTS enabled         BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE config.users ADD COLUMN IF NOT EXISTS awaiting_access BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE config.users ADD COLUMN IF NOT EXISTS home_node_id    UUID REFERENCES config.folders (folder_id) ON DELETE SET NULL;
ALTER TABLE config.users ADD COLUMN IF NOT EXISTS display_name    TEXT;

-- ── permissions seen by this installation (new ones in a later version raise a notice) ─────────
CREATE TABLE IF NOT EXISTS config.permissions_known (
    permission    TEXT        PRIMARY KEY,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    acknowledged  BOOLEAN     NOT NULL DEFAULT FALSE
);

-- ── audit of every access change ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS config.access_audit (
    id         BIGSERIAL   PRIMARY KEY,
    at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    actor      TEXT,
    action     TEXT        NOT NULL,                     -- e.g. user.create, assignment.add, role.update
    target     TEXT,                                     -- what it was done to, human-readable
    node_id    UUID,                                     -- where in the tree (for the per-scope audit view)
    detail     JSONB
);
CREATE INDEX IF NOT EXISTS ix_access_audit_at ON config.access_audit (at DESC);
CREATE INDEX IF NOT EXISTS ix_access_audit_node ON config.access_audit (node_id, at DESC);

-- ── the one-time upgrade ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS config.rbac_state (
    key    TEXT PRIMARY KEY,
    value  TEXT,
    at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DO $$
DECLARE
    admin_role UUID;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM config.rbac_state WHERE key = 'upgraded') THEN
        SELECT role_id INTO admin_role FROM config.roles WHERE builtin_key = 'admin';
        INSERT INTO config.assignments (user_id, role_id, scope_kind, created_by)
        SELECT u.user_id, admin_role, 'root', 'upgrade'
          FROM config.users u
         WHERE u.role = 'admin'
           AND NOT EXISTS (SELECT 1 FROM config.assignments a WHERE a.user_id = u.user_id AND a.scope_kind = 'root');
        UPDATE config.users SET enabled = FALSE, awaiting_access = TRUE WHERE role <> 'admin';
        INSERT INTO config.access_audit (actor, action, target, detail)
        SELECT 'upgrade', 'user.awaiting_access', username, jsonb_build_object('reason', 'roles introduced')
          FROM config.users WHERE role <> 'admin';
        INSERT INTO config.rbac_state (key, value) VALUES ('upgraded', 'roles introduced');
    END IF;
END $$;
