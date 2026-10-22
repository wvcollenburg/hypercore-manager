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
- **Multi-node awareness** (migration `030`). The nodes of every registered cluster are discovered
  through `GET /Node` and listed under the cluster in Settings → Clusters with their status and
  the node in use. The collector, balancer, backup service and Sentinel share one session helper
  that logs in again after a 401 and moves to another node of the cluster when the current one
  stops answering; the switch is recorded per cluster so the services follow each other.
  **Test connection** on a registered cluster tries every known node. Registering a second node
  IP of a known cluster is refused with the node named.
- **HyperCore REST v2 task feed** behind a per-cluster switch (auto / v1 only / v2; migration
  `029`). The connection test and the collector probe `/rest/v2/TaskTag`; when in use the
  collector records the cluster's tasks, Infraviewer shows a *Cluster activity* panel, the
  migration log also lists moves made outside the balancer (engine `external`), and the balancer
  holds a migration while a move runs on the cluster or the VM is busy with a snapshot, clone,
  export, import or replication.
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
- **Backup hardening against the HyperCore backup SDK rules** (migration `031`, audit of
  2026-10-22). A disk whose capacity changed since the previous point is copied in full inside the
  delta (snapDiff reports nothing for a grown region). Every snapshot and clone request is
  repeated after a transient `500` (another task holds the VM) and runs are serialised per VM.
  Reference snapshots carry a retention deadline (`BACKUP_ANCHOR_RETAIN_DAYS`, default 60) so an
  installation that vanishes leaves nothing behind for good. A run first checks that the cluster
  is not updating itself and that every node is online; otherwise it waits up to
  `BACKUP_PREFLIGHT_WAIT_HOURS` (default 2), then is recorded as **deferred**. The Backup tab shows
  a banner while a node is missing (HyperCore cannot snapshot, so nothing can be backed up until the
  node is back or evicted by Scale Computing support). Each restore point records whether the guest
  quiesced; the points list marks crash-consistent points. Restored disks go back on their original
  bus (IDE as IDE, SCSI as SCSI) with their SSD tiering factor; a refused bus is an error, never a
  silent switch to virtio. The proxy writes deltas with 1 MiB clusters (one snapDiff block each),
  reads `differentHow` in both wire forms (string enum and the integer a 9.7 cluster sends), and
  runs a one-second self-check of the delta semantics on its own qemu at start and before every
  backup job; `tests/test_proxy_qcow2.py` proves the same semantics against real qemu in CI.
- **Restore and file recovery from restore points** (migration 027). A VM is restored from any
  point as a new VM (`<name>-restored-<time>`, MAC addresses kept or regenerated, NIC
  disconnected on request); single files are browsed and downloaded through the dashboard, or
  delivered as an ISO inserted into a running VM's CD-ROM and ejected afterwards. Nothing is
  written into a guest through the hypervisor.
- **Chain integrity check before a restore** (migration `032`). A restore first re-hashes every
  file of the chain up to the chosen point against the checksums written at backup time and stops
  before anything is created on the cluster when a file does not match; the Runs panel shows the
  check's progress. The form has a checkbox to skip it (on by default) with the point's last
  verified time next to it. A point that failed a check is marked **corrupt** in the points list
  with the failing files, by the weekly verify, the manual **Verify** button and a restore alike.
  The manual Verify now runs in the background with progress, so chains larger than one HTTP
  request can be verified from the dashboard.
- InfluxDB runs with memory caps below its container limit (`INFLUXD_QUERY_MAX_MEMORY_BYTES` and
  friends in compose): a query that needs more than the container has now fails with an error
  instead of Docker killing the database, which on a 768 MB container happened on any
  whole-range scan.
- **The user manual is in the dashboard.** `docs/manual` (one chapter per module, with screenshots)
  is rendered by the dashboard at `/manual` behind the login, reachable from the rail under
  Account; it works on dark sites. The administration guide is served with it. The former
  `DEPLOY.md` is folded into the manual's installing chapter, which now holds everything about
  the installation itself, without repeating the administration guide.
