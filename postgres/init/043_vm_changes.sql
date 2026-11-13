-- =============================================================================
-- 043_vm_changes.sql
-- VM edits and power actions from HCM. A change set holds every change to one VM and runs at one
-- moment: now, or at run_at (a maintenance window). HCM keeps the changes until then; nothing is
-- written to HyperCore before the set runs, so a set can be cancelled up to its start.
--
-- ops       the changes, as validated by the backup service (backup/vmedit.py), in apply order
-- cycle     the set needs the VM off (an OS shutdown, the changes, a start): every change except
--           the few HyperCore applies to a running VM (disk expansion, new disks, ISOs, metadata)
-- force_off a guest that has not shut down after shutdown_timeout_min is powered off; without it
--           the set fails and the VM keeps running unchanged
-- result    one line per step (shutdown, each change, start) with its outcome
-- A 'create' set makes a new VM; its vm_uuid is empty until HyperCore has created it.
-- Idempotent.
-- =============================================================================
CREATE TABLE IF NOT EXISTS backup.vm_changes (
    id                   BIGSERIAL   PRIMARY KEY,
    cluster_id           TEXT        NOT NULL,
    vm_uuid              TEXT        NOT NULL,
    vm_name              TEXT,
    kind                 TEXT        NOT NULL DEFAULT 'edit' CHECK (kind IN ('edit', 'power', 'delete', 'create', 'snapshot', 'clone')),
    ops                  JSONB       NOT NULL DEFAULT '[]'::jsonb,
    summary              TEXT,
    cycle                BOOLEAN     NOT NULL DEFAULT false,
    run_at               TIMESTAMPTZ,
    force_off            BOOLEAN     NOT NULL DEFAULT false,
    shutdown_timeout_min INTEGER     NOT NULL DEFAULT 10 CHECK (shutdown_timeout_min BETWEEN 1 AND 120),
    status               TEXT        NOT NULL DEFAULT 'scheduled'
                             CHECK (status IN ('scheduled', 'running', 'done', 'failed', 'cancelled')),
    created_by           TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    cancelled_by         TEXT,
    started_at           TIMESTAMPTZ,
    finished_at          TIMESTAMPTZ,
    result               JSONB       NOT NULL DEFAULT '[]'::jsonb,
    error                TEXT
);
CREATE INDEX IF NOT EXISTS vm_changes_cluster ON backup.vm_changes (cluster_id, created_at DESC);
CREATE INDEX IF NOT EXISTS vm_changes_due ON backup.vm_changes (status, run_at);
