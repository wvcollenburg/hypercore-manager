# HyperCore Manager — Deployment Guide

## Overview

This stack provides automated VM load balancing and capacity intelligence for a Scale Computing
HyperCore cluster. It runs as a set of Docker containers on a management host (VM or bare-metal)
with outbound network access to the HyperCore REST API.

**Services:**

| Service | Profile | Port | Purpose |
|---|---|---|---|
| `influxdb` | always-on | 8086 (internal) | Time-series metrics store |
| `postgres` | always-on | internal only | Config, recommendations, audit |
| `collector` | always-on | — | Polls HyperCore every 30s, writes to InfluxDB |
| `balancer` | always-on | — | Reactive + predictive VM migration engine |
| `dashboard` | always-on | 5000 | Web UI |
| `sentinel` | `sentinel` / `full` | 8081 (internal) | Foresight capacity analysis + REST API |
| `grafana` | `tools` | 3000 | Optional metrics explorer |

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
# InfluxDB admin token
python3 -c "import secrets; print(secrets.token_hex(32))"
# Paste result as INFLUX_TOKEN and DOCKER_INFLUXDB_INIT_PASSWORD

# Dashboard password
python3 -c "import secrets; print(secrets.token_hex(16))"
# Paste result as SC_DASHBOARD_PASSWORD

# PostgreSQL password
python3 -c "import secrets; print(secrets.token_hex(16))"
# Paste result as SENTINEL_PG_PASSWORD

# Sentinel API token (if using Foresight)
python3 -c "import secrets; print(secrets.token_hex(32))"
# Paste result as SENTINEL_API_TOKEN
```

### 3. Start the core stack

```bash
docker compose up -d
```

This starts influxdb, postgres, collector, balancer, and dashboard. Sentinel is **not** started
unless you add the `sentinel` profile (see below).

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
and the balancer starts evaluating in dry-run.

### 7. Confirm dry-run before going live

Every cluster starts in dry-run mode: the balancer logs what it *would* do but performs no
migrations. Once you have confirmed it is analysing the cluster correctly, switch **Dry run**
off for that cluster in the dashboard's Balancer Settings. No restart is needed.

---

## Starting Foresight (Sentinel)

Sentinel is gated behind Docker Compose profiles to keep it optional.

```bash
# Start sentinel alongside the core stack
docker compose --profile sentinel up -d

# Or with the 'full' profile (core + sentinel); add --profile tools for grafana
docker compose --profile full --profile tools up -d
```

Sentinel exposes a health endpoint and a REST API:

```bash
# Health check
curl http://localhost:8080/health

# REST API (requires SENTINEL_API_TOKEN if set)
curl -H "Authorization: Bearer <token>" http://localhost:8081/api/v1/summary
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

| Variable | Description |
|---|---|
| `SC_DRY_RUN` | `true` = log only, no migrations. **Start here.** |
| `SC_VERBOSITY` | Log verbosity 1–5 (default: 3) |
| `SC_RAM_LIMIT_PERCENT` | Hard RAM ceiling per node (default: 85%) |
| `SC_CPU_UPPER_THRESHOLD_PERCENT` | CPU % that triggers reactive balancing (default: 50%) |
| `SC_PREDICTIVE_BALANCING_ENABLED` | Enable/disable The Oracle predictive engine |
| `SC_PREDICTIVE_INTERVAL_SECONDS` | How often The Oracle runs (default: 43200 / 12h) |
| `SC_EXCLUDE_NODE_IPS` | Comma-separated node IPs to never migrate to/from |

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
- [ ] `SC_DRY_RUN` sign-off completed before setting to `false`
- [ ] Dashboard (port 5000) not directly internet-facing — place behind a reverse proxy with TLS
- [ ] InfluxDB (port 8086) not exposed beyond the host — internal Docker network only in production
- [ ] Sentinel API (port 8081) not exposed beyond the host — accessed via dashboard proxy only
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
`influxdb-data`.

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

### 1. Pull latest code

```bash
git pull
```

### 2. Rebuild images

```bash
docker compose build
# With sentinel:
docker compose --profile sentinel build
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
docker compose --profile full build
docker compose --profile full up -d
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
# With sentinel:
docker compose --profile sentinel up -d
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
docker compose logs sentinel | grep '{' | jq 'select(.levelname == "WARNING")'
```

---

## Health Checks

```bash
# Sentinel health endpoint
curl http://localhost:8080/health
curl http://localhost:8080/health/detail

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

1. Confirm `SC_DRY_RUN=false` in `.env`
2. Check CPU/RAM thresholds — balancing only triggers when a node exceeds them
3. Check `SC_EXCLUDE_NODE_IPS` — the target node may be excluded
4. Review balancer logs for `would migrate` or `skipping` messages

### Sentinel shows no data in dashboard

1. Confirm sentinel is running: `docker compose --profile sentinel ps`
2. Check sentinel logs for errors on first collection/analysis cycle
3. The Foresight tab populates after the first analysis cycle (up to 1 hour by default)
4. Verify `SENTINEL_API_TOKEN` matches between `.env` and the dashboard's `SENTINEL_API_TOKEN`

### `.env` changes not taking effect

Environment variable changes require a container restart:

```bash
docker compose up -d --no-deps <service>
```

For token or password changes, restart the affected service plus any service that connects to it.
