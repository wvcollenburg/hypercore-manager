# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [1.2.1]

### Fixed
- **The VM console did not open after updating from 1.1.x** ("Console service unreachable"). 1.2.0 adds
  the `console` service, but the update is carried out by the dashboard that is running at that moment,
  and a 1.1.x dashboard only starts the services on its own fixed list, which has no `console`. The
  image was built, the container never created. Two fixes:
  - The dashboard now starts the services an older updater could not know about (currently only
    `console`) by itself, about half a minute after it starts, when they have no container at all.
    Services an admin deliberately runs without (Sentinel, the backup proxy) are left alone.
  - The updater takes the list of services to recreate from the `docker-compose.yaml` it has just
    pulled, instead of a list in its own code, so future new services come up with the update itself.
  An installation on 1.2.0 gets the console started by the update to 1.2.1. On 1.2.0 without the
  update: `docker compose up -d --build console` in the HCM folder.

## [1.2.0]

### Added
- **VM console from HCM** (new `console` service). **Console** in a VM's detail panel
  (administrators) opens the VM's console in its own window, without the HyperCore session ever
  reaching the browser: HyperCore's console page runs on that session, and whoever lifts it from a
  browser is HCM's service account on the cluster. The browser talks only to the dashboard
  (`/console/ws`); the dashboard checks the HCM session and a one-time ticket (30 s, single use,
  bound to that browser session and that one VM) and pipes the stream to the console service, which
  holds the cluster session and opens HyperCore's console websocket for exactly that VM. Buttons for
  the key combinations the client OS keeps (Ctrl+Alt+Del, Win, Win+L, Alt+Tab, Alt+F4, Ctrl+Alt+Fn),
  **Type text…** for long passwords, fit to window and full screen. A console closes after
  `CONSOLE_IDLE_MINUTES` (15) without input and after `CONSOLE_MAX_HOURS` (8). Every session is
  logged (who, VM, from where, start, end, why it ended) in Settings → Console; HyperCore does not
  log console access. noVNC 1.5.0 is vendored at build time like Chart.js.
- **Open console sessions check** (migration `041`). Per cluster, HCM takes a screenshot of each
  running VM's console on a schedule (default every 30 minutes, spread over the interval, shared
  connection, no input unless the wake option is set) and looks for a lock or login screen: text
  recognition (tesseract) for login prompts and lock-screen texts, plus reference screenshots an
  admin learns with **Learn locked** for graphical lock screens. A console that shows neither is a
  warning on the cluster card. Off by default; per-VM exclude, optional wake by pointer nudge or
  Shift, only the verdict stored unless screenshots are kept. Automated grabs are in the console log
  as user `system`, hidden by default and kept 30 days.
- **Cluster alert badges** (migration `042`). The warning sign on the cluster card becomes one badge
  per severity with a count, in HyperCore's order and colours: critical, warning, notice, info.
  They carry HyperCore's active conditions (SET and OK; read from the Collector's existing 5-minute
  poll, no extra login), a terminated cluster update and open console sessions. Each line can be
  muted for 1-365 days for everybody; each node card on the Nodes tab shows its own conditions and badges; **History** lists past conditions with start, end and
  duration (from this version on: the Collector now stores each condition's `lastSetTime`,
  `lastClearTime` and tags).

