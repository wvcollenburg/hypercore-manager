# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Exports: clusters without a folder can be given an export share too (admin menu on the "No folder" group); their exports land in `<share>/no_folder/`. Folder names with spaces map to underscores on the share.
- **Multi-cluster.** HyperCore clusters are registered in the dashboard (Settings → Clusters)
  instead of `.env`; credentials are stored Fernet-encrypted in PostgreSQL under the new
  `HCM_SECRET_KEY`. The collector and balancer run one worker per enabled cluster, every
  InfluxDB point carries a `cluster_id` tag and every query filters on it, tunables are per
  cluster with a Defaults template, and a cluster rail appears when more than one is enabled.
  Fresh installs start with an onboarding form. `SC_HOST`, `SC_USERNAME`, `SC_PASSWORD` and
  `SC_VERIFY_SSL` are gone from `.env`; data written before this version has no cluster tag
  and is not shown. New migration `020_cluster_registry.sql`.
- `shared/` holds the single source of the modules every service embeds, with
  `tools/sync_shared.py` and a test that fails when a copy drifts.
- Sentinel runs one analysis engine per enabled cluster and its REST API answers for the
  cluster named by `?cluster=`; live tunables are per cluster. The dashboard rail shows the
  balancer mode (dry run / live) and the latest Foresight health score per cluster; settings
  can target the selected cluster, the Defaults template, or be applied to all clusters.
- Registering the same HyperCore cluster twice (for example two node addresses) is refused at
  registration and, if it slips through, detected at first contact: the newer entry is flagged
  in the dashboard and the balancer suspends itself for it so only one balancer acts per
  physical cluster.

- **Exports module** (`exports` profile, migration 022): the former hypercore-export-manager,
  rebuilt on the cluster registry and PostgreSQL. Daily VM exports to SMB/NFS in timestamped
  folders with retention pruning (SMB over the protocol, NFS via a mount), VM import into any
  registered cluster with duplicate-name guard, live task progress, and re-attachment of runs
  interrupted by a restart. Dashboard tab with Schedules, VMs, Import and Runs per cluster.
  Shares are managed by admins (Settings → Shares, migration 023) and assigned per folder; users
  schedule exports and run imports without seeing paths or credentials. Exports are written
  under `<share>/<HyperCore Manager folder>/` and imports are scoped to the cluster's folder,
  so the share layout mirrors tenancy.
- Folders in the cluster rail with per-user access (migration 021), searchable members and
  per-user folder dialogs, a users panel with search and by-folder grouping, breadcrumbs, rail
  search, per-cluster data-quality banner, Account page.

### Changed
- Released as community-supported open-source software under the Apache License 2.0.
  Copyright Scale Computing. The source-available "all rights reserved" terms are withdrawn.
- Scale Computing logos removed from the dashboard and login page; replaced by a text wordmark.
- Product names lose the `SC//` prefix: Foresight, Infraviewer, Clusterbalancer, Foresight-Central.
- Dashboard shows a one-time community-support notice per user after login
  (new migration `019_community_ack.sql`).
- Dashboard, Balancer and Collector Python dependencies are pinned.
- Added CONTRIBUTING.md, SECURITY.md, NOTICE, issue templates, a CI workflow and a test suite.

### Removed
- Windows Deploy Wizard and the cloud-init provisioning path. Use Docker Compose or `install.py`.
- Experimental LCARS dashboard theme (`/lcars`, `/lcars2`).

### Fixed
- Apply Update failed on arm64 hosts because the dashboard image shipped an x86_64-only
  `docker-compose` binary. The binary is now fetched for the image's own architecture.
- The running version is shown in the footer even when the update check cannot reach the
  remote repository.
- Obsolete `version:` key removed from `docker-compose.yaml`.
- Sentinel: Flux queries that drop the `_time` column (`query_active_vms`,
  `query_running_vms`) crashed the record normaliser with a KeyError. The VM
  pruning loop had therefore failed on every cycle since April 2026, and the
  cluster-health HA memory check silently counted RAM of VMs that were no
  longer running. Both work again. The prune's own SQL also compared a
  `uuid` column with a `text[]` parameter; cast fixed.

## [0.9.2] - 2026-05-10
- Public beta release preparation; first release with the Windows installer attached.

## [0.9.1] - 2026-05-09
- Running version shown in the dashboard footer.

## [0.9.0] - 2026-05-09
- In-app updater compares `VERSION` against the latest semver git tag.
- Database migrations apply automatically on dashboard start and during Apply Update.
