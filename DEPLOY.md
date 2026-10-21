# HyperCore Manager — Deployment Guide

## Overview

This stack provides automated VM load balancing, capacity intelligence and scheduled VM backups for
one or more Scale Computing HyperCore clusters. It runs as a set of Docker containers on a
management host (VM or bare-metal) with outbound network access to the HyperCore REST API of every
registered cluster. Clusters are registered in the dashboard after the stack is up; each service
runs one worker per cluster inside its container.

**Services:**

| Service | Started | Port | Purpose |
|---|---|---|---|
| `influxdb` | always-on | 8086 (internal) | Time-series metrics store |
| `postgres` | always-on | internal only | Config, recommendations, audit |
| `collector` | always-on | — | Polls HyperCore every 30s, writes to InfluxDB |
| `balancer` | always-on | — | Reactive + predictive VM migration engine |
| `dashboard` | always-on | 5000 | Web UI |
| `sentinel` | always-on | 8081 (internal) | Foresight capacity analysis + REST API |
| `backup` | always-on | 8082 (internal) | VM backup/import scheduler + REST API |
| `proxy` | always-on | 8083 (internal) | Backup proxy: reads snapshot clones, writes backup chains (privileged) |

---

## Prerequisites

- **Docker Engine 24+** and **Docker Compose v2** (`docker compose`, not `docker-compose`)
- A Linux host (or Linux VM) with outbound HTTPS access to the HyperCore cluster IP
- Minimum resources: 2 vCPU, 4 GB RAM (resource limits in `docker-compose.yaml` are tuned for
  a 2-CPU host; see the comments at the top of that file before changing them)
- The HyperCore management IP/hostname and a service account with read + migration permissions

---

## Quick Start

### 1. Clone and configure

```bash
git clone https://github.com/wvcollenburg/hypercore-manager.git
cd hypercore-manager
cp .env.template .env
```

Open `.env` and fill in every `CHANGE_ME` value. See the **Environment Variable Reference**
section below for details on each setting. Cluster addresses and credentials are entered in the
dashboard later, not here.

### 2. Generate secrets

```bash
`python3 install.py` generates every secret for you (easy mode asks only for the dashboard
password). By hand, run the generator once per value:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

| Value | Used for |
|---|---|
| `HCM_SECRET_KEY` | Encrypts cluster and share passwords in PostgreSQL. **Back it up with the database.** |
| `SC_DASHBOARD_SECRET_KEY` | Signs browser sessions |
| `SC_DASHBOARD_PASSWORD` | Initial admin login (seeded once; manage users in the dashboard afterwards) |
| `INFLUX_TOKEN`, `DOCKER_INFLUXDB_INIT_PASSWORD` | InfluxDB |
| `SENTINEL_PG_PASSWORD` | PostgreSQL (shared by every service) |
| `SENTINEL_API_TOKEN` | Dashboard ↔ Sentinel API |
| `BACKUP_API_TOKEN` | Dashboard ↔ Backup API |

### 3. Start the stack

```bash
docker compose up -d
```

This starts everything: influxdb, postgres, collector, balancer, dashboard, sentinel, backup and
the backup proxy. There are no Compose profiles any more; a module you do not want is
stopped (`docker compose stop sentinel`, `docker compose stop backup proxy`) and the dashboard
hides its tab. Note that the proxy container runs privileged with the host's `/dev`; it only does
something once a proxy is registered in the dashboard, see "The backup proxy" below.

### 4. Verify everything is up

```bash
docker compose ps
```

All services should show `healthy` or `running`. If a service is restarting, check its logs:

```bash
docker compose logs <service-name> --tail=50
```

### 5. Open the dashboard

Navigate to `http://<host-ip>:5000` and log in with `SC_DASHBOARD_USER` / `SC_DASHBOARD_PASSWORD`.

### 6. Register the cluster

The first login shows the cluster registration form: management address, HyperCore account,
TLS verification. Use **Test connection** first. Within a minute the collector writes metrics
and the balancer starts evaluating in dry-run. Add further clusters under Settings → Clusters;
no restart is needed. Folders, users and shares are described in
[docs/administration.md](docs/administration.md).