- **Edit VMs and power actions, now or in a maintenance window** (migration `043`, administrators).
  **Edit** on a VM's line opens every setting: name, description, tags, OS type, vCPUs, memory, boot
  order, snapshot schedule, node affinity; disks (grow, add, cache mode, tiering, snapshots off, remove),
  CD-ROMs (insert or eject an ISO), network cards (type, VLAN, MAC, connected, add, remove) and
  replication (start replicating to a remote cluster, pause or resume its transmissions; ending a
  replication is not in HyperCore's REST API and stays in HyperCore itself); plus Start, Shut down OS, Reboot, Power off, Reset and Delete VM. Every change
  set runs **now** or **at** a chosen time. HCM holds it until then, so it can be cancelled up to its
  start. Changes HyperCore takes on a running VM (growing or adding a disk, ISOs, metadata) apply live;
  everything else needs the VM off and runs as OS shutdown, change, start (a reboot is not enough). A
  guest that has not shut down after the timeout (default 10 minutes) leaves the VM running and
  unchanged and the change fails, unless **Force power off if the graceful shutdown fails** is ticked.
  The OS shutdown is asked again every minute while waiting: a guest that is still booting ignores the
  first request (seen on a lab cluster).
  Removing a disk or NIC and deleting a VM need the VM's name typed; a snapshot is taken before a disk
  is removed (kept 30 days). Every change set keeps its steps, who made it and when. Stopped VMs are now
  listed too, so they can be started.

- **New VM** (administrators). Name, description, tags, OS type, firmware (BIOS, UEFI, UEFI + vTPM,
  vTPM compatibility) and Secure Boot, vCPUs, memory, disks (empty, or the first one cloned from an
  image on the cluster, such as a cloud image), network cards, an install ISO, the guest tools ISO,
  cloud-init user-data for image-based VMs, snapshot schedule, and whether to start it. Created now or
  at a chosen time, as a change set like any edit; a step that fails after the VM exists leaves the VM
  in place for inspection. Verified on a lab cluster: built from the Ubuntu cloud image, changed, a disk
  deleted (snapshot first) and destroyed, and the force-off path on a VM without an OS.

- **Snapshot and Clone** in a VM's details (administrators). Snapshot: label, quiesce through the guest
  agent, replicate (when the VM replicates), and an optional removal after N days; the VM's snapshots
  are listed below. Clone: name, description, tags, from the current state or an existing snapshot,
  started or not; HyperCore gives the clone new MAC addresses (verified). Both run now or at a time and
  are kept with the VM's other changes.

### Fixed
- **A backup proxy VM's console was buried under kernel messages** (proxy `0.5.7`). nbd prints a line
  for every I/O on a disconnected device, and on the proxy VM's text console those lines hid the login
  prompt, so the open console session check could not read it. On a proxy VM the proxy now lowers the
  kernel's console log level to critical messages only; everything is still in `dmesg` and the
  journal. Not done for the proxy inside the HCM stack (`PROXY_TLS=off`), whose host is not ours.
  Existing proxies get it with the automatic proxy update after the stack update.

### Changed
- **VM list: grouped by tag by default, collapsible groups, and a search field** that matches name, all
  tags, description, guest IPs, MACs, VLAN, mounted ISO, disks, OS and node (read live from HyperCore
  through the console service, cached a minute). Grouping and folded groups are remembered per browser;
  switching cluster clears the search.
- **Infraviewer in four tabs: VMs, Nodes & Drives, VM performance, Cluster events.** Selecting a cluster
  opens its VM list (Settings excepted). The event list shows up to 30 finished tasks and says when a
  cluster has no REST v2 task feed instead of disappearing.
- **VMs as a list** (Infraviewer → VMs & Storage). The VM cards became one row per VM: name (with tag,
  peer and VLAN), CPU and IOPS bars, RAM and total disk size in GB (mounted ISOs not counted), sortable
  by every column, grouping as before. **Details** opens the VM's window with live facts from HyperCore
  above the charts: state, node, OS, firmware and Secure Boot, machine and CPU type, guest agent, node
  affinity, snapshots and their schedule, boot order, tags, every disk and every NIC with its MAC and
  the guest agent's IP addresses (read through the console service's session; cloud-init data is left
  out). **Console** (administrators) opens the console in a new browser tab, so several can be open.
