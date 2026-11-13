# Administration guide

Day-to-day administration of a HyperCore Manager installation: registering clusters, grouping
them into folders, giving people access, and setting up targets for the Backup module. Everything
here happens in the dashboard; nothing requires a restart or an `.env` change.

Who may do what is set per person and per place: a **role** (a set of permissions) on a part of
the **tenant tree** (MSP > customer > folder > cluster, down to VMs with a tag or a single VM). See
**People and access** below. When authentication is disabled (`SC_DASHBOARD_USER` left blank),
everyone may do everything.

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

**HyperCore API.** Every cluster has a switch: *auto* (default), *v1 only* or *v2*. The
connection test and the collector's regular contact probe whether the cluster answers on REST v2
(`/rest/v2/TaskTag`, available from HyperCore 9.7.8 as an experimental API) and record the result
next to the HyperCore version in the clusters table. With v2 in use the collector keeps the
cluster's task feed, which unlocks three things: a **Cluster activity** panel at the top of
Infraviewer (running tasks with progress, recently finished ones), a migration log that also
shows moves the balancer did not make (engine *external*: another balancer, an administrator,
Scale's own rebalancing), and an **interlock** that makes the balancer hold a migration while a
move runs anywhere on the cluster or the VM is in a snapshot, clone, export, import or
replication. Everything else keeps using v1. Choose *v1 only* to switch all of this off for a
cluster; choose *v2* to force it on a cluster the probe did not recognise.

Passwords are stored encrypted with `HCM_SECRET_KEY`. If the dashboard reports that the key is
not set, add it to `.env` and restart the stack before registering anything.

### What the HyperCore account needs

| Use | Rights |
|---|---|
| Monitoring, Foresight, dry-run balancing | read-only |
| Live balancing | VM live-migration |
| Backups and imports | Backup, Media Management, VM Create/Edit, VM Delete, VM Power Controls (a read-only account gets a clear 403 on the first run) |

Each service holds one login session per cluster account and reuses it: the session is renewed a
few times a day and after a 401, and closed when the service stops. HyperCore limits the number
of logins per account per hour, so an account shared with other tools that log in per request can
still hit that limit; give HyperCore Manager its own account where possible.

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

## Tenants and folders

Clusters are arranged in a tree: **MSP > customer > folder > cluster**. A cluster is in at most one
folder; clusters in no folder appear under **No folder**, which only administrators of the whole
installation see. A single-company installation has one MSP and one customer, created by the
upgrade and hidden in the interface: it shows folders only, as before. Adding a second customer or
MSP (**Settings → Access → Tenants**) shows the levels.

- **New folder** is at the bottom of the rail (for whoever manages the tree at customer level or
  above). Folders can be renamed, deleted (their clusters stay registered and fall back to **No
  folder**), collapsed individually, or all at once from the rail header.
- The **cluster menu** ("…" on a cluster) moves it to another folder of the same customer; moving
  it to another customer or MSP is for administrators of the whole installation.
- The **folder menu** ("…" on a folder) holds **Access…**, **Backup target**, **Rename** and
  **Delete folder**.
- **Settings → Access → Tenants** is a column browser, like a file browser: MSPs in the first
  column, a selected MSP's customers in the next, then a customer's folders, then a folder's
  clusters. The wide pane on the right shows the selected layer: the users and groups assigned on
  it (for a cluster also its tag and single-VM assignments), with **+ Assign** and a remove button,
  and below them, dimmed, what it inherits from the layers above. Each column adds a customer,
  folder or MSP at its foot; the pane renames and deletes the selected node. A node can only be
  deleted from above it, and only when it is empty; deleting removes its roles, groups and access.
  Accounts stay: they only lose the access they had there.
- Nobody sees the layers above their own. A customer's administrators and users do not see the MSP
  that serves them: not in the tree, the sidebar, labels or "who has access", and the accounts above
  them (the MSP's staff, the installation's administrators) are not listed. In the audit log, the
  guest console list and the console log their names show as "an administrator above your level",
  and the console log leaves out their address. Roles defined higher up can still be handed
  out below; only where they were defined is not shown.
- Users below folder level (engineers, application managers, viewers) and folder administrators
  see their clusters as one list in the rail, without folders.

---

## People and access

**Settings → Access** has a tab per subject: **Users**, **Groups**, **Roles**, **Tenants**,
**Guest console** and **Audit**. Each shows only the part of the tree you manage.

### Roles and permissions

A role is a set of permissions. Five are built in, as read-only templates:

| Role | What it may do |
|---|---|
| Admin | Everything at its scope: VMs, backups, settings, and the users, groups, roles and tree below it |
| Engineer | Full VM control: console, power, edit, snapshot, clone, create, delete; sees backups |
| App Manager | Sees VMs and their details and opens their console; changes nothing |
| Backup Manager | Every backup action: schedules, runs, file browse and download, file and full restores, verification |
| Viewer | Sees clusters, VMs and backups; changes nothing |

**Copy** a built-in role to make your own, at the level that should be able to use it (a role
defined at a customer is usable within that customer only). The editor groups the permissions by
area; ticking one ticks what it needs, and permissions you do not hold yourself are greyed out.
Editing a role changes everyone who has it at once (it is logged); a role still assigned cannot be
deleted. When a new version brings new permissions, the built-in roles get them, your own roles do
not: a banner on the Access panel names them until a role manager acknowledges it.

Each permission has a level, which decides where it can be given:

- **VM**: down to VMs with a tag, or a single VM (console, power, edit, the backup actions, ...).
- **Cluster**: needs the whole cluster (the overview, cluster settings, creating VMs, proxies, ...).
  A tag or VM assignment never carries them: a user with only some VMs sees the VM list and those
  VMs' details, not the nodes, the balancer, Foresight or the cluster events.
- **Tree**: users, groups, roles, the tree itself, backup targets, adding clusters.
- **Installation**: the updater and the default settings; only on the whole installation.

### Users

An account is a person's **email address**; it logs in with that address, in any capitals. Only
the initial administrator (`SC_DASHBOARD_USER`) keeps a plain username. Two John Smiths are
`jsmith@contoso.com` and `jsmith@mymail.com`: two accounts.

An account belongs to the person, not to a tenant. Tenants hold the access that person has with
them: an outside consultant can have access in two customers and sees both, side by side, without
anything above them. You manage someone when they hold access in your part of the tree, and you
see only that access, never what they hold elsewhere.

**Add user** asks for the email address, an optional full name, a password and the account's
**first access** (a role and where it applies). An address that already has an account is refused
without saying where it is used. **Access…** on a user opens its assignments:

- **Give access**: a role on a scope: the whole installation, an MSP, customer or folder, a
  cluster, the VMs with a tag on a cluster, or one VM.
- Group memberships of your part of the tree.
- **Remove from my tenants**: removes all access the person has in your part of the tree; the
  account, and access they have elsewhere, stay.
- Administrators of the whole installation also see **Disable**/**Enable**, **Reset password**,
  **Delete** (the address must be typed) and can change the address. Nobody below them touches an
  account's password, two-factor or address: an account can have access in other tenants too.

Rules that always hold:

- **No escalation**: you can only give (or take away) a role whose every permission you hold on
  that scope yourself, and only on scopes inside your own.
- **Nobody changes their own access**; another administrator can.
- The last enabled administrator of the whole installation cannot lose that role.
- One account per email address across the installation.

Two-factor (TOTP) is set up by each user on their own **Account** page.

The initial administrator comes from `SC_DASHBOARD_USER` / `SC_DASHBOARD_PASSWORD` in `.env` and
is seeded only once; manage accounts here afterwards.

### Groups

A group gives its access to every member, and is defined at a level of the tree like a role.
Adding someone to a group follows the same no-escalation rule as giving them the group's access
directly. (Groups are also where an identity provider's groups will map later.)

### Tags hand out access

An assignment on "the VMs with tag `erp` on cluster X" covers whatever VM carries that tag, now or
later. So adding or removing a tag that an assignment depends on, in a VM edit, a clone or a new
VM, needs **users.manage** on the cluster; the VM edit window marks those tags. Tags changed
directly in HyperCore change access within a minute. **Who has access** on a VM's details lists
everyone who can reach it and why.

A restored or cloned VM is a new VM: access given to the original VM alone does not follow it.
**Who has access → Copy access from another VM…** gives the same people the same access on the
new one.

### Audit

Every change to users, groups, roles, assignments and the tree, and every guest console event, is
in **Settings → Access → Audit**, narrowed to your part of the tree.

### After upgrading to roles

The upgrade makes every administrator **Admin** on the whole installation. Every other account is
**disabled** until an administrator gives it access: at the first login an administrator gets a
dialog listing those accounts (suggested: Viewer on the folders they were a member of). It cannot
be closed while one is left; **Assign** enables the account, **Delete** removes it. A user who
signs in before that, with the right password, is told their account is waiting.

Accounts are email addresses since this version. An account from before without an address (every
one except the initial administrator) stays disabled until an administrator of the whole
installation opens it and enters its address together with a new password; the old password does
not carry over. The user list marks these accounts "needs an email address".

### Mail

**Settings → Mail** (administrators of the whole installation) sets the one SMTP server HCM sends
through: invitations, password resets and address changes.

- Plain SMTP to a relay or a mail service: STARTTLS (587), TLS (465), or none (25) for a relay on
  your own network only; an optional login. The password is stored encrypted with
  `HCM_SECRET_KEY`, like the cluster passwords, and never shown again.
- **Address in links**: the address people use to reach the dashboard, put in the links of
  invitations and resets. Empty: the address of the browser that sends the invitation.
- **Send test mail** sends one and shows the server's answer when it fails. Every test, every
  failed mail and every change to these settings is in the access audit.
- Microsoft 365 / Exchange Online: use a connector or relay (SMTP AUTH with basic authentication is
  being retired by Microsoft). Sending through Microsoft Graph is a later wish.
- Without a mail server everything still works: whoever sends an invitation gets the link to pass
  on.

---

## Guest console access

One-time console access for someone outside, for example an engineer of a telephony supplier
installing a SIP server on one VM: they get that VM's console within a time window, and nothing
else: no VM name, no cluster, no other page.

- **Settings → Access → Guest console → Give outside access** (or **Outside access…** on a VM's
  details): the VM, the guest's name and company, the window, the length and number of sessions,
  and the title the guest sees. You get a **link** and a **6-digit code**, shown once. Give the
  link by e-mail and the code by phone or text message: both are needed.
- Someone who may only **ask** (for example an App Manager) sends a request; whoever may give
  outside access on that VM is told at login and approves or rejects it.
- The guest opens the link, enters the code and gets the console. Five wrong codes lock the grant
  (**New code** issues a new link and code). Redeeming binds the access to that one browser.
- A guest only connects onto a console that shows a **login or lock screen**, so they never land
  in someone's open session; they log in with credentials you give them. Tick **Also connect when
  the console is logged in** to allow it anyway.
- While the guest is connected: **Watch** shows the console read-only (your keyboard and mouse do
  not reach the VM), **End session** closes it, **Revoke** ends the access altogether.
- The guest controls the VM completely through its console (keyboard, reboot keys, boot menus) and
  can reach whatever the VM can reach on the network.

Everything a guest needs lives under `/guest/`, so the reverse proxy can publish that path to the
internet and keep the rest of the dashboard internal; see **Installing and updating → Securing the
installation** in the manual.

---

## Targets and backups

The Backup module needs somewhere to write. Administrators define
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

A target must live outside the cluster it protects. A share served by a VM on the same cluster
is stored in the same Scribe pool as the VMs being backed up: it is a copy, not a backup, and
every MiB written to it is written twice more as that VM's replicas. Use a NAS, a file server
on other hardware or a share on another site; a share on another HyperCore cluster is fine.

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
blocks and writes them as a qcow2 chain onto the target. The [installing chapter](manual/08-installing-and-updating.md#the-backup-proxy)
says what the proxy container needs on its host.

**Settings → Targets → Backup proxies → Register proxy**: pick the cluster, give the proxy a
name, its URL (`http://proxy:8083` for the container in this stack, `https://<ip>:8083` for a
proxy VM; a self-signed certificate is pinned on registration) and the token from
`PROXY_API_TOKEN`. On registration the proxy is contacted and its VM on the cluster is detected
from its MAC addresses; the status column says **VM detected** when that worked. **Test
connection** repeats the probe. A paused proxy is not used for new runs.

**Deploy a proxy VM** (same panel) creates the proxy VM for you: pick the cluster, a name, VLAN,
sizes and DHCP or a static address, and the backup service uploads an Ubuntu cloud image to the
cluster once, creates the VM with a cloud-init document that installs Docker and the proxy
container, waits for its address and registers it with a freshly generated token. The list below
the form follows the deployment; a finished one shows up in the proxies table with its VM
detected. The installing chapter describes what the VM needs to reach and what to do when a deployment
fails halfway.

The cloud-init document carries the proxy's token, and HyperCore keeps it as a small ISO
attached to the VM. Once the proxy has registered, the deployment removes that ISO from the VM
(ejects it while the VM runs; the empty CD-ROM stays and is reused for ISO delivery). The daily
sweep repeats this for every registered proxy VM, so proxies deployed by earlier versions are
cleaned up too. A proxy VM built by hand that was given its token through cloud-init should get
the same treatment: eject the cloud-init CD-ROM once the container runs.

**Measure throughput** (per proxy) writes 512 MiB onto the cluster's backup target through each
path a backup can use and reports MiB/s per mode: `plain` (a flat file, the share's ceiling),
`nbd` (a qcow2 image through qemu-nbd with the proxy's current settings, what a backup does),
`nbd_writeback` (strict qcow2, every flush honoured), `nbd_lazy` (qcow2 lazy refcounts),
`nbd_unsafe` (no flushes until the image is closed), `nbd_lazy_unsafe` (both) and `nbd_read`
(the restore staging path). The source data is read from the proxy VM's own disk, evicted from
its cache first, so on HyperCore the figures include the Scribe read. It takes a few minutes and
leaves nothing behind. The defaults (`PROXY_QCOW2_LAZY_REFCOUNTS=true`, `PROXY_NBD_CACHE=unsafe`)
are the `nbd_lazy_unsafe` mode: on our lab's SMB share it wrote 55 MiB/s where the strict mode
wrote 37 and a plain file 45, because qemu no longer waits for an SMB flush per MiB. A finished
point is still synced to the share before its checksum is taken, and a point without its
manifest is never used. Set the variables to `false` / `writeback` in `.env` (or the proxy VM's
container environment) to let qemu honour every flush; the installing chapter lists the variables.

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

**Plan times are local to the cluster.** Every cluster reports its time zone (HyperCore's own
setting) and HyperCore Manager reads it on each connection test; Settings → Clusters shows it
next to the API version, and an administrator can type another IANA name there to override it.
A plan's *Daily at* is read in that zone, so 03:00 on a cluster in Indianapolis runs at 03:00
US/Eastern. Wherever a plan time is shown, the dashboard adds what that is in your own time
("03:00 US/Eastern · 09:00 your time"), and the plan form says so while you type.

**Restore points** (Backup tab) lists every chain of the cluster's VMs with its points. **Verify**
re-reads every file of a chain against the checksums written at backup time; it runs in the
background and the row shows how far it is. The same check runs every Sunday at 05:00 for every
active chain. A point whose files do not match is marked **corrupt** in the points list, with the
failing files in the tooltip, until a later check passes. **Delete data** removes a chain from
the target; this is the only action that deletes backups. Deleting a plan *retires* its chain:
no new points, retention keeps applying, restores stay possible.

### Restoring a VM

On the **Restore points** sub-tab, open a chain's points and press **Restore** on the point you
want. The form asks for the name of the restored VM, whether to keep the original MAC addresses
(by default the copy gets new ones; the original addresses are set only on request, and
HyperCore refuses to start a VM whose MAC address is in use by a running VM), whether to
connect the network interfaces, and whether to start the VM. A restore whose start was refused
is still complete: the VM exists and the entry under **Runs** says why it was not started.

**Verify the chain before restoring** is on by default: the proxy re-reads every file of the
chain up to the chosen point and compares it with the checksums written at backup time, before
anything is created on the cluster. A mismatch stops the restore with the failing files named
and marks the point corrupt; no VM is created. The check reads the chain once at the share's
read speed (a 50 GB chain takes about 15–20 minutes on a gigabit share), which is why it can be
skipped: the form shows when the point was last verified, and skipping is reasonable right after
a clean weekly check when time matters. The entry under **Runs** records whether the chain was
verified or the check was skipped.

The vTPM travels with the backup: its state is captured with every point and imported with the
restored VM, so keys sealed in it (BitLocker with a TPM protector, LUKS with systemd-cryptenroll)
unseal on the copy without a recovery key. Verified with a TPM-sealed LUKS key bound to PCR 7:
the restored VM reported the same endorsement key and PCR values and unlocked the volume.

What happens: a diskless definition (generated from the stored VM record, with the NVRAM and
vTPM state of the point) is imported in seconds; the proxy then writes each data disk into a
staging disk at full speed and HyperCore clones the staging disks onto the restored VM. The
restored VM keeps the original boot order, VLANs and tags. Nothing of the backup chain is
modified by a restore, and a restore never touches the original VM.

### Recovering single files

**Files** on a restore point mounts that point read-only on the backup proxy and opens a file
browser: the filesystems of the VM (EFI partition, boot, the root volume including LVM), then
directories and files with size and date. **Download** fetches one file; tick several files or
directories and **Download selection as zip** streams them as one archive. **Close** unmounts
the point; an idle mount is closed after 30 minutes. The backup is never modified: journals,
NTFS logs and LVM metadata are replayed on a copy-on-write layer that is discarded on close.

Supported filesystems: ext2/3/4, xfs, btrfs, FAT, exFAT, NTFS, and LVM volumes on any of them.
Encrypted volumes (LUKS, BitLocker) and Windows dynamic disks are not browsable.

Files reach the guest through the guest's own means (download, copy, or an ISO attached to the
VM); HyperCore Manager never writes into a running VM through the hypervisor.

**Deliver as ISO**: pick a VM in the file browser's toolbar and press *Deliver selection as
ISO*. The selected files and directories are packed into an ISO 9660 image (Rock Ridge and
Joliet, so Linux and Windows guests read the names), uploaded to the cluster and inserted into
the VM's CD-ROM drive, with their original directory structure (`disk1-p3-ubuntu-lv/etc/hosts`
and so on). Copy the files inside the guest, then press **Eject & remove ISO** on the entry under
Runs → Restores; that empties the drive and deletes the image from the cluster. The limit is
8 GiB per delivery.

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
