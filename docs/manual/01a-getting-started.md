# Getting started

This chapter sets up an installation from scratch:
1. an MSP;
2. a customer;
3. a folder;
4. a cluster in that folder;
5. a user with access to all of it.

Each step names the button to click. The [administration guide](../administration.md) covers every
option in more detail.

You need an account that is **Admin on the whole installation**. On a new installation that is
the initial administrator from `.env` (`SC_DASHBOARD_USER`).

**Why the tree comes first.** In HyperCore Manager access is always given *on* something: an
MSP, a customer, a folder, a cluster, the VMs with a tag, or one VM. A new user starts with
their first access, so the place you give it on has to exist first. That is why this walkthrough
builds the tree first and adds the user last.

The example builds the MSP **Northwind IT**, the customer **Contoso**, the folder **Production**
and the cluster **Contoso-Prod**, then gives **jane@contoso.example** access to them. Use your own
names.

## 1. Open the tenant tree

Go to **Settings** (bottom of the rail) → **Access** → **Tenants**. The tree is shown as columns,
like a file browser: MSPs, then a selected MSP's customers, then a customer's folders, then a
folder's clusters. The pane on the right shows who has access to whatever you selected.

**One company or several?** A new installation starts as a single company: the MSP and customer
levels exist but are hidden, and you only see folders.
- **If you run HyperCore Manager for your own company only,** skip steps 2 and 3. Add folders with
  **+ Folder** in the folder column (or **New folder** at the bottom of the rail) and continue at
  step 4.
- **If you serve customers,** continue with step 2.

## 2. Add the MSP

Click **+ MSP** at the top of the Tenants tab and confirm. The MSP and customer levels now show
everywhere: in Access, in the rail and in paths. Enter the name, **Northwind IT**.

Folders and clusters that already existed stay where they were, under the customer **Default** in
the MSP **Default**. Rename those to your own company later (select it, then rename it in the pane),
or move their clusters into the new tree.

After the first MSP, add more with **+ MSP** at the foot of the MSP column.

## 3. Add the customer

Select **Northwind IT** in the first column. Its customers appear in the second column, which is
still empty. Click **+ Customer** at the foot of that column and enter **Contoso**.

## 4. Add the folder

Select **Contoso**. Click **+ Folder** at the foot of the folder column and enter **Production**.

A folder groups the clusters that belong together. It is also where the backup target is set (the
folder menu "⋯" in the rail → **Backup target**). If the customer has a DR cluster, give it a folder of
its own or put it in this one with the role **DR**; see the Backup chapter.

## 5. Add the cluster

Select **Production**. The last column lists its clusters, which is still empty. Click **+ Cluster**
at its foot and fill in:

| Field | What to enter |
|---|---|
| Name | **Contoso-Prod**; unique within the folder |
| Management address | Any node's IP address or host name; `https://` and `/rest/v1` are added for you |
| HyperCore user, Password | A HyperCore account; the administration guide lists the rights each feature needs |
| Verify TLS certificate | On if the cluster has a trusted certificate |

Click **Test connection**: it logs in and shows the cluster name, the number of nodes and the
HyperCore version. Then click **Add cluster**. Within a minute the cluster appears in the rail
in Production and the first charts fill. The balancer starts in **dry-run**: it only
logs what it would do. Switch dry-run off later in Clusterbalancer once you have checked its
settings.

The HCM host needs TCP 443 to every node of the cluster; see [Connectivity](09-connectivity.md).

**A cluster you registered at first login** may show under **No folder**. Select it in the tree,
click **Edit**, and pick the folder.

## 6. Add the user

Go to the **Users** tab and click **+ Invite**:

- **Email address**: jane@contoso.example. The email address is the login.
- **Full name**: optional.
- **Give**: a role and where it applies. For Jane, choose **Viewer** on **MSP, customer or
  folder** → **Customer: Northwind IT / Contoso**.

Jane gets a mail with a link. She confirms her address and chooses her own password; the link works
once, for 48 hours. Open invitations are listed under the users, with **Resend** and **Withdraw**.
Without a mail server (Settings → Mail) you get the link to pass on yourself.

You can also invite straight from the tree: select the layer and click **+ Invite** in the pane.

Administrators of the whole installation can use **+ Add user with password** instead, for a test
or service account. It asks for the same first access.

## 7. Give rights on the entities

Access given on a layer reaches everything below it. Jane's **Viewer** on Contoso already lets her
see every folder, cluster, VM and backup of Contoso. Now give her more where she needs it. In the
Tenants tab, select the layer and click **+ Assign** in the pane:

| Select | + Assign | Effect |
|---|---|---|
| Contoso | (already) Viewer | Sees everything of Contoso, changes nothing |
| Production | **Backup Manager** | Runs backups, restores and file recovery for every cluster in Production |
| Contoso-Prod | **Engineer** | Full VM control on that cluster: console, power, edit, snapshot, clone, create, delete |

Each layer's pane lists who is assigned there and, dimmed below, what is inherited from above, with
a link (↗) to the layer it comes from. Select **Contoso-Prod** now: Jane's Engineer shows as given
here, and her Backup Manager and Viewer show as inherited from Production and Contoso.

A cluster also offers **VMs with a tag** and **One VM** as the place to apply a role. Use them for
someone who should only reach some of the VMs (for example App Manager on the VMs tagged `erp`). Such
a person sees those VMs in the cluster's VM list, but not the nodes, the balancer or Foresight.

**To let Contoso manage its own people,** give someone **Admin** on Contoso. They can then invite
users, create folders, register clusters and assign roles inside Contoso, and they never see
Northwind IT or other customers. Nobody can give a right they do not hold themselves, and nobody
changes their own access.

**Check it.** **Access…** on Jane in the Users tab lists all her access in one place. When she logs
in, she sees Contoso's clusters and nothing else.

## Next steps

- **Backups:** add a backup target and assign it to the folder, then deploy a proxy for the cluster
  (Settings → Targets; see [Backup](05-backup.md) and the administration guide).
- **Mail:** configure a mail server under Settings → Mail, so invitations and alerts go out by
  themselves.
- **Your own roles:** **Copy** a built-in role under Access → Roles to make one with exactly the
  permissions you want.
- **Groups:** for several people with the same access, create a group on the layer, give the group
  the access, and add people to it.