### 7. Confirm dry-run before going live

Every cluster starts in dry-run mode: the balancer logs what it *would* do but performs no
migrations. Once you have confirmed it is analysing the cluster correctly, switch **Dry run**
off for that cluster in the dashboard's Balancer Settings. No restart is needed.

---

## Backup

The `backup` and `proxy` containers start with the stack; the Backup tab appears in the
dashboard as soon as the backup container is healthy. Before users can
create a plan an administrator adds a target (Settings → Targets) and assigns it to the folders
whose clusters should back up there; clusters in no folder use the target flagged as their default.
Copies land in `<target>/<folder>/<vm>_<timestamp>/`. NFS retention pruning needs the share
mounted into the container (see the Backup variables below); SMB needs nothing extra.

---

## The backup proxy

The `proxy` container does the data work of incremental backups: HyperCore attaches a read-only
clone of a VM snapshot to the VM the proxy runs on, the proxy reads only the changed blocks and
writes qcow2 chains onto the backup target. For that it runs **privileged**, with the host's
`/dev` (hot-added disks, `/dev/nbd*`) and a read-only view of `/sys` (its own MAC addresses, so
the backup service can find the proxy's VM on the cluster). It never receives cluster
credentials; target credentials arrive per job and are not stored.

Requirements on the host that runs the proxy container:

- It must be a VM **on the HyperCore cluster** it serves (the stack VM itself, or a small
  dedicated proxy VM per remote cluster). Clones can only be attached within a cluster.
- Linux with the `nbd` kernel module loaded without partition scanning:
  `echo "options nbd max_part=0" > /etc/modprobe.d/nbd.conf && modprobe nbd`.
- If the host boots from LVM, restrict LVM scanning to its own disk (clones of other VMs can
  carry identical volume group names): in `/etc/lvm/lvm.conf`,
  `devices { global_filter = [ "a|^/dev/vda|", "r|.*|" ] }`.
- The proxy VM must **not** be on a HyperCore snapshot schedule: a VM with read-only clones
  attached cannot be snapshotted, so scheduled snapshots would fail while a backup runs.
- `PROXY_API_TOKEN` set in `.env`; the backup service presents it on every job.
- Nothing else on that host may use `/dev/nbd*`: at start the container disconnects every nbd
  slot and removes leftover file-recovery devices (`hcmflr-*`) of a previous container life. A
  slot the kernel left in a broken state after an unclean stop is detected on first use and
  retired until the container restarts.

The HyperCore account registered for the cluster needs the rights Backup, Media Management,
VM Create/Edit, VM Delete and VM Power Controls.

### A proxy VM for a remote cluster

For a cluster that does not host the stack VM (the MSP case), run the same container in a small
VM on that cluster: Ubuntu 24.04 or later, 1 vCPU, 2 GB RAM, 20 GB disk, Docker, and the two
host settings above (`nbd max_part=0`, LVM filter). Then:

```bash
docker run -d --name hcm-proxy --restart unless-stopped --privileged --memory 1g -p 8083:8083 \
  -v /dev:/dev -v /sys:/host/sys:ro -v hcm-proxy-data:/var/lib/hcm-proxy \
  -e PROXY_API_TOKEN=<token> -e PROXY_HOST_NET=/host/sys/class/net -e PROXY_TLS=auto -e TZ=<zone> \
  ghcr.io/<org>/hcm-proxy:<version>      # or build it: docker build -t hcm-proxy ./proxy
```

With `PROXY_TLS=auto` the proxy generates a self-signed certificate on first start and keeps it
in its volume. Register the proxy in the dashboard (Settings → Targets → Backup proxies) with
`https://<vm ip>:8083` and the token: the certificate's fingerprint is pinned at that moment and
checked on every later connection. To use your own certificate instead, set `PROXY_TLS=cert`
with `PROXY_TLS_CERT` and `PROXY_TLS_KEY` mounted into the container and tick *verify with the
system CA store* when registering.

### Deploying the proxy VM from the dashboard

Instead of building the VM by hand, an administrator can let the stack create it: **Settings →
Targets → Backup proxies → Deploy a proxy VM**. The backup service downloads an Ubuntu cloud image
(24.04 by default; the URL can be changed, for instance to a local mirror), uploads it to the
cluster once as a virtual disk and reuses it for later proxies, then creates a UEFI VM whose
cloud-init document installs Docker, the guest agent and the host settings above, writes a
fresh token to `/etc/hcm-proxy.env`, and starts the proxy container with `PROXY_TLS=auto`. Once
the VM reports an address (static from the form or via the guest agent) the proxy is registered,
probed until the container answers, and its certificate fingerprint pinned. Progress is shown in
the panel; the whole thing takes five to ten minutes.

What the form asks for: cluster, VM name, VLAN, sizes (1 vCPU, 2 GiB, 20 GiB by default), DHCP
or a static address, and optionally an SSH public key for the VM. The VM needs to reach the
Docker registry that holds the proxy image; on a site without registry access, point *Proxy
image tarball URL* at a `docker save` archive on a web server the VM can reach (the image name
in the form must then match the tag inside the archive). The backup container itself only needs
to reach the cloud image URL and the cluster.

The deploying account needs the same rights as for backups (VM Create/Edit, VM Delete, VM Power
Controls, Media Management). If a deployment ends in an error after the VM was created, the VM is
left in place for inspection; delete it in HyperCore or register it by hand once it is up. Pick
the VLAN with care: cloud-init installs its packages on the first boot only, so a VM that comes
up on a VLAN without DHCP (or with a wrong static address) never gets the guest agent or Docker
and will not recover by being moved later. Delete it and deploy again on the right VLAN.

### Several proxies per cluster

Register more than one proxy for a cluster and the backup service spreads jobs over them (least
recently used first; one job per proxy at a time). Each proxy VM reads through its own node's
network interface, so proxies on different nodes add bandwidth. Keep them apart with the
balancer's anti-affinity tags: tag each proxy VM `anti_<name of the other proxy VM>` in HyperCore
and the Clusterbalancer keeps them on separate nodes.

---

## Foresight (Sentinel)

Sentinel starts with the stack (`docker compose stop sentinel` runs without it).

Sentinel exposes a health endpoint (inside the container; `docker compose ps` shows it as
`healthy`) and a REST API on 8081:

```bash
# Health check
docker compose exec sentinel python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8080/health/detail').read().decode())"

# REST API (requires SENTINEL_API_TOKEN if set); one cluster at a time
curl -H "Authorization: Bearer <token>" "http://localhost:8081/api/v1/summary?cluster=<cluster uuid>"
```

The Foresight tab in the dashboard becomes active once Sentinel has completed its first
analysis cycle (default: 1 hour after startup).

---

## Environment Variable Reference

### Clusters and secrets

HyperCore clusters are **not** configured in `.env`. Register them in the dashboard under
Settings → Clusters after the stack is up (address, account, TLS verification per cluster).

| Variable | Required | Description |
|---|---|---|
| `HCM_SECRET_KEY` | Yes | Encrypts cluster (and share) passwords stored in PostgreSQL. Any long random string; `install.py` generates one. Back it up with the database. |
| `TZ` | No | IANA timezone for scheduled jobs and log timestamps (default `UTC`) |

### InfluxDB

| Variable | Description |
|---|---|
| `DOCKER_INFLUXDB_INIT_USERNAME` | InfluxDB admin username (first-run only) |
| `DOCKER_INFLUXDB_INIT_PASSWORD` | InfluxDB admin password (first-run only) |
| `INFLUX_TOKEN` | Long-lived API token used by all services |
| `INFLUX_ORG` | InfluxDB organisation name (default: `hypercore`) |
| `INFLUX_BUCKET` | Primary metrics bucket name (default: `metrics`) |
| `INFLUX_PRIMARY_RETENTION` | Retention for primary bucket in days (default: 365) |
| `INFLUX_LONGTERM_BUCKET` | Long-term downsampled bucket name |
| `INFLUX_LONGTERM_RETENTION` | Retention for long-term bucket in days (default: 1825 / 5yr) |

### Balancer

These values seed the **Defaults** template on first start. Afterwards they are edited per
cluster in the dashboard (Settings → Tunables), which also offers "apply to all clusters".
Changing them in `.env` later does not alter clusters that already exist.

| Variable | Description |
|---|---|
| `SC_DRY_RUN` | `true` = log only, no migrations. **Start here.** |
| `SC_VERBOSITY` | Log verbosity 1–5 (default: 3) |
| `SC_RAM_LIMIT_PERCENT` | Hard RAM ceiling per node (default: 85%) |
| `SC_CPU_UPPER_THRESHOLD_PERCENT` | CPU % that triggers reactive balancing (default: 50%) |
| `SC_PREDICTIVE_BALANCING_ENABLED` | Enable/disable The Oracle predictive engine |
| `SC_PREDICTIVE_INTERVAL_SECONDS` | How often The Oracle runs (default: 43200 / 12h) |
| `SC_EXCLUDE_NODE_IPS` | Comma-separated node IPs to never migrate to/from |

### Backup

| Variable | Description |
|---|---|
| `BACKUP_API_TOKEN` | Bearer token between dashboard and backup container. **Empty = auth disabled.** |
| `BACKUP_TASK_TIMEOUT_HOURS` | Give up on a HyperCore export/import task after this long (default 8) |
| `BACKUP_POLL_INTERVAL_SECONDS` | Task progress polling interval (default 15) |
| `BACKUP_DEFAULT_RETENTION` | Pre-filled copies-to-keep for new plans (default 7) |
| `BACKUP_DEFAULT_RUN_TIME` | Pre-filled daily run time for new plans (default 03:00, in `TZ`) |
| `PROXY_API_TOKEN` | Bearer token between the backup service and the proxy container(s). **Empty = auth disabled.** |
| `PROXY_WRITERS` | Parallel writer threads per backup job in the proxy (default 4) |
| `PROXY_NBD_SLOTS` | `/dev/nbd` devices the proxy may use at once (default 8) |

The `EXPORTS_*` names used by the first release of the module are still read when the `BACKUP_*`
name is not set.

NFS pruning needs the share mounted into the container; add a `docker-compose.override.yml`
with `services: backup: volumes: ["/mnt/nas/hypercore:/mnt/nas/hypercore"]` and set that path
as the target's prune path in Settings → Targets. SMB needs no mount.

### Dashboard

| Variable | Description |
|---|---|
| `SC_DASHBOARD_USER` | Web UI login username |
| `SC_DASHBOARD_PASSWORD` | Web UI login password |
| `SC_DASHBOARD_STALE_SECONDS` | Age (seconds) at which metric data is shown as stale |

### Foresight — Sentinel

| Variable | Description |
|---|---|
| `SENTINEL_PG_PASSWORD` | PostgreSQL password shared across all services |
| `SENTINEL_API_TOKEN` | Bearer token for Sentinel REST API. **Empty = auth disabled.** |
| `SENTINEL_ANALYSIS_WINDOW_DAYS` | Days of history used for right-sizing (default: 14) |
| `SENTINEL_RETENTION_MONTHS` | How long to keep recommendations in PostgreSQL (default: 12) |
| `SENTINEL_WEBHOOK_URL` | Target URL for outbound alert POSTs. Empty = disabled. |
| `SENTINEL_WEBHOOK_SECRET` | HMAC-SHA256 signing secret for webhook payloads |

See `.env.template` for the full list including forecast toggles, thresholds, and outcome tracking.

---

## Security Hardening Checklist

Before exposing the stack to any network beyond localhost, complete every item:

- [ ] All `CHANGE_ME` values replaced with strong random secrets
- [ ] "Verify TLS" enabled on every cluster with a trusted certificate (self-signed documented otherwise)
- [ ] `HCM_SECRET_KEY` set, backed up together with the PostgreSQL dump
- [ ] `SENTINEL_API_TOKEN` set to a strong random token (not empty)
- [ ] `SC_DASHBOARD_PASSWORD` set to a strong password
- [ ] Dry-run observation completed per cluster before switching that cluster's balancer to live (Clusterbalancer → settings)
- [ ] Dashboard (port 5000) not directly internet-facing — place behind a reverse proxy with TLS
- [ ] Non-admin users exist only where needed; folder membership reviewed (users see every cluster in their folders)
- [ ] InfluxDB (port 8086) not exposed beyond the host — internal Docker network only in production
- [ ] Sentinel API (port 8081) not exposed beyond the host — accessed via dashboard proxy only
- [ ] Backup API (port 8082) not exposed beyond the host; `BACKUP_API_TOKEN` set
- [ ] Proxy API (port 8083) reachable from the backup service only; `PROXY_API_TOKEN` set
- [ ] `.env` file is not committed to version control (`.gitignore` covers this)
- [ ] PostgreSQL has no host port binding (removed from `docker-compose.yaml`)

---

## Reverse Proxy (HTTPS)

The dashboard does not terminate TLS itself. For production place it behind nginx or Caddy.

### Nginx example

```nginx
server {
    listen 443 ssl;
    server_name balancer.your-domain.com;

    ssl_certificate     /etc/letsencrypt/live/balancer.your-domain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/balancer.your-domain.com/privkey.pem;

    location / {
        proxy_pass         http://127.0.0.1:5000;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;
    }
}

server {
    listen 80;
    server_name balancer.your-domain.com;
    return 301 https://$host$request_uri;
}
```

---

## Backup

PostgreSQL data is stored in the `postgres-data` Docker volume. InfluxDB data is in
`influxdb-data`. The PostgreSQL dump is only useful together with the `HCM_SECRET_KEY` from
`.env`: cluster and share passwords are stored encrypted with it. Back up `.env` alongside the
dumps, and never restore a database under a different key.

### PostgreSQL backup

```bash
docker exec sc_postgres \
  pg_dump -U sentinel hypercore_balancer \
  > backup_pg_$(date +%Y%m%d).sql
```

### InfluxDB backup

```bash
docker exec sc_influxdb \
  influx backup /tmp/influx-backup --token <INFLUX_TOKEN>
docker cp sc_influxdb:/tmp/influx-backup ./influx-backup-$(date +%Y%m%d)
```

Store both off-host (S3, SFTP, etc.). Schedule via cron:

```bash
# /etc/cron.d/sc-balancer-backup
0 2 * * * root docker exec sc_postgres pg_dump -U sentinel hypercore_balancer \
  > /var/backups/sc-balancer/pg_$(date +\%Y\%m\%d).sql 2>&1
```

### Restore PostgreSQL

```bash
docker compose stop balancer collector dashboard sentinel
docker exec -i sc_postgres psql -U sentinel hypercore_balancer < backup_pg_YYYYMMDD.sql
docker compose up -d
```

---

## Upgrading

### Upgrading from 0.9.x

The first release after 0.9.2 moves cluster configuration out of `.env` and supports several
clusters. Before you pull:

1. Add `HCM_SECRET_KEY` to `.env` (any long random string) and, if you want backups,
   `BACKUP_API_TOKEN`. `SC_HOST`, `SC_USERNAME`, `SC_PASSWORD` and `SC_VERIFY_SSL` are ignored
   from now on and can be removed.
2. Pull, rebuild and start as below. Migrations `019`–`024` apply on the first dashboard start.
3. Log in. The onboarding form asks for your cluster: re-enter the address and account that
   were in `.env`. The collector, balancer and Sentinel pick it up within a minute, the balancer
   **in dry-run**; switch it back to live once you have checked the settings, which were copied
   from your `.env` values into that cluster's tunables.
4. Metrics written by 0.9.x carry no cluster tag and are no longer shown. Forecasts and the
   balancer's averages therefore start from scratch; Foresight needs its usual 14 days of history
   again. The old data stays in InfluxDB until its retention expires.
5. If you ran the former standalone export manager, its schedules are not migrated: create a
   target, assign it, and re-create the plans from the Backup tab.

### 1. Pull latest code

```bash
git pull
```

### 2. Rebuild images

```bash
docker compose build
```

### 3. Apply database migrations

Migrations are now applied **automatically** by the dashboard container.

- The dashboard runs the migration runner ([dashboard/migrations.py](dashboard/migrations.py))
  in a background thread on every startup, and again as part of the Apply Update flow.
- Successfully-applied filenames are recorded in `public.schema_migrations`; only files
  not yet recorded get re-executed.
- On the first run after deploying the runner against an existing install, every `*.sql`
  in `postgres/init/` is "applied" — the scripts are required to be idempotent
  (`IF NOT EXISTS`, `DO $$ ... $$` guards, etc.) so reapplying ones that were already in
  place is a no-op. They're then recorded so subsequent runs only apply new files.

In practice this means a normal upgrade is just:

```bash
git pull
docker compose build
docker compose up -d
```

…or simply clicking **Apply Update** in the dashboard.

If you'd rather apply migrations manually (e.g. for an air-gapped install or to inspect
SQL before it runs), the old path still works:

```bash
docker exec -i sc_postgres psql -U sentinel hypercore_balancer \
  < postgres/init/018_component_reasons.sql
```

The runner records files by name once they apply cleanly, so a manual run followed by
the automatic one will skip the script the second time around.

**On failure:** if a migration errors, the runner stops at that file, leaves earlier ones
recorded, and returns. Fix the SQL and re-trigger Apply Update; it resumes from the
failed script.

### 4. Restart containers

```bash
docker compose up -d
```

---

## Viewing Logs

```bash
# All services
docker compose logs -f

# Single service
docker compose logs -f sentinel
docker compose logs -f balancer
docker compose logs -f collector

# Last 100 lines
docker compose logs --tail=100 dashboard
```

All application logs are structured JSON. Filter with `jq`:

```bash
docker compose logs sentinel | grep '{' | jq 'select(.level == "WARNING")'
```

---

## Health Checks

```bash
# Sentinel and Backup health (ports 8080/8082 inside the containers; compose reports healthy)
docker compose ps
docker compose exec sentinel python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8080/health/detail').read().decode())"
docker compose exec backup   python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8082/health').read().decode())"

# InfluxDB
curl http://localhost:8086/health

# PostgreSQL
docker exec sc_postgres pg_isready -U sentinel -d hypercore_balancer

# Dashboard
curl -s -o /dev/null -w "%{http_code}" http://localhost:5000
```

