# Installing and updating

Everything about the installation itself: what it needs, how to install and update it, what
`.env` controls, how to secure and back it up, and what to check when something is off. What
you do *in* the dashboard afterwards (clusters, folders, users, targets, proxies) is in the
[administration guide](../administration.md).

## What you need

**A host.** A Linux host or VM with Docker Engine 24 or later and Docker Compose v2
(`docker compose`, no hyphen; the old python `docker-compose` is not supported) and outbound
HTTPS to the management address of every cluster. Two vCPU and 4 GB of RAM carry the core
stack; Foresight wants more when it analyses many VMs. A laptop with Docker Desktop is fine for
a trial, Apple Silicon included.

The CPU and memory limits in `docker-compose.yaml` are tuned for a 2-core shared host; the
comments at the top of that file explain them. On a dedicated VM with four or more cores remove
the `cpus:` limits from every service. InfluxDB in particular suffers from CPU quota
throttling: its queries are bursty and multi-threaded, and hitting the quota turns sub-second
queries into waits of ten seconds and more.

**A HyperCore account** per cluster. Read and migration rights are enough for monitoring,
Foresight and the balancer. Backups need Backup, Media Management, VM Create/Edit, VM Delete
and VM Power Controls as well. The administration guide lists what each right is used for.

**The services.** The stack is a set of containers on that host; clusters are registered in
the dashboard once it is up, and each service runs one worker per cluster.

| Service | Port | Purpose |
|---|---|---|
| `influxdb` | 8086 | Time-series metrics store |
| `postgres` | internal | Configuration, recommendations, audit, backup catalogue |
| `collector` | — | Polls every cluster every 30 s, writes to InfluxDB |
| `balancer` | — | Reactive and predictive VM migration engine |
| `dashboard` | 5000 | The web UI, this manual, the updater |
| `sentinel` | 8081 | Foresight capacity analysis and its REST API |
| `backup` | 8082 | Backup and import scheduler and its REST API |
| `proxy` | internal | Backup proxy: reads snapshot clones, writes backup chains (privileged) |

There are no Compose profiles. A module you do not want is stopped (`docker compose stop
sentinel`, `docker compose stop backup proxy`) and the dashboard hides its tab.

## Installing

Two ways to write `.env`; the rest is the same.

**With the installer.** `install.py` is an interactive, text-mode installer that walks you
through every setting: what it does, why the default is what it is, when to change it. Easy
mode asks only for the dashboard password and your time zone, picked from a list (UTC, or a
region and then a city, with a filter as you type), and generates every secret; advanced mode
shows them all, with every module on by default. It warns when the wrong Compose is on the host and ends with the commands to start the
stack. Run it again later and it finds the existing `.env` and asks whether to start from
its values: yes for an upgrade of a running installation, no for a fresh install after
`docker compose down -v`, which ignores the file and starts blank.

```
git clone <repository>
cd hypercore-manager
python3 install.py
```

**By hand.** Copy `.env.template` to `.env` and replace every `CHANGE_ME`. Each secret is any
long random string:

```
python3 -c "import secrets; print(secrets.token_hex(32))"
```

| Value | Used for |
|---|---|
| `HCM_SECRET_KEY` | Encrypts cluster and share passwords in PostgreSQL. **Back it up with the database.** |
| `SC_DASHBOARD_SECRET_KEY` | Signs browser sessions |
| `SC_DASHBOARD_PASSWORD` | The initial admin login (seeded once; manage users in the dashboard afterwards) |
| `INFLUX_TOKEN`, `DOCKER_INFLUXDB_INIT_PASSWORD` | InfluxDB |
| `SENTINEL_PG_PASSWORD` | PostgreSQL, shared by every service |
| `SENTINEL_API_TOKEN` | Dashboard to Sentinel API |
| `BACKUP_API_TOKEN` | Dashboard to backup API |
| `PROXY_API_TOKEN` | Backup service to the proxy container(s) |

Cluster addresses and credentials do not go into `.env`; they are entered in the dashboard.

**Start it:**

```
docker compose up --build -d
docker compose ps
```

A warning that Compose "is configured to build using Bake, but buildx isn't installed" is
harmless; install the `docker-buildx-plugin` package to make it go away. A warning that a
variable "is not set" names a `$` inside an `.env` value: Compose reads `$NAME` as a variable
there, so a password with a dollar sign reaches the containers with a piece cut out. The
installer never generates one and refuses a typed one; in a hand-written file, write `$$`.

Every service should show `healthy` or `running` within a minute; one that keeps restarting
is explained by `docker compose logs <service> --tail=50`, usually a missing `.env` value or a
database that is not healthy yet.

