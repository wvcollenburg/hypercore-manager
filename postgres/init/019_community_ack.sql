-- Community-support notice acknowledgement
-- One boolean per dashboard user; the notice is shown until it is set.
-- Idempotent: safe to re-run on existing installs.

ALTER TABLE config.users
    ADD COLUMN IF NOT EXISTS community_ack BOOLEAN NOT NULL DEFAULT FALSE;
