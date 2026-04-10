# HyperCore Cluster Balancer

**Reactive, Predictive & Affinity-Based Load Balancing for Scale Computing HyperCore**

The HyperCore Cluster Balancer is a containerized solution that automatically distributes virtual machine workloads across Scale Computing HyperCore cluster nodes. It monitors resource usage in real time, enforces VM placement rules, and — optionally — uses machine learning to predict and prevent future overloads before they happen.

The optional **SC//Foresight** module extends the platform with long-range capacity intelligence: multi-horizon forecasting, VM right-sizing recommendations, cluster health scoring, scenario modelling, and data sharing with a central capacity viewer.

---

## Features

### Core Stack

- **Reactive Balancing** — Continuously monitors node CPU and RAM usage via sliding-window averages. When a node exceeds configurable thresholds, the lightest suitable VM is live-migrated to the least-loaded node.
- **Predictive Balancing (The Oracle)** — Uses [Facebook Prophet](https://facebook.github.io/prophet/) to forecast per-VM CPU load 24 hours ahead. Proactively migrates VMs before predicted spikes arrive. Runs in a background thread with parallel workers so the reactive engine is never blocked.
- **Affinity Enforcement** — Highest-priority engine that enforces VM placement rules via HyperCore tags:
  - `node_<suffix>` — Pin a VM to a specific node (e.g. `node_241` pins to the node whose IP ends in `.241`)
  - `anti_<vm_name>` — Prevent two VMs from sharing the same node (e.g. `anti_dc02` on `dc01`)
- **Live Configuration** — All tunable settings are stored in a shared PostgreSQL database. Change any parameter through the dashboard UI at runtime — no container restarts required.
- **Built-in Dashboard** — Web-based UI (port 5000) showing node/VM performance, drive health, VSD I/O, migration event log, balancer settings, and the full SC//Foresight interface. Fully offline-capable for dark-site deployments.
- **Metrics Collection** — Comprehensive HyperCore telemetry (node performance, VM stats, drive health, VSD I/O, replication, cluster conditions) stored in InfluxDB with automatic downsampling.

### SC//Foresight (Optional — `sentinel` profile)

- **Capacity Forecasting** — Regression-based forecasts across four horizons (30, 90, 180, 365 days) for CPU, RAM, storage capacity, storage latency, and network throughput. Confidence-scored; low-confidence forecasts are suppressed.
- **VM Right-Sizing** — Detects over- and under-provisioned VMs via three independent signals: sustained CPU usage, storage latency correlation, and recurring CPU spike patterns.
- **Resize Advisor** — Translates right-sizing flags into concrete vCPU change recommendations with confidence scores and risk classifications.
- **Cluster Health Scoring** — Aggregate 0–100 health score derived from forecast trends and right-sizing findings. Severity levels: EMERGENCY, CRITICAL, WARNING, ATTENTION, INFO.
- **What-If Scenario Modelling** — Simulate adding or removing VMs and see the projected impact on cluster health and resource exhaustion timelines.
- **Outcome Tracking** — Polls HyperCore for VM vCPU changes and records whether right-sizing recommendations were acted on, allowing recommendation quality to be measured over time.
- **PDF Reports** — Downloadable capacity reports with optional branding (logo, accent colour, company name).
- **Webhook Alerts** — HMAC-SHA256 signed HTTPS alerts for CRITICAL and EMERGENCY events, with automatic retry.
- **Live Tunables** — All analysis thresholds and toggles are editable from the dashboard at runtime, without restarting Sentinel.
- **Data Sharing** — Optionally push signed capacity snapshots to an SC//Foresight-Central viewer. Includes customer-controlled field anonymisation and AES-256-GCM encrypted offline export.

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
                   │ (reactive + │──┐   │(SC//Foresight│
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
| **PostgreSQL** | Config store and SC//Foresight conclusions database                      |
| **Dashboard**  | Web UI for monitoring, live configuration, and SC//Foresight             |
| **Sentinel**   | *(optional)* SC//Foresight capacity analysis, forecasting, and REST API  |
| **Grafana**    | *(optional)* Advanced metrics exploration                                |

---

## Quick Start

```bash
git clone https://github.com/wvcollenburg/hypercore-manager.git
cd hypercore-manager

cp .env.template .env
# Edit .env — set SC_HOST, SC_USERNAME, SC_PASSWORD, and change all CHANGE_ME values

docker compose up --build -d
```

The dashboard is available at **http://localhost:5000**.

> **Note:** On first startup, the balancer needs ~2.5 minutes to fill its averaging buffers before it begins evaluating the cluster. The dashboard will show data after the first collector cycle (~30 seconds).

### Starting with SC//Foresight

```bash
# Core stack + Sentinel capacity analysis
docker compose --profile sentinel up --build -d

# Core stack + Sentinel + Grafana
docker compose --profile full up --build -d

# Grafana only (without Sentinel)
docker compose --profile tools up --build -d
```

> **Note:** SC//Foresight requires at least 14 days of metrics history before meaningful forecasts and right-sizing analysis can be produced.

---

## Configuration

All settings are defined in `.env` and seeded into a shared PostgreSQL config database on first startup. After that, settings can be changed live through the dashboard UI — no container restart required. Cluster credentials and connection strings are environment-only and require a restart to change.

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

### SC//Foresight Tunables

All Sentinel analysis thresholds are live-editable from the SC//Foresight → Settings tab in the dashboard. Key parameters:

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

The dashboard is a single-page application served on port 5000. It is divided into four main areas:

| Tab | Contents |
|-----|----------|
| **SC//Infraviewer** | Node CPU/RAM history, drive health and temperature, VM metrics, VSD read/write I/O |
| **SC//Clusterbalancer** | Migration event log (last 7 days), reactive and predictive engine settings |
| **SC//Foresight** | Capacity forecasts, right-sizing recommendations, cluster health, reports, what-if modelling, live Sentinel tunables, data sharing — requires `sentinel` or `full` profile |
| **Modules** | Auto-discovered InfluxDB measurements for ad-hoc data exploration |

---

## SC//Foresight in Detail

SC//Foresight (the `sentinel` container) runs four independent background loops:

1. **Connectivity loop** — lightweight InfluxDB ping every 5 minutes
2. **Analysis loop** — full capacity analysis every hour: forecasting, right-sizing, resize advice, cluster health scoring. Queries InfluxDB on demand and frees all data after each cycle to keep memory usage flat.
3. **Outcome loop** — polls HyperCore every 6 hours for VM vCPU changes and records whether right-sizing recommendations were followed
4. **Webhook loop** — dispatches CRITICAL and EMERGENCY alerts every 60 seconds via outbound HTTPS POST (HMAC-SHA256 signed, retry schedule: 5 min, 30 min, dead-letter)

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

Sentinel can push signed capacity snapshots to an SC//Foresight-Central viewer instance every 4 hours. Customers control which fields are anonymised before data leaves the cluster. Snapshots can also be exported manually as an AES-256-GCM encrypted `.hcb` file for offline transfer.

See [docs/phase4-data-sharing.md](docs/phase4-data-sharing.md) for the setup guide.

The central viewer that receives these snapshots is a separate deployment:
**[SC//Foresight-Central](https://github.com/wvcollenburg/sc-foresight-central)** — multi-tenant MSP capacity viewer.

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

The core stack (Collector, Balancer, Dashboard, InfluxDB, PostgreSQL) is lightweight. SC//Foresight (Sentinel) adds significant memory during analysis cycles due to time-series data processing; it releases memory immediately after each cycle.

### Core Stack

| VMs  | Min. vCPUs | Recommended RAM | Disk (1 yr) |
|------|-----------|-----------------|-------------|
| 5    | 2         | 1 GB            | 2 GB        |
| 50   | 2         | 1 GB            | 12 GB       |
| 100  | 2         | 2 GB            | 23 GB       |
| 500  | 4         | 4 GB            | 112 GB      |

### SC//Foresight (Sentinel) — Peak Analysis Memory

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
├── DEPLOY.md                        # Full operational guide
├── docs/
│   └── phase4-data-sharing.md       # Data sharing setup guide
├── influxdb/
│   ├── Dockerfile
│   └── init.sh                      # Creates long-term bucket and downsampling tasks
├── postgres/
│   ├── Dockerfile
│   └── init/                        # 16 SQL migration scripts (run in order on fresh volume)
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
│   ├── app.py                       # Flask API and Sentinel proxy routes
│   └── static/
│       └── index.html               # Single-page dashboard
├── sentinel/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── sentinel.py                  # Entry point — four background loops
│   ├── api.py                       # REST API (port 8081)
│   ├── config.py                    # Environment variable config with defaults
│   ├── tunables.py                  # Live-editable analysis settings (Phase 3.5)
│   ├── influx_client.py             # Read-only streaming InfluxDB consumer
│   ├── writer.py                    # PostgreSQL conclusions writer
│   ├── exporter.py                  # Signed snapshot export (Phase 4)
│   ├── anonymiser.py                # Field-level anonymisation (Phase 4)
│   ├── hcb.py                       # AES-256-GCM .hcb file format (Phase 4)
│   ├── sync_push.py                 # 4-hour auto-push to central viewer (Phase 4)
│   ├── webhook.py                   # HMAC-signed alert delivery (Phase 3)
│   ├── outcome_tracker.py           # Right-sizing outcome polling (Phase 3)
│   ├── health.py                    # Liveness probe (/health)
│   ├── log.py                       # Structured JSON logging
│   ├── collector/
│   │   ├── nodes.py                 # Node CPU/RAM/network data collection
│   │   ├── vms.py                   # Per-VM CPU and network profiling
│   │   └── storage.py               # Storage capacity, VSD latency, drive health
│   ├── analysis/
│   │   ├── forecaster.py            # Multi-horizon regression forecasting
│   │   ├── rightsizing.py           # Over/under-provisioning detection
│   │   ├── resize_advisor.py        # Concrete vCPU recommendations
│   │   ├── cluster_health.py        # Aggregate health scoring
│   │   ├── whatif.py                # Scenario modelling (Phase 3)
│   │   ├── anomaly_filter.py        # Seasonality-aware outlier removal
│   │   └── seasonality.py           # Daily/weekly/monthly pattern recognition
│   └── reports/
│       └── pdf_report.py            # PDF report generation with branding
└── tests/
    ├── test_01_infra.py              # Health endpoint and API reachability
    ├── test_02_db_schema.py          # PostgreSQL schema validation
    ├── test_03_auth.py               # Bearer token authentication
    ├── test_04_api.py                # REST endpoint contracts
    ├── test_05_data.py               # Data collection and analysis pipeline
    ├── test_06_tunables.py           # Live config GET/PUT
    ├── test_07_whatif.py             # Scenario modelling
    ├── test_08_export.py             # Snapshot export and anonymisation
    ├── test_09_webhooks.py           # Webhook dispatch and HMAC signing
    ├── conftest.py
    └── pytest.ini
```

---

## Documentation

| Document | Contents |
|----------|----------|
| [DEPLOY.md](DEPLOY.md) | Prerequisites, environment variables, security hardening, backup/restore, upgrade procedure, health checks, troubleshooting |
| [docs/phase4-data-sharing.md](docs/phase4-data-sharing.md) | Data sharing setup: enrollment, anonymisation, `.hcb` file format, troubleshooting |

---

## Related Projects

| Repository | Description |
|------------|-------------|
| [SC//Foresight-Central](https://github.com/wvcollenburg/sc-foresight-central) | Multi-tenant MSP viewer — aggregates capacity snapshots pushed by Sentinel across all enrolled customer clusters |

---

## Disclaimer

**USE AT YOUR OWN RISK.** While the HyperCore Cluster Balancer has been carefully developed and tested, it is provided "as is" without warranty of any kind, express or implied. Scale Computing accepts no responsibility or liability for any damage, data loss, downtime, or other consequences resulting from the use of this software. Always run in dry-run mode (`SC_DRY_RUN=true`) for an extended observation period before enabling live migrations.

## License

MIT License

Copyright (c) 2026 Scale Computing, Inc.

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