**First login.** Open `http://<host>:5000` and log in with `SC_DASHBOARD_USER` and
`SC_DASHBOARD_PASSWORD`. The first screen asks for your cluster: the management address, the
HyperCore account and whether to verify its certificate. Use **Test connection** first. Within
a minute the collector writes metrics and the first charts appear; the balancer starts in
**dry-run**, logging what it would do, and needs a few minutes of samples before it has an
opinion. Switch dry-run off for that cluster in Clusterbalancer → settings once you have
checked its tunables. Foresight's tab fills after its first analysis cycle, an hour after start
by default. Further clusters are added under Settings → Clusters; no restart is needed.

## Backups: what the installation needs

The backup tab appears as soon as the `backup` container is healthy. An administrator then adds
a target and assigns it to folders, both in Settings → Targets and described in the
administration guide. Two things are the installation's business rather than the dashboard's:

- **NFS retention pruning** needs the share mounted into the backup container. Add a
  `docker-compose.override.yml` with `services: backup: volumes:
  ["/mnt/nas/hypercore:/mnt/nas/hypercore"]` and set that path as the target's prune path.
  SMB needs no mount.
- **Incremental backups** need a backup proxy, below. Without one, plans use HyperCore's native
  export.

## The backup proxy

The `proxy` container does the data work of incremental backups: HyperCore attaches a
read-only clone of a VM snapshot to the VM the proxy runs on, the proxy reads only the changed
blocks and writes qcow2 chains onto the backup target. For that it runs **privileged**, with
the host's `/dev` (hot-added disks, `/dev/nbd*`) and a read-only view of `/sys` (its own MAC
addresses, so the backup service can find the proxy's VM on the cluster). It never receives
cluster credentials; target credentials arrive per job and are not stored.

The host that runs the proxy container must:

- be a VM **on the HyperCore cluster** it serves, either the stack VM itself or a small
  dedicated proxy VM per remote cluster, because clones can only be attached within a cluster;
- load the `nbd` kernel module without partition scanning:
  `echo "options nbd max_part=0" > /etc/modprobe.d/nbd.conf && modprobe nbd`;
- if it boots from LVM, restrict LVM scanning to its own disk, because clones of other VMs can
  carry identical volume group names: in `/etc/lvm/lvm.conf`,
  `devices { global_filter = [ "a|^/dev/vda|", "r|.*|" ] }`;
- **not** be on a HyperCore snapshot schedule: a VM with read-only clones attached cannot be
  snapshotted, so scheduled snapshots would fail while a backup runs;
- leave `/dev/nbd*` to the proxy. At start the container disconnects every nbd slot and removes
  leftover file-recovery devices (`hcmflr-*`) of a previous container life; a slot the kernel
  left broken after an unclean stop is detected on first use and retired until the container
  restarts.

Registering a proxy, deploying one from the dashboard and measuring its throughput are in the
administration guide. Two installation matters remain here.

**A proxy VM built by hand.** For a cluster that does not host the stack VM, run the same
container in a small VM on that cluster: Ubuntu 24.04 or later, 2 vCPU, 2 GB RAM, 20 GB disk,
Docker, and the two host settings above. Then:

```
docker run -d --name hcm-proxy --restart unless-stopped --privileged --memory 1g -p 8083:8083 \
  -v /dev:/dev -v /sys:/host/sys:ro -v hcm-proxy-data:/var/lib/hcm-proxy \
  -e PROXY_API_TOKEN=<token> -e PROXY_HOST_NET=/host/sys/class/net -e PROXY_TLS=auto -e TZ=<zone> \
  ghcr.io/<org>/hcm-proxy:<version>      # or build it: docker build -t hcm-proxy ./proxy
```

With `PROXY_TLS=auto` the proxy generates a self-signed certificate on first start and keeps it
in its volume; register it with `https://<vm ip>:8083` and the token, and the certificate's
fingerprint is pinned at that moment and checked on every later connection. For your own
certificate set `PROXY_TLS=cert` with `PROXY_TLS_CERT` and `PROXY_TLS_KEY` mounted into the
container and tick *verify with the system CA store* when registering. If you gave the VM its
token through cloud-init, eject that CD-ROM once the container runs; the deployed VMs get this
done for them.