- **Adopt 0.9.x history.** Metrics written before the multi-cluster release carry no cluster tag
  and were invisible after an upgrade. Settings → Clusters now offers to adopt them for the
  registered cluster: a background job re-tags them per measurement and bucket (scratch copy,
  delete, copy back; idempotent, resumes after an interruption), so Foresight and the balancer
  keep their history across the upgrade.
- **Plan times are local to the cluster** (migration `033`). The cluster's time zone is read from
  HyperCore on every connection test and shown under Settings → Clusters, with an override; a
  plan's daily time is scheduled in that zone, and every place a plan time appears adds the
  viewer's local equivalent ("03:00 US/Eastern · 09:00 your time").
- Proxy VM deployments default to 2 vCPU (measured: the single core sat at 0 % idle between the
  SMB client and the data path; a second core removes that without changing throughput).
- **Cloud-init data removed from deployed proxy VMs.** HyperCore keeps a deployed VM's cloud-init
  document as an ISO attached to the VM, and that document carries the proxy's API token in
  clear, readable by anyone who can clone or mount the VM's disks. A deployment now deletes the
  ISO once the proxy has registered (ejects it while the VM runs), and the daily sweep does the
  same for proxies deployed by earlier versions.
- **Restore staging reads the chain with qemu-img convert** (16 coroutines, out-of-order writes,
  direct I/O onto the zeroed staging disk) instead of through an nbd attach. Reading a chain from
  a CIFS share measured 59.5 MiB/s against 28.7 before, and a restore no longer holds an nbd
  slot. On the lab the end-to-end restore time did not change (118 s against 120 s for 6 GB):
  there the write into the staging disk caps at ~50 MiB/s, so the gain shows only where the
  target writes faster than the share reads. The bench has a restore profile (`profile:
  restore` on the API) that compares every read strategy.
- **Proxy write throughput onto SMB shares doubled.** The proxy writes qcow2 images with lazy
  refcounts and qemu-nbd's unsafe cache mode by default (`PROXY_QCOW2_LAZY_REFCOUNTS`,
  `PROXY_NBD_CACHE`), which removes the SMB flush qemu issued for every MiB written; a finished
  point is synced to the share before its checksum is taken. Lab measurement onto a CIFS share:
  55 MiB/s against 37 before, with a plain file copy at 45. **Measure throughput** on
  a registered proxy (Settings → Backup proxies) writes 512 MiB per mode onto the cluster's target
  and reports MiB/s for a flat file, today's path, the alternatives and the restore read path, so
  the setting can be chosen per site.
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
- **Grafana.** The optional `tools` profile shipped a bare Grafana without a datasource or a
  dashboard; the dashboard covers the metrics. Point your own Grafana at InfluxDB (Flux, bucket
  `metrics`, a read token) if you want one. `GF_SECURITY_ADMIN_PASSWORD` is no longer read.
- **Compose profiles.** `docker compose up -d` starts everything (core, Sentinel, Backup and its
  proxy). Old `--profile sentinel|backup|full` commands still work and change nothing; a module
  you do not use is stopped with `docker compose stop <service>`.
- Windows Deploy Wizard (`HyperCoreDeploy.exe`, still attached to the v0.9.2 release for history)
  and the cloud-init provisioning path. Use Docker Compose or `install.py`.
- Experimental LCARS dashboard theme (`/lcars`, `/lcars2`).
- Cluster credentials in `.env`.

### Fixed
- **Infraviewer storage graphs were slow and could kill dashboard workers.** The nodes and VMs
  views each fetched every disk's full history (`/api/vsds`, 1 MB and several seconds at the
  24-hour window) every 30 seconds, and the stale-data check opened a PostgreSQL connection per
  disk. Under load that exceeded gunicorn's timeout. They now share one request to the new
  `/api/storage/summary`: one series per VM (IOPS and throughput summed over its disks, latency of
  the worst disk) plus a cluster total, aggregated by InfluxDB, with each disk's newest sample for
  the VM cards. Per-disk history is only fetched for the VM a detail panel shows
  (`/api/vsds?vm=<uuid>`), and the stale threshold is read once per request.
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
