# HyperCore Manager — user manual

This manual explains what you see in the dashboard and what to do with it, one chapter per
module. The installing chapter covers the installation itself, from prerequisites to
troubleshooting; the administrator's tasks in the dashboard (registering clusters, folders,
users, backup targets and proxies) are in [administration.md](../administration.md).

| Chapter | What it covers |
|---|---|
| [Installing and updating](08-installing-and-updating.md) | prerequisites, the installer `install.py` or a hand-written `.env`, first login, the backup proxy's host, every `.env` setting, hardening, backing up the installation, updating and upgrading from 0.9.x, health checks, troubleshooting |
| [Connectivity](09-connectivity.md) | every connection between the HCM host, clusters, proxies, the backup share and browsers: direction, ports and protocols, for firewall planning |
| [Getting around](01-getting-around.md) | the cluster rail and its alert badges, folders, tabs, roles, language, the first-login notice |
| [Infraviewer](02-infraviewer.md) | nodes, drives, VMs and storage performance; the cluster activity panel |
| [Clusterbalancer](03-clusterbalancer.md) | the migration log, the balancer's settings, the Oracle's forecast log, placement tags |
| [Foresight](04-foresight.md) | capacity forecasts, right-sizing, exhaustion alerts, reports, what-if |
| [Backup](05-backup.md) | schedules, verification, test restores, plans, restore points, restoring a VM, recovering files, imports, runs |
| [Settings](06-settings.md) | clusters (in the tenant tree), tunables, access (users, groups, roles, tenants, guest console, audit), the open console session check and console log, mail, targets and proxies (overview; details in the administration guide) |
| [Modules and Account](07-modules-and-account.md) | the data-module view and your own account |

**Roles decide what you see.** Access is given per role and per place: the whole installation, an
MSP, a customer, a folder, a cluster, the VMs with a tag, or one VM (Settings → Access). You see the
clusters and VMs your roles reach, and only the panels and buttons your roles allow. Where a chapter
says "administrators only", it means someone holding that permission there; the built-in **Admin**
role holds all of them.

**A page shows state, a dialog changes it.** Adding or editing something (a target, a proxy, a
schedule, a test plan, a restore) opens a dialog over the page; Escape, ✕ or **Cancel** closes it
without saving.

**Everything is per cluster.** The cluster selected in the rail on the left drives every tab.
The breadcrumb in the header tells you which folder, cluster and tab you are looking at.

Screenshots in this manual were taken on a three-node lab cluster; your numbers will differ.

The manual is also served by the dashboard itself: **Manual** in the rail under Account opens
it at `/manual`, so it is at hand on a site without internet access.
