-- Media management (1.9.0): who owns the ISOs and disk images on a cluster (William, 2026-11-22).
--
-- HyperCore keeps ISOs and uploaded disk images per cluster and knows nothing of tenants. A row here gives
-- one of them an owner layer of the tenant tree: the layer of whoever uploaded it through HCM. Only that
-- layer and the layers below it see the item, plus the layers it is assigned to (down the cluster's own
-- branch) and the top-level administrators. An item with no row, or owner_node NULL, belongs to the
-- top-level administrators: everything that was on the cluster before, or was put there outside HCM.
CREATE TABLE IF NOT EXISTS config.media (
    cluster_id     UUID        NOT NULL REFERENCES config.clusters (cluster_id) ON DELETE CASCADE,
    kind           TEXT        NOT NULL CHECK (kind IN ('iso', 'disk')),
    hc_uuid        TEXT        NOT NULL,
    name           TEXT        NOT NULL,
    owner_node     UUID        REFERENCES config.folders (folder_id) ON DELETE SET NULL,   -- NULL: top-level admins
    assigned_nodes UUID[]      NOT NULL DEFAULT '{}',
    uploaded_by    TEXT,
    uploaded_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (cluster_id, kind, hc_uuid)
);