**What a dashboard deployment needs.** The backup service downloads an Ubuntu cloud image
(24.04 by default; the URL can point at a local mirror), uploads it to the cluster once as a
virtual disk named `hcm-ubuntu-noble-cloudimg.img` and reuses it for later proxies. The backup
container only needs to reach that URL and the cluster. The new VM needs DHCP or a correct
static address on its VLAN and the Docker registry that holds the proxy image. The VM pulls
the image without credentials, so it must be public; the deployment checks that with the
registry before it creates anything and refuses a private or missing image outright. On a
site without registry access, point *Proxy image tarball URL* at a `docker save` archive on a
web server the VM can reach, with the image name in the form matching the tag inside the
archive. The default sizes are 2 vCPU, 2 GiB and 20 GiB; the second core keeps the SMB client and the
data path off one core. The whole thing takes five to ten minutes.

Two things to know when it goes wrong. Cloud-init installs its packages on the first boot
only, so a VM that comes up on a VLAN without DHCP, or with a wrong static address, never gets
the guest agent or Docker and will not recover by being moved later: delete it and deploy again
on the right VLAN. And when the backup service runs behind a VPN on a Docker Desktop host, the
image upload can stall and be closed by the node after about a minute while the API itself
works; upload the image through the HyperCore UI under the exact name above and the deployment
finds and uses it. A Linux Docker host does not have this problem. A VM that comes up but
never answers on port 8083 did not start its container: `/var/log/hcm-proxy-start.log` on
the VM holds the pull and start output, and `/usr/local/sbin/hcm-proxy-start.sh` runs the
same steps again once the cause is fixed. A deployment that fails after the VM was created
leaves the VM in place for inspection; delete it in HyperCore or
register it by hand once it is up.

**Several proxies per cluster.** Register more than one and the backup service spreads jobs
over them, least recently used first, one job per proxy at a time. Each proxy VM reads through
its own node's network interface, so proxies on different nodes add bandwidth. Keep them apart
with the balancer's anti-affinity tags: tag each proxy VM `anti_<name of the other proxy VM>`
in HyperCore and the Clusterbalancer keeps them on separate nodes.

## Settings in `.env`

`.env.template` documents every variable; these are the ones that matter when installing.
A change to `.env` takes effect when the service is restarted:
`docker compose up -d --no-deps <service>`, plus any service that connects to it when a token
or password changed.

**Secrets and time.** `HCM_SECRET_KEY` (see above) and `TZ`, the IANA time zone for log
timestamps and the default of scheduled jobs (`UTC` by default). Each cluster carries its own
time zone, read from the cluster; backup plans are scheduled in that one.

**InfluxDB.**

| Variable | Description |
|---|---|
| `DOCKER_INFLUXDB_INIT_USERNAME`, `DOCKER_INFLUXDB_INIT_PASSWORD` | Admin account, first run only |
| `INFLUX_TOKEN` | Long-lived API token used by every service |
| `INFLUX_ORG`, `INFLUX_BUCKET` | Organisation (`hypercore`) and primary bucket (`metrics`) |
| `INFLUX_PRIMARY_RETENTION` | Days the primary bucket keeps raw points (365) |
| `INFLUX_LONGTERM_BUCKET`, `INFLUX_LONGTERM_RETENTION` | Downsampled bucket and its retention in days (1825) |
| `INFLUXD_QUERY_MAX_MEMORY_BYTES`, `INFLUXD_QUERY_MEMORY_BYTES`, `INFLUXD_QUERY_INITIAL_MEMORY_BYTES`, `INFLUXD_QUERY_CONCURRENCY`, `INFLUXD_STORAGE_CACHE_MAX_MEMORY_SIZE` | InfluxDB's own memory caps: 384 MB for all queries together, 256 MB per query, 192 MB write cache, set below the container's 768 MB so a heavy query is refused with an error instead of Docker killing the database. Raise them together with the container limit on a big installation |

**Balancer, collector and dashboard tunables** are not in `.env`. Dry-run, thresholds,
cooldowns, the Oracle, the poll intervals and the stale threshold ship with defaults and are
edited per cluster in the dashboard under Settings → Tunables; the **Defaults** template there
is what a newly registered cluster starts with, and "apply to all clusters" pushes a value
everywhere. Nothing you set there is touched by a restart or an update.

**Dashboard.** `SC_DASHBOARD_USER` and `SC_DASHBOARD_PASSWORD` seed the admin account.

**Foresight.**

| Variable | Description |
|---|---|
| `SENTINEL_PG_PASSWORD` | The PostgreSQL password, shared by every service |
| `SENTINEL_API_TOKEN` | Bearer token for the Sentinel REST API. **Empty = no authentication** |
| `SENTINEL_ANALYSIS_WINDOW_DAYS` | Days of history used for right-sizing (14) |
| `SENTINEL_RETENTION_MONTHS` | How long recommendations stay in PostgreSQL (12) |
| `SENTINEL_WEBHOOK_URL`, `SENTINEL_WEBHOOK_SECRET` | Outbound alert POSTs and their HMAC-SHA256 signing secret; empty URL = off |