---

## Troubleshooting

### Container is restarting

```bash
docker compose logs <service> --tail=50
```

Common causes: missing or incorrect `.env` values, PostgreSQL or InfluxDB not yet healthy
(the healthcheck dependency handles this — wait 30–60 seconds and check again).

### Balancer is not migrating VMs

1. Confirm the cluster's balancer is not in dry-run (the rail badge says **dry run** or **live**;
   switch it in Clusterbalancer → settings for that cluster)
2. Check CPU/RAM thresholds — balancing only triggers when a node exceeds them
3. Check the excluded node IPs for that cluster — the target node may be excluded
4. Check the cluster is not flagged as a duplicate registration (the balancer suspends itself for
   the newer entry when the same HyperCore cluster is registered twice)
5. Review balancer logs for `would migrate` or `skipping` messages

### A cluster shows "duplicate" in the rail

Two registry entries point at the same HyperCore cluster (for example two node addresses). Remove
or disable one of them; the remaining entry resumes normal operation.

### Backup tab is missing

The tab appears only while the `backup` container is healthy and the dashboard can reach it
with `BACKUP_API_TOKEN`. Check `docker compose ps` and the dashboard log.

### Backup run fails with 403

The HyperCore account registered for that cluster lacks the required rights: Backup, Media
Management, VM Create/Edit, VM Delete and VM Power Controls. Grant them or register an account
that has them; read-only accounts are enough for monitoring and dry-run only.

### Sentinel shows no data in dashboard

1. Confirm sentinel is running: `docker compose ps sentinel`
2. Check sentinel logs for errors on first collection/analysis cycle
3. The Foresight tab populates after the first analysis cycle (up to 1 hour by default)
4. The banner above the tab shows how many days of history the selected cluster has; forecasts
   need 14 days from the moment the cluster was registered
5. Verify `SENTINEL_API_TOKEN` matches between `.env` and the dashboard's `SENTINEL_API_TOKEN`

### `.env` changes not taking effect

Environment variable changes require a container restart:

```bash
docker compose up -d --no-deps <service>
```

For token or password changes, restart the affected service plus any service that connects to it.
