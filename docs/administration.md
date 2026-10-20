# Administration guide

Day-to-day administration of a HyperCore Manager installation: registering clusters, grouping
them into folders, giving people access, and setting up shares for the Exports module. Everything
here happens in the dashboard; nothing requires a restart or an `.env` change.

Two roles exist. **Administrators** see every cluster and every panel described below.
**Users** see the clusters in the folders they are members of, operate them (balancer settings,
Foresight, exports, imports) and manage their own account; they do not see Settings or Modules.
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
| Exports and imports | VM export/import and snapshot (a read-only account gets a clear 403 on the first run) |

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
- The **folder menu** ("…" on a folder) holds **Members**, **Export share**, **Rename** and
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

## Shares and exports

The Exports module (profile `exports` or `full`) needs somewhere to write. Administrators define
shares centrally and attach them to folders; users never see a URI or a credential.

### Defining a share

**Settings → Shares → Add share**:

| Field | Meaning |
|---|---|
| Name | The alias users see, for example `Backup NAS` |
| URI | `smb://host/share/path` or `nfs://host/export/path` |
| Username, password | For SMB. `user`, `DOMAIN\user` and `user@domain` are all accepted |
| Prune path | NFS only: where the export is mounted inside the `exports` container, so retention can delete old copies |

**Probe** (in the add/edit form) connects with the values typed so far and lists the directories
it finds, before anything is saved. **Test** (on a saved share) does the same with the stored
credentials. Both run from the exports container, so they prove what the scheduler will see.

### Assigning a share

From the folder menu in the rail, **Export share** picks one of the defined shares for that
folder (or clears it). Every cluster in the folder exports there. The **No folder** group has the
same menu; its share is flagged as the default for clusters without a folder.

One share for everyone or one per tenant are both fine. What differs is only the layout on the
share.

### Layout on the share

```
<share>/
  <Folder name>/              folder name with spaces and odd characters replaced by "_"
    <vm>_YYYYMMDD-HHMMSS/     one directory per export, created by HyperCore
      <disk>.qcow2
      <vm>.xml
  no_folder/                  clusters that are in no folder
```

The tenant directory is created over SMB when missing (NFS: by the mount). Because the layout
mirrors the folders, you can set per-tenant permissions on the share itself, and an import can
only pick up exports from the cluster's own folder.

### What users do with it

On the **Exports** tab for a cluster they can reach:

- **Schedules.** Pick a VM, copies to keep, daily run time (in `TZ`) and optional compression.
  The destination is shown as `Share/Folder/vm_timestamp` and follows from the folder; it cannot
  be typed in. **Run now** starts an export immediately.
- **VMs.** Every VM on the cluster with its schedule state.
- **Import.** Browse the folder's directory on the share, pick an export, optionally rename, and
  import into the selected cluster. A VM name that already exists is refused.
- **Runs.** Export and import history with live progress from HyperCore. A run interrupted by a
  container restart is re-attached by its task tag.

Retention runs after each successful export: copies of that VM beyond the retention count are
deleted, oldest first; other VMs' directories are never touched.

---

## Account

Everyone has an **Account** page in the rail footer: change the own password, enable or disable
TOTP two-factor (scan the QR code, confirm with a code), and see the language selector in the
header. Administrators use the same page for their own account.
