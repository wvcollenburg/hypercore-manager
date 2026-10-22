# HyperCore Manager — user manual

This manual explains what you see in the dashboard and what to do with it, one chapter per
module. It assumes the stack is installed and at least one cluster is registered; for
installation and upgrades see [DEPLOY.md](../../DEPLOY.md), for the administrator's tasks
(registering clusters, folders, users, backup targets and proxies) see
[administration.md](../administration.md).

| Chapter | What it covers |
|---|---|
| [Getting around](01-getting-around.md) | the cluster rail, folders, tabs, roles, language, the first-login notice |
| [Infraviewer](02-infraviewer.md) | nodes, drives, VMs and storage performance; the cluster activity panel |
| [Clusterbalancer](03-clusterbalancer.md) | the migration log, the balancer's settings, the Oracle's forecast log, placement tags |
| [Foresight](04-foresight.md) | capacity forecasts, right-sizing, exhaustion alerts, reports, what-if, data sharing |
| [Backup](05-backup.md) | plans, restore points, restoring a VM, recovering files, imports, runs |
| [Settings](06-settings.md) | clusters, tunables, users, targets and proxies (overview; details in the administration guide) |
| [Modules and Account](07-modules-and-account.md) | the data-module view and your own account |

**Two roles.** Administrators see every cluster and manage the installation. Users see the
clusters in the folders they are members of and operate them: settings, backup plans, restores.
Where a chapter says "administrators only", a user does not see that panel or button.

**Everything is per cluster.** The cluster selected in the rail on the left drives every tab.
The breadcrumb in the header tells you which folder, cluster and tab you are looking at.

Screenshots in this manual were taken on a three-node lab cluster; your numbers will differ.

The manual is also served by the dashboard itself: **Manual** in the rail under Account opens
it at `/manual`, so it is at hand on a site without internet access.