The forecast toggles, thresholds and outcome tracking are in `.env.template`.

**Backup.**

| Variable | Description |
|---|---|
| `BACKUP_API_TOKEN` | Bearer token between dashboard and backup container. **Empty = no authentication** |
| `BACKUP_TASK_TIMEOUT_HOURS` | Give up on a HyperCore export or import task after this long (8) |
| `BACKUP_POLL_INTERVAL_SECONDS` | Task progress polling interval (15) |
| `BACKUP_DEFAULT_RETENTION`, `BACKUP_DEFAULT_RUN_TIME` | Pre-filled copies to keep (7) and daily run time (03:00) for new plans |
| `BACKUP_ANCHOR_RETAIN_DAYS` | HyperCore removes a plan's reference snapshot itself after this many days, 0 = never (60) |
| `BACKUP_PREFLIGHT_WAIT_HOURS` | How long a run waits for a cluster update or a missing node to clear before it is deferred (2) |
| `PROXY_API_TOKEN` | Bearer token between the backup service and the proxy container(s). **Empty = no authentication** |
| `PROXY_WRITERS` | Parallel writer threads per backup job in the proxy (4) |
| `PROXY_NBD_SLOTS` | `/dev/nbd` devices the proxy may use at once (8) |
| `PROXY_QCOW2_LAZY_REFCOUNTS` | `true` (default) skips the flush qemu issues per allocated qcow2 cluster when the proxy writes a point |
| `PROXY_NBD_CACHE` | qemu-nbd cache mode for backup writes: `unsafe` (default: no flushes until the image is closed; a finished point is synced to the share before its checksum is taken, an unfinished point is never valid), `writeback` (every flush honoured) or `none` (direct I/O). On our lab's SMB share: 55 MiB/s against 37, a plain file copy at 45 |

The last two are what **Measure throughput** in the proxies panel compares on your own target.
The `EXPORTS_*` names of the module's first release are still read when the `BACKUP_*` name is
not set.

## Securing the installation

Before the stack is reachable from anything but localhost, work through this list:

- [ ] Every `CHANGE_ME` replaced with a strong random secret, and `.env` outside version control
- [ ] `HCM_SECRET_KEY` set and backed up together with the PostgreSQL dump
- [ ] `SENTINEL_API_TOKEN`, `BACKUP_API_TOKEN` and `PROXY_API_TOKEN` set, not empty
- [ ] "Verify TLS" on for every cluster with a trusted certificate; self-signed ones documented
- [ ] Dry-run observation completed per cluster before its balancer goes live
- [ ] The dashboard (port 5000) behind a reverse proxy with TLS, never directly internet-facing
- [ ] InfluxDB (8086), Sentinel (8081) and the backup API (8082) not reachable beyond the host;
      the proxy API (8083) reachable from the backup service only
- [ ] Non-admin users only where needed, folder membership reviewed: users see every cluster in
      their folders

The dashboard does not terminate TLS itself. Put nginx or Caddy in front of it:

```nginx
server {
    listen 443 ssl;
    server_name hcm.your-domain.com;

    ssl_certificate     /etc/letsencrypt/live/hcm.your-domain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/hcm.your-domain.com/privkey.pem;

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
    server_name hcm.your-domain.com;
    return 301 https://$host$request_uri;
}
```

## Backing up the installation

PostgreSQL lives in the `postgres-data` Docker volume, InfluxDB in `influxdb-data`. A
PostgreSQL dump is only useful together with the `HCM_SECRET_KEY` from `.env`: cluster and
share passwords are stored encrypted with it. Back up `.env` alongside the dumps, and never
restore a database under a different key.

```
docker exec sc_postgres pg_dump -U sentinel hypercore_balancer > backup_pg_$(date +%Y%m%d).sql
docker exec sc_influxdb influx backup /tmp/influx-backup --token <INFLUX_TOKEN>
docker cp sc_influxdb:/tmp/influx-backup ./influx-backup-$(date +%Y%m%d)
```

Store both off the host and schedule the dump, for instance in `/etc/cron.d/hcm-backup`:

```
0 2 * * * root docker exec sc_postgres pg_dump -U sentinel hypercore_balancer \
  > /var/backups/hcm/pg_$(date +\%Y\%m\%d).sql 2>&1
```

To restore PostgreSQL:

```
docker compose stop balancer collector dashboard sentinel backup
docker exec -i sc_postgres psql -U sentinel hypercore_balancer < backup_pg_YYYYMMDD.sql
docker compose up -d
```

## Updating

