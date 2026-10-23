-- =============================================================================
-- 030_cluster_nodes.sql
-- Multi-node awareness: the nodes of each registered cluster as discovered through GET /Node,
-- and the node the cluster is currently reached on. Idempotent.
-- =============================================================================

-- The address the services currently talk to. Written by whichever service last switched;
-- NULL until the first contact. Informational: HyperCore has no primary node.
ALTER TABLE config.clusters ADD COLUMN IF NOT EXISTS active_host TEXT;

-- One row per node ever seen on a cluster. The collector upserts the list every poll and the
-- registration/test paths upsert what they found. A node missing from the latest list is kept
-- with present = FALSE (shown greyed) until an admin forgets it.
CREATE TABLE IF NOT EXISTS config.cluster_nodes (
    cluster_id              UUID        NOT NULL REFERENCES config.clusters (cluster_id) ON DELETE CASCADE,
    node_uuid               TEXT        NOT NULL,
    peer_id                 INTEGER,
    lan_ip                  TEXT,
    backplane_ip            TEXT,
    address                 TEXT        NOT NULL,   -- what the services connect to: the reverse-resolved
                                                    -- name when the cluster was registered by name, else lan_ip
    network_status          TEXT,                   -- ONLINE | OFFLINE | UNKNOWN
    disposition             TEXT,                   -- IN | OUT | UNKNOWN
    allow_running_vms       BOOLEAN,
    virtualization_online   BOOLEAN,
    supports_virtualization BOOLEAN,                -- FALSE on a witness node
    present                 BOOLEAN     NOT NULL DEFAULT TRUE,
    first_seen_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (cluster_id, node_uuid)
);
CREATE INDEX IF NOT EXISTS ix_cluster_nodes_uuid ON config.cluster_nodes (node_uuid);
