-- Cluster names are unique per folder, no longer across the whole installation (plans/PLAN-roles.md,
-- cluster registration at folder level, 2026-11-21).
--
-- Customer admins register their own clusters; an install-wide unique name would tell them that another
-- tenant has a cluster called "Production". Nothing keys on the name (everything uses cluster_id), so two
-- customers may now both have one. Unfiled clusters and the Defaults template share the "no folder" bucket.

DROP INDEX IF EXISTS config.ux_clusters_name;
CREATE UNIQUE INDEX IF NOT EXISTS ux_clusters_folder_name
    ON config.clusters (COALESCE(folder_id, '00000000-0000-0000-0000-000000000000'::uuid), lower(cluster_name));
