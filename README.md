# HyperCore Manager

**Cluster Balancing, Capacity Intelligence & Self-Updating Stack for Scale Computing HyperCore**

> **Community supported · Apache-2.0**
>
> HyperCore Manager is open-source software maintained on a community-supported basis. It is **not** an official Scale Computing product: there is no official support, no SLA, and no guarantee of future releases. Use it at your own risk. Questions, bug reports and feature requests go through [GitHub Issues](https://github.com/wvcollenburg/hypercore-manager/issues). See [CONTRIBUTING.md](CONTRIBUTING.md) for how contributions are handled.

HyperCore Manager is a containerized stack that automatically distributes virtual machine workloads across Scale Computing HyperCore cluster nodes. It monitors resource usage in real time, enforces VM placement rules, and — optionally — uses machine learning to predict and prevent future overloads before they happen.

The optional **Foresight** module extends the platform with long-range capacity intelligence: multi-horizon forecasting, VM right-sizing recommendations, cluster health scoring, scenario modelling, and data sharing with a central capacity viewer.

---

## Features

### Core Stack

- **Reactive Balancing** — Continuously monitors node CPU and RAM usage via sliding-window averages. When a node exceeds configurable thresholds, the lightest suitable VM is live-migrated to the least-loaded node.
- **Predictive Balancing (The Oracle)** — Uses [Facebook Prophet](https://facebook.github.io/prophet/) to forecast per-VM CPU load 24 hours ahead. Proactively migrates VMs before predicted spikes arrive. Runs in a background thread with parallel workers so the reactive engine is never blocked.
- **Affinity Enforcement** — Highest-priority engine that enforces VM placement rules via HyperCore tags:
  - `node_<suffix>` — Pin a VM to a specific node (e.g. `node_241` pins to the node whose IP ends in `.241`)
  - `anti_<vm_name>` — Prevent two VMs from sharing the same node (e.g. `anti_dc02` on `dc01`)
- **Clusters configured in the tool** — Register one or more HyperCore clusters from the dashboard (Settings → Clusters). Credentials are stored encrypted in PostgreSQL; nothing cluster-specific lives in `.env`. With more than one cluster a selector rail appears and every view, setting and Foresight analysis is per cluster.
- **Live Configuration** — All tunable settings are stored in a shared PostgreSQL database. Change any parameter through the dashboard UI at runtime — no container restarts required.
- **Built-in Dashboard** — Web-based UI (port 5000) showing node/VM performance, drive health, VSD I/O, migration event log, balancer settings, and the full Foresight interface. Fully offline-capable for dark-site deployments.
- **Multi-language UI** — English, Dutch, French, and German, selectable from the dashboard header.
- **User Authentication** — Username/password login with optional per-user TOTP (one-time password) two-factor. Auth can be disabled for local/dev use by leaving the dashboard credentials blank.
- **In-app Updater** — Detects new tagged releases on origin and applies them in place with one click; database migrations ship and apply automatically with the release that needs them.
- **Exports (optional `exports` profile)** — Daily VM exports to an SMB or NFS share in timestamped folders with retention pruning, plus VM import back into any registered cluster. Scheduled per VM from the dashboard; progress tracked from HyperCore's task API; interrupted runs re-attach after a restart.
- **Metrics Collection** — Comprehensive HyperCore telemetry (node performance, VM stats, drive health, VSD I/O, replication, cluster conditions) stored in InfluxDB with automatic downsampling.

### Foresight (Optional — `sentinel` profile)

- **Capacity Forecasting** — Regression-based forecasts across four horizons (30, 90, 180, 365 days) for CPU, RAM, storage capacity, storage latency, and network throughput. Confidence-scored; low-confidence forecasts are suppressed.
- **VM Right-Sizing** — Detects over- and under-provisioned VMs via three independent signals: sustained CPU usage, storage latency correlation, and recurring CPU spike patterns.
- **Resize Advisor** — Translates right-sizing flags into concrete vCPU change recommendations with confidence scores and risk classifications.
- **Cluster Health Scoring** — Aggregate 0–100 health score derived from forecast trends and right-sizing findings. Severity levels: EMERGENCY, CRITICAL, WARNING, ATTENTION, INFO.
- **What-If Scenario Modelling** — Simulate adding or removing VMs and see the projected impact on cluster health and resource exhaustion timelines.
- **Outcome Tracking** — Polls HyperCore for VM vCPU changes and records whether right-sizing recommendations were acted on, allowing recommendation quality to be measured over time.
- **PDF Reports** — Downloadable capacity reports with optional branding (logo, accent colour, company name).
- **Webhook Alerts** — HMAC-SHA256 signed HTTPS alerts for CRITICAL and EMERGENCY events, with automatic retry.
- **Live Tunables** — All analysis thresholds and toggles are editable from the dashboard at runtime, without restarting Sentinel.
- **Data Sharing** — Optionally push signed capacity snapshots to an Foresight-Central viewer. Includes customer-controlled field anonymisation and AES-256-GCM encrypted offline export.

---

## Architecture

```
┌────────────┐     ┌────────────┐      ┌─────────────┐
│  Collector │────▶│  InfluxDB  │◀────│  Dashboard  │
│  (30s poll)│     │ (metrics)  │      │  (port 5000)│
└────────────┘     └─────┬──────┘      └──────┬──────┘
                         │                     │
                   ┌─────▼───────┐      ┌──────▼──────┐
                   │  Balancer   │      │   Sentinel  │
                   │ (reactive + │──┐   │ (Foresight)  │
                   │  predictive │  │   │ port 8081)  │
                   │  + affinity)│  │   └──────┬──────┘
                   └─────────────┘  │          │
                          │         │   ┌──────▼──────┐
                          └─────────┼──▶│  PostgreSQL │
                                    │   │  (config +  │
                                    │   │conclusions) │
                                    │   └─────────────┘
                                    │
                                    ▼
                            HyperCore REST API
                            (live migrations)
```

| Container    | Role                                                                       |
|--------------|----------------------------------------------------------------------------|
| **Collector**  | Polls HyperCore API, writes metrics to InfluxDB                          |
| **Balancer**   | Decision engine — affinity, reactive, and predictive modes               |
| **InfluxDB**   | Time-series metrics storage with 1-year detailed + 5-year long-term retention |
| **PostgreSQL** | Config store and Foresight conclusions database                      |
| **Dashboard**  | Web UI for monitoring, live configuration, and Foresight             |
| **Sentinel**   | *(optional)* Foresight capacity analysis, forecasting, and REST API  |
| **Exports**    | *(optional)* Scheduled VM exports to SMB/NFS, retention pruning, VM imports |
| **Grafana**    | *(optional)* Advanced metrics exploration                                |

---

## Installation

### Prerequisites

- A Linux host or VM (Ubuntu 22.04+ tested) with **Docker Engine 24+** and **Docker Compose v2**.
  - Compose v2 is the `docker compose` (no hyphen) command. The legacy python `docker-compose` 1.29.x is **not supported** — it has known recreate bugs that break the in-app updater. On Debian/Ubuntu, install via `sudo apt install docker-compose-v2`.
- HyperCore cluster management IP and a service account with read + migration permissions.
- Outbound HTTPS access to the HyperCore REST API (or fully offline — see [Dark-Site Deployment](#dark-site--air-gapped-deployment)).
- ~2 vCPU / 4 GB RAM minimum for the core stack; more for Foresight (see [Resource Requirements](#resource-requirements)).

There are two supported installation paths. Both work equally well on a laptop with Docker Desktop for a trial run as on a dedicated Linux VM; Apple Silicon and other arm64 hosts are supported.

### Method 1 — Manual Docker Compose

For operators running the stack on infrastructure they already manage.

```bash
git clone https://github.com/wvcollenburg/hypercore-manager.git
cd hypercore-manager

cp .env.template .env
# Edit .env — replace every CHANGE_ME (HCM_SECRET_KEY, database secrets, dashboard password)

docker compose up --build -d
```

The dashboard is available at **http://localhost:5000**. Log in, and the first screen asks for
your cluster: management address plus a HyperCore account. Save it and the collector, balancer
and Foresight start on that cluster within a minute. Add further clusters under
Settings → Clusters.

> **Note:** On first startup, the balancer needs ~2.5 minutes to fill its averaging buffers before it begins evaluating the cluster. The dashboard will show data after the first collector cycle (~30 seconds).

### Method 2 — Interactive Curses Installer (Linux/macOS)

`install.py` walks every `.env` setting interactively, explaining each one and generating strong secrets on demand. Pre-fills any existing `.env`. Helpful when you want guided configuration instead of editing the file by hand.

```bash
git clone https://github.com/wvcollenburg/hypercore-manager.git
cd hypercore-manager

python3 install.py
# Then run the docker compose commands shown on the final screen.
```

The installer also detects whether Compose v2 is present on the host and warns if the legacy v1 is in the way. Windows users can run this via WSL, or `pip install windows-curses` first.

### Starting with Foresight

```bash
# Core stack + Sentinel capacity analysis
docker compose --profile sentinel up --build -d

# Core stack + Sentinel + Exports (the `full` profile covers both; Grafana is the `tools` profile)
docker compose --profile full --profile tools up --build -d

# Core stack + Exports only
docker compose --profile exports up --build -d

# Grafana only (without Sentinel)
docker compose --profile tools up --build -d
```

> **Note:** Foresight requires at least 14 days of metrics history before meaningful forecasts and right-sizing analysis can be produced.

---

## Configuration

Clusters are registered in the dashboard, never in `.env`. Everything else (database secrets,
ports, the `HCM_SECRET_KEY` that encrypts stored cluster credentials, and the application defaults
below) is defined in `.env`. The defaults are seeded into PostgreSQL on first start as the
**Defaults** template; every cluster you register gets its own copy, editable live in the dashboard
without a restart. Database connection strings and secrets remain environment-only.

### Registering clusters

Settings → Clusters (admin only): name, management address (any node IP or hostname), HyperCore
username and password, and whether to verify the TLS certificate. **Test connection** logs in
and reads the cluster name, node count and HyperCore version before anything is saved. The
balancer account needs rights to live-migrate VMs; read-only accounts work for monitoring and
dry-run. A cluster can be disabled (collection and balancing pause, history stays) or removed
(optionally purging its Foresight history).

`HCM_SECRET_KEY` protects the stored passwords. Back it up with the database; if it is lost the
clusters keep working until the next restart, after which their passwords must be re-entered.

### Key Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `SC_DRY_RUN` | `true` | **Start here.** Logs decisions without executing migrations. |
| `SC_CPU_UPPER_THRESHOLD_PERCENT` | `50.0` | Node CPU % that triggers reactive balancing |
| `SC_RAM_UPPER_THRESHOLD_PERCENT` | `80.0` | Node RAM % that triggers reactive balancing |
| `SC_RAM_LIMIT_PERCENT` | `85.0` | Hard ceiling — never overload a target node beyond this |
| `SC_CPU_TARGET_TOLERANCE_PERCENT` | `5.0` | Re-balancing stops when all node CPU values are within this % of the target |
| `SC_MAX_VCPU_RATIO` | `2.0` | Max vCPU-to-thread overcommit ratio |
| `SC_VM_MOVE_COOLDOWN_MINUTES` | `30` | Minimum time between moves of the same VM |
| `SC_MIGRATION_COOLDOWN_MINUTES` | `5` | Minimum time between any migration events |
| `SC_PREDICTIVE_BALANCING_ENABLED` | `false` | Enable/disable the Oracle |
| `SC_PREDICTIVE_INTERVAL_SECONDS` | `43200` | How often the Oracle runs (default: 12 hours) |
| `SC_PREDICTIVE_MAX_WORKERS` | `8` | Parallel threads for VM forecasting |
| `SC_EXCLUDE_NODE_IPS` | *(empty)* | Comma-separated node IPs to exclude from all balancing |

See [DEPLOY.md](DEPLOY.md) for the full configuration reference.

### Foresight Tunables

All Sentinel analysis thresholds are live-editable from the Foresight → Settings tab in the dashboard. Key parameters:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `SENTINEL_ANALYSIS_WINDOW_DAYS` | `14` | Lookback window for right-sizing analysis |
| `SENTINEL_CPU_OVER_PROVISIONED_THRESHOLD` | `20.0` | CPU avg % below which a VM is flagged over-provisioned |
| `SENTINEL_CPU_UNDER_PROVISIONED_AVG_THRESHOLD` | `80.0` | CPU avg % above which a VM is flagged under-provisioned |
| `SENTINEL_CPU_UNDER_PROVISIONED_P95_THRESHOLD` | `90.0` | CPU p95 % above which a VM is flagged under-provisioned |
| `SENTINEL_FORECAST_30D_ENABLED` | `true` | Enable/disable 30-day forecast horizon |
| `SENTINEL_FORECAST_90D_ENABLED` | `true` | Enable/disable 90-day forecast horizon |
| `SENTINEL_FORECAST_180D_ENABLED` | `true` | Enable/disable 180-day forecast horizon |
| `SENTINEL_FORECAST_365D_ENABLED` | `true` | Enable/disable 365-day forecast horizon |
| `SENTINEL_ANOMALY_FILTER_ENABLED` | `true` | Enable seasonality-aware outlier suppression |
| `SENTINEL_RETENTION_MONTHS` | `12` | How long conclusions are retained in the database |
| `SENTINEL_OUTCOME_TRACKER_ENABLED` | `true` | Track whether right-sizing recommendations were acted on |
| `SENTINEL_WEBHOOK_ENABLED` | `true` | Send CRITICAL/EMERGENCY alerts via webhook |

---

## VM Placement Tags

Placement rules are defined as tags on VMs in HyperCore — no balancer configuration required.

| Tag Format | Effect | Example |
|------------|--------|---------|
| `node_<suffix>` | Pin VM to the node whose LAN IP ends with `<suffix>` | `node_241` |
| `anti_<vm_name>` | Never place this VM on the same node as `<vm_name>` | `anti_dc02` |

Affinity rules take priority over all other engines. A VM with a `node_` tag will be moved back to its pinned node even if doing so would worsen cluster balance.

---

## Dashboard

The dashboard is a single-page application served on port 5000. The UI is available in **English, Dutch, French, and German** — pick from the language selector in the header. The currently running version is shown in a small footer at the bottom-right of every page.

It is divided into four main areas:

| Tab | Contents |
|-----|----------|
| **Infraviewer** | Node CPU/RAM history, drive health and temperature, VM metrics, VSD read/write I/O |
| **Clusterbalancer** | Migration event log (last 7 days), reactive and predictive engine settings |
| **Foresight** | Capacity forecasts, right-sizing recommendations, cluster health, reports, what-if modelling, live Sentinel tunables, data sharing — requires `sentinel` or `full` profile |
| **Modules** | Auto-discovered InfluxDB measurements for ad-hoc data exploration |

Authentication is handled per-user from the Settings → Users panel, including optional per-user TOTP two-factor. To run without auth (local/dev), leave `SC_DASHBOARD_USER` blank in `.env`.

On first login each user sees a one-time notice explaining that this is community-supported software and where to report issues. The acknowledgement is stored per user; when auth is disabled it is stored in the browser instead.

---

## Foresight in Detail

Foresight (the `sentinel` container) runs five independent background loops:

1. **Connectivity loop** — lightweight InfluxDB ping every 5 minutes
2. **Analysis loop** — full capacity analysis every hour: forecasting, right-sizing, resize advice, cluster health scoring. Queries InfluxDB on demand and frees all data after each cycle to keep memory usage flat.
3. **Pruning loop** — every 15 minutes, drops conclusions for VMs that no longer exist on the cluster and enforces the retention window
4. **Outcome loop** — polls HyperCore every 6 hours for VM vCPU changes and records whether right-sizing recommendations were followed
5. **Webhook loop** — dispatches CRITICAL and EMERGENCY alerts every 60 seconds via outbound HTTPS POST (HMAC-SHA256 signed, retry schedule: 5 min, 30 min, dead-letter)

### REST API

Sentinel exposes a REST API on port 8081 (internal to the Docker network; proxied through the dashboard). Authentication is optional via `SENTINEL_API_TOKEN`.

| Endpoint | Description |
|----------|-------------|
| `GET /api/v1/forecasts` | Latest capacity forecast snapshots |
| `GET /api/v1/rightsizing` | Right-sizing findings per VM |
| `GET /api/v1/resize` | Concrete vCPU resize recommendations |
| `GET /api/v1/cluster-health` | Cluster health score and breakdown |
| `GET /api/v1/exhaustion` | Resource exhaustion alerts |
| `GET /api/v1/summary` | Single-call overview |
| `POST /api/v1/whatif` | Scenario simulation |
| `GET /api/v1/outcomes` | Right-sizing outcome history |
| `GET /api/v1/tunables` | Live analysis settings |
| `PUT /api/v1/tunables` | Update live analysis settings |
| `GET /api/v1/export/snapshot` | Signed capacity snapshot (JSON) |
| `GET /api/v1/export/hcb` | AES-256-GCM encrypted offline export |

### Data Sharing

Sentinel can push signed capacity snapshots to an Foresight-Central viewer instance every 4 hours. Customers control which fields are anonymised before data leaves the cluster. Snapshots can also be exported manually as an AES-256-GCM encrypted `.hcb` file for offline transfer.

See [docs/phase4-data-sharing.md](docs/phase4-data-sharing.md) for the setup guide.

The central viewer that receives these snapshots is a separate deployment:
**[Foresight-Central](https://github.com/wvcollenburg/sc-foresight-central)** — multi-tenant MSP capacity viewer.

---

## Exports

The optional `exports` container schedules VM exports and runs imports, driven from the
**Exports** tab (shown only when the container is running).

- **Schedules.** Per VM: a share URI (`smb://nas.local/share/path` or `nfs://nas.local/export/path`,
  without credentials), share username and password for SMB (stored encrypted), copies to keep,
  daily run time (in `TZ`), optional qcow2 compression. Every run exports to
  `<destination>/<vm-name>_YYYYMMDD-HHMMSS/`; HyperCore creates that folder itself, so the
  destination path must already exist on the share.
- **Retention.** After a successful export, older copies of that VM beyond the retention count
  are deleted, oldest first. SMB shares are pruned over the protocol with the stored credentials;
  for NFS, bind-mount the share into the `exports` container and set the prune path on the
  schedule. Other VMs' folders are never touched.
- **Imports.** Pick a saved export location (credentials reused server-side) or type one, browse
  the share for export folders, optionally rename, and import into the selected cluster. A name
  that already exists on the target is refused.
- **Runs.** Export and import history with live progress from HyperCore's task API. A run
  interrupted by a container restart is re-attached by its task tag.
- **Rights.** The cluster account registered in Settings → Clusters needs HyperCore permission to
  export and import VMs; a read-only account gives a clear 403 on the first run. Share usernames
  may be `user`, `DOMAIN\user` or `user@domain`.
- **Share access from a laptop stack** works over SMB without any mount (userland client).

## Updates

The dashboard includes an in-app updater that detects new releases and applies them in place. It compares the running version (read from the `VERSION` file at the repo root) against the latest semver-style git tag on `origin`, and surfaces an "Update available" badge in the header when a newer release exists.

- Versioning follows [Semantic Versioning 2.0.0](https://semver.org/). During the pre-1.0 phase, MINOR bumps may include breaking changes; PATCH bumps are bug-fix and documentation only.
- Releases are tagged in git as `vMAJOR.MINOR.PATCH` and announced on [GitHub Releases](https://github.com/wvcollenburg/hypercore-manager/releases). Untagged commits don't trigger update prompts — only deliberate releases do, so customers are never bombarded with "update available" for in-flight development.
- Database migrations under `postgres/init/` are applied automatically on dashboard startup; every script is idempotent (`CREATE TABLE IF NOT EXISTS` and similar guards) so existing installs never lose data when a new migration ships.
- Manual update path (equivalent to clicking Apply Update in the dashboard): `git pull && docker compose --profile <profile> up --build -d`.

---

## Dark-Site / Air-Gapped Deployment

```bash
# On an internet-connected machine:
docker compose build
docker save $(docker compose config --images) -o hypercore-balancer-images.tar

# On the air-gapped machine:
docker load -i hypercore-balancer-images.tar
docker compose up -d
```

The entire stack runs fully offline once images are built. No outbound connections are required unless data sharing is explicitly configured.

---

## Resource Requirements

The core stack (Collector, Balancer, Dashboard, InfluxDB, PostgreSQL) is lightweight. Foresight (Sentinel) adds significant memory during analysis cycles due to time-series data processing; it releases memory immediately after each cycle.

### Core Stack

| VMs  | Min. vCPUs | Recommended RAM | Disk (1 yr) |
|------|-----------|-----------------|-------------|
| 5    | 2         | 1 GB            | 2 GB        |
| 50   | 2         | 1 GB            | 12 GB       |
| 100  | 2         | 2 GB            | 23 GB       |
| 500  | 4         | 4 GB            | 112 GB      |

### Foresight (Sentinel) — Peak Analysis Memory

Sentinel queries InfluxDB on demand and frees all data after each analysis cycle. Peak usage occurs during the analysis window and depends on the number of managed VMs and the `SENTINEL_ANALYSIS_WINDOW_DAYS` setting (default 14 days).

| VMs | Estimated Peak |
|-----|----------------|
| 25  | ~350 MB        |
| 100 | ~1.2 GB        |
| 250 | ~3 GB          |
| 500 | ~6 GB          |

Sentinel's memory limit in `docker-compose.yaml` should be sized to cover the peak for your VM count. Between analysis cycles, idle memory returns to ~100 MB.

> **Note on CPU throttling:** The CPU limits in `docker-compose.yaml` are tuned for a 2-core shared host. On a dedicated VM with 4+ CPUs, remove the `cpus:` limits from all services. InfluxDB in particular suffers badly from CPU quota throttling — Flux queries are bursty and multi-threaded, and hitting the quota causes repeated pause/resume cycles that can turn sub-second queries into 10+ second waits. See [DEPLOY.md](DEPLOY.md) for per-environment guidance.

---

## Grafana

Grafana is not started by default. To enable it:

```bash
docker compose --profile tools up -d grafana
# Available at http://localhost:3000
```

Connect it to InfluxDB using the Flux query language at `http://influxdb:8086`.

---

## Project Structure

```
├── docker-compose.yaml
├── .env.template
├── VERSION                          # Single line: current version (e.g. 1.0.0)
├── LICENSE                          # Apache License 2.0
├── NOTICE
├── README.md
├── DEPLOY.md                        # Full operational guide
├── CONTRIBUTING.md                  # How issues and contributions are handled
├── SECURITY.md                      # How to report a vulnerability
├── CHANGELOG.md
├── install.py                       # Interactive curses installer (Linux/macOS)
├── installer/
│   └── core.py                      # .env schema, validation, autogen helpers
├── shared/                          # single source for modules copied into each service
│   ├── cluster_registry.py          # config.clusters access, credentials, connection test
│   ├── hcm_crypto.py                # Fernet encryption keyed by HCM_SECRET_KEY
│   └── config_db.py                 # per-cluster live tunables
├── tools/
│   └── sync_shared.py               # copies shared/ into the service directories
├── tests/                           # pytest suite (unit + optional integration)
├── .github/                         # CI workflow and issue templates
├── docs/
│   └── phase4-data-sharing.md       # Data sharing setup guide
├── influxdb/
│   ├── Dockerfile
│   └── init.sh                      # Creates long-term bucket and downsampling tasks
├── postgres/
│   ├── Dockerfile
│   └── init/                        # SQL migration scripts (idempotent, auto-applied)
├── Collector/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── config_db.py                 # Shared live-config module (PostgreSQL backed)
│   └── collector.py                 # HyperCore polling and InfluxDB writes
├── Balancer/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── config_db.py
│   ├── HyperCore_balancer.py        # Main decision engine (affinity, reactive, predictive)
│   └── predictive_engine.py         # The Oracle (Prophet forecasting)
├── dashboard/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── config_db.py
│   ├── migrations.py                # DB migration runner (idempotent)
│   ├── versioning.py                # semver helpers for the updater
│   ├── user_db.py                   # users, passwords, TOTP
│   ├── app.py                       # Flask API, Sentinel proxy, in-app updater
│   └── static/
│       ├── index.html               # Single-page dashboard
│       └── lang/                    # English, Dutch, French, German translations
├── exports/
│   ├── Dockerfile
│   ├── exports.py                   # Entry point — scheduler + REST API (port 8082)
│   ├── jobs.py                      # Export/import jobs, SMB pruning, restart recovery
│   ├── scheduler.py                 # APScheduler wiring (daily cron per schedule)
│   ├── api.py                       # REST API proxied by the dashboard
│   └── db.py                        # PostgreSQL schema `exports`
└── sentinel/
    ├── Dockerfile
    ├── requirements.txt
    ├── sentinel.py                  # Entry point — four background loops
    ├── api.py                       # REST API (port 8081)
    ├── analysis/                    # Forecasting, right-sizing, cluster health, what-if
    ├── collector/                   # Per-component data collection (nodes/VMs/storage)
    └── reports/                     # PDF report generation with branding
```

---

## Documentation

| Document | Contents |
|----------|----------|
| [DEPLOY.md](DEPLOY.md) | Prerequisites, environment variables, security hardening, backup/restore, upgrade procedure, health checks, troubleshooting |
| [docs/phase4-data-sharing.md](docs/phase4-data-sharing.md) | Data sharing setup: enrollment, anonymisation, `.hcb` file format, troubleshooting |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Filing good issues, running the stack locally, how code contributions are handled |
| [SECURITY.md](SECURITY.md) | Reporting a vulnerability |
| [CHANGELOG.md](CHANGELOG.md) | Release history |
| [LICENSE](LICENSE) | Apache License 2.0 |

---

## Related Projects

| Repository | Description |
|------------|-------------|
| [Foresight-Central](https://github.com/wvcollenburg/sc-foresight-central) | Multi-tenant MSP viewer — aggregates capacity snapshots pushed by Sentinel across all enrolled customer clusters |

---

## Disclaimer

**USE AT YOUR OWN RISK.** HyperCore Manager is community-supported software, not an official Scale Computing product. It is provided "as is" without warranty of any kind, express or implied, and without any official support or service-level commitment. Neither the authors nor Scale Computing accept responsibility or liability for any damage, data loss, downtime, or other consequences resulting from its use. The balancer live-migrates virtual machines using cluster credentials you provide: always run in dry-run mode (`SC_DRY_RUN=true`) for an extended observation period before enabling live migrations.

## License

Copyright (c) 2026 Scale Computing.

Licensed under the Apache License, Version 2.0. You may obtain a copy of the License at http://www.apache.org/licenses/LICENSE-2.0. Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See [LICENSE](LICENSE) for the full text and [NOTICE](NOTICE) for attribution.
