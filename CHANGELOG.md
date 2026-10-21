# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

This is the community release: one installation for many clusters, folders and users, scheduled
exports, and an Apache-2.0 licence. It is 1.0.0 material; the version number is set when the
release is tagged.

### Upgrade notes (from 0.9.x)
- Add `HCM_SECRET_KEY` to `.env` before starting; it encrypts the cluster and share passwords now
  kept in PostgreSQL. Back it up with the database.
- `SC_HOST`, `SC_USERNAME`, `SC_PASSWORD` and `SC_VERIFY_SSL` are ignored. After the upgrade the
  dashboard asks you to register the cluster again; its balancer starts in dry-run.
- Metrics written before this release have no cluster tag and are no longer shown. Forecast history
  restarts from the moment a cluster is registered.
- New migrations `019`–`024` apply automatically on the first dashboard start.
- Step-by-step instructions: DEPLOY.md, "Upgrading from 0.9.x".

### Added
- **Multi-cluster.** HyperCore clusters are registered in the dashboard (Settings → Clusters)
  instead of `.env`, with a connection test, enable/disable and remove (optionally purging
  history). The collector, balancer and Sentinel run one worker per enabled cluster, every
  InfluxDB point carries a `cluster_id` tag and every query filters on it. Tunables are per
  cluster with a Defaults template and "apply to all". The same HyperCore cluster registered
  twice is refused at registration and detected at first contact; the duplicate's balancer
  suspends itself.
- **Folders, users and roles.** A cluster rail with collapsible, colour-coded folders and a
  search box (folder, cluster name, address). Administrators see everything; users see the
  folders they are members of. Folder menus move clusters, edit members and assign a backup
  share. Settings has sub-tabs (Clusters, Tunables, Users, Shares); the users panel searches,
  sorts A–Z or by folder, and edits a user's folders. Modules and Settings are admin-only and
  live in the rail footer; everyone gets an Account page. Breadcrumbs in the header.
- **Backup module** (`backup` profile, port 8082; named Exports during development): the former hypercore-export-manager,
  rebuilt on the cluster registry and PostgreSQL. Daily VM exports to SMB or NFS in timestamped
  folders with retention pruning (SMB over the protocol, NFS via a mount), import into any
  registered cluster with a duplicate-name guard, live task progress, and re-attachment of runs
  interrupted by a restart. Dashboard tab with Schedules, VMs, Import and Runs per cluster.
- **Shares managed by administrators** (Settings → Shares) and assigned per folder, so users
  create backup plans and run imports without seeing paths or credentials. Copies are written
  under `<share>/<folder>/<vm>_<timestamp>/` and imports are scoped to the cluster's folder;
  spaces in folder names become underscores. Clusters without a folder use the share flagged
  as their default and the `no_folder` directory. Share usernames may be `user`, `DOMAIN\user`
  or `user@domain`.
- **Incremental backups through a backup proxy** (`proxy` container, migration 026). HyperCore's
  changed-block tracking (`snapDiff`) tells the proxy which 1 MiB blocks changed; the proxy reads
  them from a read-only snapshot clone hot-added to its own VM and writes qcow2 chains (base +
  deltas) onto the target. Plans have a mode (`incremental` / `export`), a full every N runs,
  retention by points and days with merge-forward pruning, a reference snapshot per plan, and
  restore points with verification. Proxies are registered per cluster and auto-detect their VM
  by MAC address. Deleting a plan retires its chain instead of deleting backups.
- **Restore and file recovery from restore points** (migration 027). A VM is restored from any
  point as a new VM (`<name>-restored-<time>`, MAC addresses kept or regenerated, NIC
  disconnected on request); single files are browsed and downloaded through the dashboard, or
  delivered as an ISO inserted into a running VM's CD-ROM and ejected afterwards. Nothing is
  written into a guest through the hypervisor.
- **Remote proxies** for clusters that do not host the stack: the same proxy container in a small
  VM, reached over HTTPS with a pinned self-signed certificate (or a customer certificate),
  several proxies per cluster with rotation and one job per proxy. An administrator can deploy
  such a VM from the dashboard: an Ubuntu cloud image is uploaded to the cluster once and the VM
  is created with a cloud-init document that installs Docker and the proxy container and
  registers itself (migration 028).
- Per-cluster data-quality banner that shows how much history a cluster has since registration.
- HyperCore REST OpenAPI specs (v1 and v2) under `docs/`, with contract tests that pin the
  calls this project makes.
- `shared/` holds the single source of the modules every service embeds, with
  `tools/sync_shared.py` and a test that fails when a copy drifts.
- Test suite (unit and optional integration), CI workflow, issue templates, CONTRIBUTING,
  SECURITY, NOTICE, a dashboard favicon, and an administration guide (`docs/administration.md`).

### Changed
- Released as community-supported open-source software under the Apache License 2.0.
  Copyright Scale Computing. The source-available "all rights reserved" terms are withdrawn.
- Scale Computing logos removed from the dashboard and login page; replaced by a text wordmark.
- Product names lose the `SC//` prefix: Foresight, Infraviewer, Clusterbalancer, Foresight-Central.
- Dashboard shows a one-time community-support notice per user after login
  (migration `019_community_ack.sql`).
- Data sharing with Foresight-Central is configured per registered cluster.
- Dashboard, Balancer, Collector and Backup Python dependencies are pinned.
- `TZ` in `.env` sets the timezone for scheduled jobs and log timestamps.
- The module is called **Backup** everywhere (tab, `backup` container and profile, `/api/backup`,
  `BACKUP_*` variables, schema `backup` with `targets` and `plans`); `EXPORTS_*` variables and the
  `exports` profile keep working for one release. Migration `025_backup_rename.sql`.

### Removed
- Windows Deploy Wizard and the cloud-init provisioning path. Use Docker Compose or `install.py`.
- Experimental LCARS dashboard theme (`/lcars`, `/lcars2`).
- Cluster credentials in `.env`.

### Fixed
- Apply Update failed on arm64 hosts because the dashboard image shipped an x86_64-only
  `docker-compose` binary. The binary is now fetched for the image's own architecture.
- The running version is shown in the footer even when the update check cannot reach the
  remote repository.
- Migrations no longer race between gunicorn workers on start-up (advisory lock).
- Obsolete `version:` key removed from `docker-compose.yaml`.
- Sentinel: Flux queries that drop the `_time` column (`query_active_vms`,
  `query_running_vms`) crashed the record normaliser with a KeyError. The VM
  pruning loop had therefore failed on every cycle since April 2026, and the
  cluster-health HA memory check silently counted RAM of VMs that were no
  longer running. Both work again. The prune's own SQL also compared a
  `uuid` column with a `text[]` parameter; cast fixed.
- Foresight and balancer settings follow the selected cluster; Grafana is probed through the
  backend instead of from the browser.

## [0.9.2] - 2026-05-10
- Public beta release preparation; first release with the Windows installer attached.

## [0.9.1] - 2026-05-09
- Running version shown in the dashboard footer.

## [0.9.0] - 2026-05-09
- In-app updater compares `VERSION` against the latest semver git tag.
- Database migrations apply automatically on dashboard start and during Apply Update.