- **Reverse proxies must pass websockets** for the VM console. nginx needs the `Upgrade` and
  `Connection` headers (the manual's installing chapter has the updated example); Caddy needs
  nothing. Without them only the console fails.
- The dashboard runs threaded gunicorn workers (`gthread`, 4 x 16), since a console is a
  long-lived websocket.
- Notice mutes moved from `backup.notice_mutes` to `config.notice_mutes`; existing mutes are kept.

## [1.1.1]

### Fixed
- **A backup proxy deployed or registered for one cluster ended up on another.** The proxy forms
  in Settings have their own cluster choice, but the dashboard replaced the cluster in every
  forwarded request with the cluster selected in the sidebar. With more than one cluster, a proxy
  meant for the second cluster was deployed on (or registered for) the selected one. A cluster
  named in a request is now checked against the clusters the user may manage and kept; one the
  user may not manage is refused instead of swapped.

## [1.1.0]

### Added
- **Browse BitLocker and LUKS volumes with a key** (proxy `0.5.3`). File browsing used to show an
  encrypted volume as an unsupported filesystem. It now lists it as locked and asks for the key (a
  BitLocker recovery password or a LUKS passphrase; several keys are tried on every volume); the proxy
  opens the volume read-only with `cryptsetup` and browses it like any other. Keys are passed for that
  one mount only, on cryptsetup's stdin, and never stored or logged. For BitLocker the window shows
  the recovery key ID and the volume description, so the admin can find the key that was valid for
  that point in AD or Entra ID. Verified with a real LUKS volume;
  BitLocker is still to be tried on a real Windows point.
- **Proxy log bundle** (proxy `0.5.1`). **Download logs** on a backup proxy (Settings) fetches a zip
  with the proxy's log, its recent jobs and file-level mounts, and its kernel messages, devices,
  mounts, disk space, memory and processes; passwords and tokens are left out. The proxy now also
  keeps its log in a rotating file on its data volume (at most ~20 MiB). The backup service pushes
  0.5.1 to every idle proxy after the stack update.
- **Backup schedules** (migration `036`). Create a schedule, then add VMs to it, from every cluster
  in the folder. Backup schedules (incremental) and export schedules are separate kinds. A
  schedule repeats on an RFC 5545 rule (every N days, chosen weekdays every N weeks, the Nth or last
  weekday of the month, a day of the month, or any custom RRULE) and has an earliest start, a
  preferred finish, a buffer, blackout periods and its own time zone. Members start when the window
  opens, at most one backup per proxy (one export per cluster) at a time, so no run takes its
  snapshot and then waits for a proxy. Adding a VM with a plan of its own takes that plan in with
  its chain (no new full); the newest assignment wins. A run that ran into a blackout or finished
  after the preferred finish gets a **notice**, a new level between information and warning.
  Existing per-VM plans keep running on their own time.
- **Predicted finish for backup schedules** (migration `038`). Each VM's backup time is learned from
  its own runs: the 80th percentile of what changes on that weekday, the cluster's measured read
  rate and the fixed work around a copy (a small model, not the balancer's forecasting: one sample
  per night is too little for that). When a window opens the night is planned once: VMs still
  learning start right away, learned VMs (five timed incrementals) are placed backwards so the night
  ends at the preferred finish minus the buffer; a VM behind plan starts as soon as a proxy is free.
  **Night plan** shows the plan per VM next to what ran, the card shows the predicted end or the
  predicted overrun, and runs that end inside the buffer get a notice. Proxy selection now prefers
  an idle proxy.
- **Full backups on chosen days, real or synthetic** (migration `037`, proxy `0.5.0`). A backup
  schedule can take its fulls on a rule (for example the first Sunday of the month) instead of every
  N runs. A *real* full reads the whole VM again; a *synthetic* full runs the normal incremental,
  verifies the chain up to it and has the proxy build a new chain from it on the target (new
  `synthesize` job), so the cluster sees only the incremental. The anchor snapshot moves to the new
  chain and the old chain retires. When the chain fails its verification or the proxy cannot build
  the full, the old chain stays as it is, the run gets a notice and the next run reads the VM in
  full. Restore points show which fulls were synthetic.
- **Hourly backup schedules and Run now** (migration `039`). A schedule can repeat every hour or every
  N hours (counted from midnight), for smaller increments; such a schedule has no start and finish
  time, each run may start until the next one is due. **Run now** on a schedule backs up every VM of
  it once, straight away, regardless of its window and blackouts, one backup per proxy at a time. An
  occurrence is now stored as the moment its window opened instead of its day; existing runs are
  converted.
- **Whole-day blackouts.** A blackout period ending at 23:59 now lasts until midnight, so
  00:00–23:59 blacks out the whole day; before, a run could still start in the last minute.
- **Run timing** (migration `035`). Every backup and export run records where its time went: when
  the work began after the pre-flight wait, the wait for a free proxy, the copy itself and the
  remaining overhead. Backup schedules learn their durations from these columns.

### Security
- **Backup data of another cluster was readable by naming it.** The dashboard forwarded the
  browser's `cluster` parameter to the backup service ahead of the one it had checked, and the
  backup service used the first. A folder member could read the plans, runs and restore points of a
  cluster outside their folders. Only the checked cluster is forwarded now.

### Fixed
- **About 2-3 % of backups failed with "image check failed"** (proxy `0.5.6`). After writing an
  image the proxy disconnects it from `qemu-nbd` and checks it with `qemu-img check`. The
  disconnect returns before the `qemu-nbd` server has closed the image (written its metadata,
  released its lock), and the check sometimes ran in that window. The proxy now waits until the
  server is gone. The check also says why it failed (the reason is in the run's error and the
  proxy log, it used to be thrown away), retries briefly when the image is still locked, and lets
  an image with only leaked clusters pass (wasted space, no wrong data). The log bundle now
  includes the container's `memory.events` and anon/file memory, which tell a real out-of-memory
  apart from a full page cache.
- **A proxy on DHCP that got a new address was lost.** HyperCore Manager kept calling the address
  it had at registration. When a proxy no longer answers there, the address its VM has now is read
  from HyperCore (reported by the guest agent) and the registration follows it, but only when the
  proxy at that address shows the pinned certificate and accepts its token. This happens before
  every run, restore, file browse, verify and image update, and on "Test" in Settings → Proxies,
  which says where the proxy was found. The connection stays one-way (HyperCore Manager → proxy),
  so no firewall opening towards HyperCore Manager is needed. A proxy registered by DNS name is
  left to DNS.
- **A proxy stopped working after about a thousand jobs** (proxy `0.5.5`). After four days of hourly
  backups every job failed with `qemu_thread_create: Resource temporarily unavailable`, and so did
  the proxy's own clean-up of file-browsing mounts. The proxy ran as PID 1 in its container, where
  nothing collects the daemons that `qemu-nbd` and `ntfs-3g` leave behind when they exit; these
  most likely piled up until no new thread could start. The image now runs under `tini`, which
  collects them. Proxies that are already deployed get it with the update. The log bundle now also
  holds `processes.txt` (every process with its state, and a count of exited-but-uncollected ones)
  and `limits.txt` (the kernel, process and cgroup limits that can refuse a new thread), and `ps`
  and `free` work again in it.
- **A terminated cluster update deferred every backup.** When an update pre-check stops a HyperCore
  update (seen on 9.8.7: "SecureBoot needs to be disabled on this node"), the cluster reports the
  update as `TERMINATING` indefinitely while still running the old version. Backups and restores
  took that for an update in progress and were deferred for days. They now run. The cluster's card
  in the sidebar shows a warning sign; it opens the cluster's reason, and the warning can be muted
  for 1 to 365 days (migration `040`). A mute is shared by all users and holds for that update only:
  the next update that is terminated shows again.
- **File browsing: LVM inside LUKS, NTFS fallback, hidden skips** (proxy `0.5.4`). A LUKS volume
  holding LVM (Ubuntu's encrypted layout) opened but its logical volumes did not appear: LVM could not
  write its temporary name on a read-only LUKS device. LUKS now opens writable on the private
  copy-on-write view (the backup is never written). The `ntfs-3g` fallback for an NTFS volume the
  kernel driver refuses failed for want of a `/dev/mapper` node; the node is now made first.
  Volumes that are found but cannot be mounted are listed in the window instead of looking absent;
  Microsoft reserved and BIOS boot partitions, empty by design, are not.
- **Restore points and file browsing open in a window of fixed size.** Both were inline in the
  restore points list, so the page grew and shrank with every folder. **Points** now opens a window
  with the VM's points (restore forms open in it), **Files** turns it into the browser of a point with
  a way back to the points, and only the list inside scrolls. Leaving or closing the browser unmounts
  the point.
- **Backups missed the end of disks whose size is not a whole number of MiB** (proxy `0.5.2`).
  HyperCore shows a guest such a disk rounded up to whole MiB (a "100 GB" disk is 100,000,595,968
  bytes), but disk images were capped at the API capacity, so the last partial MiB, with the backup
  copy of the GPT, was not backed up. Restores booted (the primary GPT was intact) but file browsing
  of those points failed. Images now take the clone's full size; the first delta on an existing chain
  copies the missing end, and older points show *end of disk missing*. File browsing reads a GPT
  that `sfdisk` refuses for such a short image directly when its checksums hold, so existing points
  are browsable too.
- **File browsing: why nothing could be mounted.** *No mountable filesystem found in this point* now
  names the reason for every partition (unsupported filesystem, unreadable partition table, mount
  error), and a partition that will not mount is left out instead of failing the whole point (proxy
  `0.5.1`). A GPT disk read as only its protective MBR says so, with what its first sector after the
  MBR holds.
- **Migrations stuck at `015`.** On installs where `015_sharing_prefs.sql` ran before the migration
  runner recorded it, the runner re-ran it on every start and failed (`'default'` is no UUID since
  `020`), so no later migration was applied. `015` now seeds its row only while the column is still
  text.
- **Backup service crash on upgrade.** When the dashboard had not yet applied a new migration the
  backup service needs, it exited at start and restarted in a loop; it now logs the error and its
  periodic sync catches up once the tables are there.
- **Migrations retried instead of given up.** The dashboard stopped trying after eight failed
  attempts at start, so a migration fixed afterwards waited for a dashboard restart. It now retries
  every five minutes and logs that migrations are not applied.
- **Disabled action buttons look disabled** (for example *New schedule* for backup schedules when the
  cluster has no backup proxy); before they looked the same as enabled ones.

## [1.0.0]

The community release: one installation for many clusters, folders and users, incremental
backups through a backup proxy, and an Apache-2.0 licence. First release on the Scale Computing
public GitHub.

### Upgrade notes (from 0.9.x)
- Add `HCM_SECRET_KEY` to `.env` before starting; it encrypts the cluster and share passwords now
  kept in PostgreSQL. Back it up with the database.
- `SC_HOST`, `SC_USERNAME`, `SC_PASSWORD` and `SC_VERIFY_SSL` are ignored. After the upgrade the
  dashboard asks you to register the cluster again; its balancer starts in dry-run.
- Metrics written before this release have no cluster tag and are no longer shown. Forecast history
  restarts from the moment a cluster is registered.
- New migrations `019`–`024` apply automatically on the first dashboard start.
- Step-by-step instructions: the manual's *Installing and updating* chapter, "Upgrading from 0.9.x".

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
- **Backup module** (`backup` container, port 8082; named Exports during development): the former hypercore-export-manager,
  rebuilt on the cluster registry and PostgreSQL. Daily VM exports to SMB or NFS in timestamped
  folders with retention pruning (SMB over the protocol, NFS via a mount), import into any
  registered cluster with a duplicate-name guard, live task progress, and re-attachment of runs
  interrupted by a restart. Dashboard tab with Schedules, VMs, Import and Runs per cluster.
- **Backup targets managed by administrators** (Settings → Targets) and assigned per folder, so users
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
  is created with a cloud-init document that installs Docker and a small receiver (migration 028).
- **The proxy image is pushed, never pulled.** The backup service pushes this installation's own
  proxy image (exported by the dashboard) to a new proxy VM over TLS, pinning the certificate
  the receiver generates; the proxy container keeps serving that certificate, so the pin holds
  through the handover. No registry and no internet access from the VM, and a proxy always runs
  the version the stack was built with. After every stack update the backup service pushes the
  new image to each idle proxy and the VM swaps the container; the proxies panel shows each
  proxy's version and offers **Update** for one that lags.
- **Removing a proxy can delete its VM.** Remove asks whether the proxy's VM goes too and then
  powers it off and deletes it; only a VM the dashboard deployed (tag `hcm-proxy`) is deleted.
- The proxy deployment log keeps finished rows for 30 days, except the last successful one of
  each registered proxy; **Clear finished** empties it at once.
- Per-cluster data-quality banner that shows how much history a cluster has since registration.
- HyperCore REST OpenAPI specs (v1 and v2) under `docs/`, with contract tests that pin the
  calls this project makes.
- `shared/` holds the single source of the modules every service embeds, with
  `tools/sync_shared.py` and a test that fails when a copy drifts.
- Test suite (unit and optional integration), CI workflow, issue templates, CONTRIBUTING,
  SECURITY, NOTICE, a dashboard favicon, and an administration guide (`docs/administration.md`).

### Changed
- **Tunables live in the dashboard only.** The balancer, collector and dashboard tunables
  (dry-run, thresholds, cooldowns, the Oracle, poll intervals, the stale threshold, excluded
  nodes) are no longer in `.env`, the compose file or the installer. They ship with defaults,
  seed the Defaults template once, and are edited per cluster under Settings → Tunables. The
  earlier seed re-applied `.env` values at every restart to any tunable set back to its default
  in the dashboard, which could switch dry-run off again; it now never overwrites a stored value.
- Released as community-supported open-source software under the Apache License 2.0.
  Copyright Scale Computing. The source-available "all rights reserved" terms are withdrawn.
- Scale Computing logos removed from the dashboard and login page; replaced by a text wordmark.
- Product names lose the `SC//` prefix: Foresight, Infraviewer, Clusterbalancer.
- Dashboard shows a one-time community-support notice per user after login
  (migration `019_community_ack.sql`).
- Dashboard, Balancer, Collector and Backup Python dependencies are pinned.
- `TZ` in `.env` sets the timezone for scheduled jobs and log timestamps.
- The module is called **Backup** everywhere (tab, `backup` container, `/api/backup`,
  `BACKUP_*` variables, schema `backup` with `targets` and `plans`); `EXPORTS_*` variables keep
  working for one release. Migration `025_backup_rename.sql`.

### Removed
- **Foresight-Central integration (data sharing).** The push of signed capacity snapshots to a
  central viewer, the anonymiser, the encrypted `.hcb` export and the Data Sharing tab are
  gone, with their API routes, the `config.sharing_prefs` table (migration `034`) and the
  setup guide. The central viewer is retired; a service provider runs one HyperCore Manager
  with a folder per customer instead.
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
- **The in-app updater** recreates the application services only; InfluxDB and PostgreSQL keep
  running through an update (the restart container's own compose version used to hash the
  config differently and recreated everything). Files git touched inside the container are
  handed back to the checkout's owner, so the host user's next `git pull` no longer fails on
  root-owned refs. The repository link in the dashboard follows the checkout's git origin.
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