**From the dashboard.** The header shows **Update** when a newer release is available. Apply it
and the stack pulls the release from the checkout's own git origin, rebuilds and restarts the
application services. InfluxDB and PostgreSQL keep running; expect a short outage of the
dashboard and the other services. Files the update touched are handed back to the owner of the
checkout, so a `git pull` by hand afterwards keeps working.

**By hand.** The same thing from a shell on the host:

```
git pull
docker compose up --build -d
```

**Database migrations** apply themselves. The dashboard runs the migration runner on every
start and again as part of the update: every `*.sql` in `postgres/init/` not yet recorded in
`public.schema_migrations` is executed and recorded. The scripts are idempotent, so the first
run against an existing installation is a no-op for everything already in place. If a script
fails, the runner stops at that file, leaves the earlier ones recorded, and resumes from it on
the next start or update once the SQL is fixed. To apply one by hand first, for instance on an
air-gapped site that inspects SQL before it runs:

```
docker exec -i sc_postgres psql -U sentinel hypercore_balancer < postgres/init/<file>.sql
```

The runner records it by name once it applies cleanly and skips it afterwards.

**Upgrading from 0.9.x** is a bigger step, because clusters moved from `.env` into the
dashboard:

1. Add `HCM_SECRET_KEY` to `.env`, and `BACKUP_API_TOKEN` and `PROXY_API_TOKEN` if you want
   backups. `SC_HOST`, `SC_USERNAME`, `SC_PASSWORD` and `SC_VERIFY_SSL` are ignored from now
   on and can be removed.
2. Pull, rebuild and start as above.
3. Log in. The onboarding form asks for your cluster: re-enter the address and account that
   were in `.env`. The collector, balancer and Sentinel pick it up within a minute, the
   balancer in dry-run; its tunables were copied from your `.env` values, so check them and
   switch it back to live.
4. Metrics written by 0.9.x carry no cluster tag and are not shown: Foresight and the balancer
   start from scratch after the upgrade, and the old points stay in InfluxDB until their
   retention expires. A fresh install instead of an in-place upgrade is therefore just as good;
   the only things an upgrade keeps are the user accounts and the tunables.
5. Schedules of the former standalone export manager are not migrated: create a target,
   assign it, and re-create the plans in the Backup tab.

## Logs and health

```
docker compose logs -f                     # every service
docker compose logs -f balancer            # one service
docker compose logs --tail=100 dashboard   # the last lines
```

Application logs are structured JSON; `docker compose logs sentinel | grep '{' | jq 'select(.level == "WARNING")'`
filters them. `docker compose ps` reports each service's health. For a closer look:

```
docker compose exec sentinel python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8080/health/detail').read().decode())"
docker compose exec backup   python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8082/health').read().decode())"
curl http://localhost:8086/health
docker exec sc_postgres pg_isready -U sentinel -d hypercore_balancer
curl -s -o /dev/null -w "%{http_code}" http://localhost:5000
```

The Sentinel REST API answers on 8081 with `SENTINEL_API_TOKEN` as bearer token, one cluster
at a time: `curl -H "Authorization: Bearer <token>" "http://localhost:8081/api/v1/summary?cluster=<cluster uuid>"`.

## Troubleshooting

**A container keeps restarting.** `docker compose logs <service> --tail=50`. Usually a missing
or wrong `.env` value, or PostgreSQL or InfluxDB not healthy yet; the dependency handles the
latter, wait a minute and look again.

**The balancer is not migrating VMs.** The rail badge says **dry run** or **live**; switch it
in Clusterbalancer → settings. Balancing only triggers when a node exceeds the CPU or RAM
thresholds, never to or from an excluded node, and not at all while the cluster is flagged as a
duplicate registration. The balancer log says `would migrate` or `skipping` and why.

**A cluster shows "duplicate" in the rail.** Two registry entries point at the same HyperCore
cluster, for instance two node addresses. Remove or disable one; the other resumes.

**The Backup tab is missing.** It appears only while the `backup` container is healthy and the
dashboard can reach it with `BACKUP_API_TOKEN`. Check `docker compose ps` and the dashboard log.

**A backup run fails with 403.** The HyperCore account registered for that cluster lacks the
rights listed under *What you need*. Grant them or register an account that has them.

**Foresight shows no data.** The tab fills after the first analysis cycle, up to an hour by
default, and forecasts need fourteen days of history from the moment the cluster was
registered; the banner above the tab shows how many days it has. Otherwise check
`docker compose ps sentinel`, its log, and that `SENTINEL_API_TOKEN` matches between `.env`
and the dashboard.

**A `.env` change does nothing.** Restart the service that reads it:
`docker compose up -d --no-deps <service>`.
