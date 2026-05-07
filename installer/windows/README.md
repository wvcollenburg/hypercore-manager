# HyperCore Manager — Deploy Wizard (Windows)

Provision a fresh HyperCore VM with the full hypercore-manager stack
(InfluxDB, Postgres, Collector, Balancer, Dashboard, Sentinel) ready to
run. The wizard talks to the Scale Computing HyperCore REST API, uploads
an Ubuntu 24.04 cloud image, and injects cloud-init userData that does
the rest on first boot.

## What you'll need

- A HyperCore cluster (any node IP, plus admin credentials).
- One free DHCP lease on the VLAN you'll use (default is untagged).
- Internet access from the *Windows* host (to fetch the Ubuntu cloud
  image once — it's cached under `%LOCALAPPDATA%\HyperCoreDeploy\images`
  for subsequent runs).

The VM itself does **not** need internet during cloud-init: the
hypercore-manager source is shipped via the embedded tarball, and
package installs come from the Ubuntu cloud image's pre-configured
mirror — but apt does need network to install Docker. If you're on an
air-gapped cluster, look at building a custom cloud image instead.

## Easy mode (recommended)

Four inputs:

1. **HyperCore IP** — any node IP. The wizard wraps it as `https://<ip>/rest/v1`.
2. **Cluster username + password** — used to authenticate to the REST API.
3. **VM name** — also becomes the hostname.
4. **Linux user + password** — for the new VM. The user has passwordless
   sudo and is in the `docker` group. Generate a random password with
   the **Generate** button if you don't have one in mind.

Everything else (InfluxDB / Postgres / Sentinel / dashboard secrets) is
auto-generated. The dashboard's initial admin password is shown on the
review page — copy it before clicking Deploy.

## Advanced mode

Adds:

- **VM resources** — vCPUs (default 4), RAM (default 8 GB), disk size
  (default 80 GB), VLAN tag (default 0 = untagged).
- **.env editor** — the wizard renders the full `.env` file with all
  the values it would use. Edit anything you like before deploying;
  your edits win.

## What happens when you click Deploy

1. The repo is bundled into a tarball (`~2 MB` after gzip + 1 MiB padding).
2. Cloud-init `userData` is built — small (`~3 KB`), holding only the
   `.env` body, user/packages config, and a first-boot script.
3. The Ubuntu cloud image is uploaded to the cluster (cached locally
   after the first run).
4. The repo tarball is uploaded as a *second* VirtualDisk. This avoids
   HyperCore's API body-size limit, which would otherwise reject any
   `userData` carrying the repo inline.
5. A new `VirDomain` is created with the cloud-init data attached as
   a NoCloud datasource ISO. HyperCore builds the cidata ISO for us
   from `cloudInitData.userData/metaData`.
6. The boot disk (Ubuntu) and payload disk (repo) are both attached
   as VIRTIO_DISKs. Boot order is pinned to the Ubuntu disk.
7. A NIC is attached on the chosen VLAN.
8. The VM powers on. Cloud-init logs to `/var/log/cloud-init-output.log`
   inside the VM. The first-boot script scans block devices for the
   gzip magic, identifies the payload disk, and `tar -xzf`s it into
   `/opt/hypercore-manager/`.

Bootstrap takes 3–6 minutes depending on bandwidth. Once it finishes,
the dashboard is reachable at `http://<vm-ip>:5000` with the dashboard
admin password from the review page.

## Re-deploying

Re-running the wizard against the same cluster is fine — give each VM
a unique name. The Ubuntu image is uploaded once per VM (HyperCore
clones the disk on attach), but the local download stays cached.

## Caveats

- **Linux password is in cloud-init logs.** Cloud-init writes
  `/var/log/cloud-init.log` inside the VM with the password redacted in
  the `chpasswd` directive but other modules may echo it. If you care,
  rotate the password after first login: `passwd`.
- **Self-signed TLS.** The wizard defaults to *not* verifying the
  cluster certificate (most HyperCore installs use self-signed). Tick
  "Verify TLS" only if you've installed a CA-signed cert on the cluster.
- **No DHCP fallback.** The VM relies on DHCP from the chosen VLAN. If
  your network requires static config, edit `.env` (advanced mode) plus
  the Ubuntu cloud-init `network-config` — currently we don't expose
  that knob.
- **Dashboard password.** Auto-generated in easy mode. Shown only on
  the review page — copy it then. It's also written into `.env` on the
  VM, so `cat /opt/hypercore-manager/.env` recovers it.
