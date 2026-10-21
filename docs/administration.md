# Administration guide

Day-to-day administration of a HyperCore Manager installation: registering clusters, grouping
them into folders, giving people access, and setting up targets for the Backup module. Everything
here happens in the dashboard; nothing requires a restart or an `.env` change.

Two roles exist. **Administrators** see every cluster and every panel described below.
**Users** see the clusters in the folders they are members of, operate them (balancer settings,
Foresight, backup plans, imports) and manage their own account; they do not see Settings or Modules.
When authentication is disabled (`SC_DASHBOARD_USER` left blank), everyone is an administrator.

---

## Clusters

**Settings → Clusters** lists the clusters this installation manages, with their status (never
contacted, seen *n* ago, error), whether they are enabled, and their folder.

### Registering a cluster

1. **Add cluster.** Give it a name, the management address (any node IP or hostname; `https://`
   and `/rest/v1` are added for you), a HyperCore account and password, and whether to verify the
   TLS certificate. Pick a colour if you like; it is used in the rail.
2. **Test connection** logs in and reports the cluster name, node count and HyperCore version
   before anything is stored. Registering a cluster that is already registered under another
   address is refused here (same HyperCore cluster identity).
3. **Save.** Within a minute the collector writes metrics, Sentinel starts an analysis engine and
   the balancer evaluates the cluster **in dry-run**. The cluster's tunables are copied from the
   Defaults template.

The first login on a fresh installation shows this form as an onboarding step.

Passwords are stored encrypted with `HCM_SECRET_KEY`. If the dashboard reports that the key is
not set, add it to `.env` and restart the stack before registering anything.

### What the HyperCore account needs

| Use | Rights |
|---|---|
| Monitoring, Foresight, dry-run balancing | read-only |
| Live balancing | VM live-migration |
| Backups and imports | Backup, Media Management, VM Create/Edit, VM Delete, VM Power Controls (a read-only account gets a clear 403 on the first run) |

### Disable, remove, duplicates

- **Disable** pauses collection, balancing and analysis; history stays and the cluster can be
  enabled again later.
- **Remove** stops everything immediately and asks whether to purge the cluster's Foresight
  history and settings as well.
- **Duplicate warning.** If two entries turn out to point at the same HyperCore cluster (for
  example two node addresses), the rail flags the newer one and only the older one balances.
  Remove or disable the duplicate.

Only one HyperCore Manager installation may balance a cluster live. A second installation
pointed at the same cluster must stay in dry-run.

### Going live

Every cluster starts in dry-run: the balancer logs what it would do and moves nothing. Watch
the migration log on the Clusterbalancer tab for a while; when the decisions look right, switch
**Dry run** off in that cluster's balancer settings. The rail badge changes from **DRY RUN** to
**LIVE**.

### Tunables

**Settings → Tunables** edits the balancer and Foresight parameters of the cluster selected in
the rail. The target bar at the top switches to the **Defaults** template (used for clusters
registered from now on) and offers **Apply to all clusters** for a value you want everywhere.
Changes take effect without a restart.

---

## Folders

Folders group clusters in the rail and are the unit of access for users. A cluster is in at most
one folder; clusters in no folder appear under **No folder**, which only administrators see.

- **New folder** is at the bottom of the rail. Folders can be renamed, deleted (their clusters
  stay registered and fall back to **No folder**), collapsed individually, or all at once from
  the rail header.
- The **cluster menu** ("…" on a cluster) moves it to another folder. The list is searchable and
  can create a new folder on the spot.
- The **folder menu** ("…" on a folder) holds **Members**, **Backup target**, **Rename** and
  **Delete folder**.
- The **rail search** matches folder names, cluster names and management addresses.

### Members

A folder's members see every cluster in it. The members dialog lists all non-admin users with a
search box, a scrollable list and **Add shown** / **Remove shown** for bulk changes. Administrators
are never listed: they see everything regardless of membership.

The same relationship is editable from the other side: **Settings → Users → Folders** on a user
shows which folders that user belongs to.

---

## Users

**Settings → Users** lists the accounts with a search box and two orderings: **A–Z** and **By
folder** (collapsible groups, administrators first, then one group per folder, then users without
a folder).

- **Create** a user with a role (`user` or `admin`) and an initial password. The role is fixed at
  creation; to change it, create a new account and delete the old one.
- **Reset password** and **delete** are available per account. An administrator cannot delete
  their own account.
- **Folders** opens the user's folder memberships.
- Two-factor (TOTP) is enabled by each user on their own **Account** page, not by an
  administrator.

The initial administrator comes from `SC_DASHBOARD_USER` / `SC_DASHBOARD_PASSWORD` in `.env` and
is seeded only once; manage accounts here afterwards.

---

## Targets and backups

The Backup module (profile `backup` or `full`) needs somewhere to write. Administrators define
targets centrally (an SMB or NFS share with its credentials) and attach them to folders; users
never see a URI or a credential.

### Defining a target

**Settings → Targets → Add target**:

| Field | Meaning |
|---|---|
| Name | The alias users see, for example `Backup NAS` |
| URI | `smb://host/share/path` or `nfs://host/export/path` |
| Username, password | For SMB. `user`, `DOMAIN\user` and `user@domain` are all accepted |
| Prune path | NFS only: where the share is mounted inside the `backup` container, so retention can delete old copies |

**Probe** (in the add/edit form) connects with the values typed so far and lists the directories
it finds, before anything is saved. **Test** (on a saved target) does the same with the stored
credentials. Both run from the backup container, so they prove what the scheduler will see.

### Assigning a target

From the folder menu in the rail, **Backup target** picks one of the defined targets for that
folder (or clears it). Every cluster in the folder backs up there. The **No folder** group has the
same menu; its target is flagged as the default for clusters without a folder.

One target for everyone or one per tenant are both fine. What differs is only the layout on the
share.

### Layout on the share

```
<target>/
  <Folder name>/              folder name with spaces and odd characters replaced by "_"
    <vm>_YYYYMMDD-HHMMSS/     one directory per copy, created by HyperCore
      <disk>.qcow2
      <vm>.xml
  no_folder/                  clusters that are in no folder
```

The tenant directory is created over SMB when missing (NFS: by the mount). Because the layout
mirrors the folders, you can set per-tenant permissions on the share itself, and an import can
only pick up copies from the cluster's own folder.

### Backup proxies (incremental mode)

Incremental backups read only the blocks that changed since the previous run. That work is done
by a **backup proxy**: a container that runs on a VM inside the cluster (the HyperCore Manager
VM itself when the stack runs there, or a small dedicated VM per remote cluster). HyperCore
attaches a read-only clone of the VM's snapshot to the proxy's VM; the proxy reads the changed
blocks and writes them as a qcow2 chain onto the target. See DEPLOY.md for what the proxy
container needs on its host.

**Settings → Targets → Backup proxies → Register proxy**: pick the cluster, give the proxy a
name, its URL (`http://proxy:8083` for the container in this stack, `https://<ip>:8083` for a
proxy VM; a self-signed certificate is pinned on registration) and the token from
`PROXY_API_TOKEN`. On registration the proxy is contacted and its VM on the cluster is detected
from its MAC addresses; the status column says **VM detected** when that worked. **Test
connection** repeats the probe. A paused proxy is not used for new runs.

Without a registered proxy, plans on that cluster use HyperCore's native export.

### Plan modes and retention

A plan has a **mode**:

- **incremental** (default when the cluster has a proxy): the first run writes a full copy (the
  *base*), every following run only the changed blocks (a *delta*). After *Full every N runs*
  (default 14) a fresh base is taken and the old chain retires. Retention keeps *N* points
  (default 14) and optionally drops points older than *Keep for days*; old points are merged
  into the base, never leaving a gap. A reference snapshot per plan stays on the cluster between
  runs (label `hcm-backup-<plan>-<run>`); if it disappears, the next run takes a full and says so.
- **export**: HyperCore's native export every run into a timestamped directory, retention by
  count, as in the first release. Required when no proxy exists; fine for small VMs.

**Restore points** (Backup tab) lists every chain of the cluster's VMs with its points. **Verify**
re-reads every file of a chain against the checksums written at backup time. **Delete data**
removes a chain from the target; this is the only action that deletes backups. Deleting a plan
*retires* its chain: no new points, retention keeps applying, restores stay possible.

### Restoring a VM

On the **Restore points** sub-tab, open a chain's points and press **Restore** on the point you
want. The form asks for the name of the restored VM, the MAC address policy (automatic: the
original addresses when the backed-up VM no longer exists on the cluster, new ones otherwise),
whether to connect the network interfaces, and whether to start the VM. Restores are listed
under **Runs**.

What happens: a diskless definition (generated from the stored VM record, with the NVRAM and
vTPM state of the point) is imported in seconds; the proxy then writes each data disk into a
staging disk at full speed and HyperCore clones the staging disks onto the restored VM. The
restored VM keeps the original boot order, VLANs and tags. Nothing of the backup chain is
modified by a restore, and a restore never touches the original VM.

### What users do with it

On the **Backup** tab for a cluster they can reach:

- **Plans.** Pick a VM, copies to keep, daily run time (in `TZ`) and optional compression. The
  destination is shown as `Target/Folder/vm_timestamp` and follows from the folder; it cannot be
  typed in. **Run now** starts a backup immediately.
- **VMs.** Every VM on the cluster with its plan state.
- **Import.** Browse the folder's directory on the target, pick a copy, optionally rename, and
  import into the selected cluster. A VM name that already exists is refused.
- **Runs.** Backup and import history with live progress from HyperCore. A run interrupted by a
  container restart is re-attached by its task tag.

Retention runs after each successful backup: copies of that VM beyond the retention count are
deleted, oldest first; other VMs' directories are never touched. Deleting a plan never deletes
the copies already on the target.

---

## Account

Everyone has an **Account** page in the rail footer: change the own password, enable or disable
TOTP two-factor (scan the QR code, confirm with a code), and see the language selector in the
header. Administrators use the same page for their own account.
