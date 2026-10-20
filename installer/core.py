"""Shared installer core — used by the curses TUI (install.py) and the test suite.

Clusters are not configured here: they are registered in the dashboard after the stack
is up. The .env holds databases, secrets, ports and application defaults only.

This module owns:
  * The schema describing every variable in .env.template.
  * .env file parsing + rendering.
  * Per-field validation and section status.
  * Strong password / token generation and the easy-mode autogen pass.
  * Small utilities (host normalization, path autodetect).

It deliberately has zero UI dependencies so the curses TUI and the unit
tests share the same logic.
"""

from __future__ import annotations

import os
import secrets
import shutil
import string
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional


# ============================================================================
# constants
# ============================================================================

ENV_PATH = Path(".env")
TEMPLATE_PATH = Path(".env.template")

# strings the schema treats as "still a placeholder, must be replaced"
PLACEHOLDERS = {"CHANGE_ME", "https://nodeip/rest/v1", "password", ""}

# profiles available in the schema, mirrors the docker-compose --profile flags
PROFILE_CORE = "core"
PROFILE_SENTINEL = "sentinel"
PROFILE_TOOLS = "tools"

# easy-mode required keys — filled by the operator, never autogen'd
EASY_REQUIRED_KEYS = {"SC_DASHBOARD_PASSWORD"}

# easy-mode default profile set
EASY_PROFILES = {PROFILE_CORE, PROFILE_SENTINEL}


# ============================================================================
# data model
# ============================================================================

@dataclass
class Field:
    key: str                                # env var name
    label: str                              # short human-readable label
    type: str = "text"                      # text|password|token|int|float|bool|choice|path
    default: str = ""
    description: str = ""                   # what it does
    why_default: str = ""                   # why this value is the default
    when_change: str = ""                   # when you'd want to change it
    choices: Optional[list[str]] = None     # for type=choice
    profiles: set[str] = field(default_factory=lambda: {PROFILE_CORE})
    autofill: Optional[Callable[[], str]] = None
    must_change: bool = False               # placeholder must be replaced before save
    reveal: bool = False                    # password fields, current reveal state


@dataclass
class Section:
    key: str
    title: str
    blurb: str                              # one-line description shown on menu
    fields: list[Field]
    profiles: set[str] = field(default_factory=lambda: {PROFILE_CORE})
    enable_field: Optional[str] = None      # if set, all other fields are disabled when this is empty/false


# ============================================================================
# password / token generation
# ============================================================================

def gen_password(length: int = 24) -> str:
    """Strong password: alphanumeric + symbols, no ambiguous chars (0/O/1/l/I)."""
    alphabet = (
        string.ascii_letters
        + string.digits
        + "!@#$%^&*-_=+?"
    )
    alphabet = alphabet.translate(str.maketrans("", "", "0O1lI"))
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in pw)
                and any(c.isupper() for c in pw)
                and any(c.isdigit() for c in pw)
                and any(not c.isalnum() for c in pw)):
            return pw


def gen_token(nbytes: int = 32) -> str:
    """Hex API token / signing key (default = 256 bits)."""
    return secrets.token_hex(nbytes)


# ============================================================================
# auto-detection helpers
# ============================================================================

def detect_repo_path() -> str:
    """Best-guess for SC_REPO_PATH — the directory the installer is running from
    if it looks like a hypercore-manager checkout, else the canonical default."""
    here = Path.cwd().resolve()
    if (here / "docker-compose.yaml").exists() and (here / ".env.template").exists():
        return str(here)
    return "/opt/hypercore-manager"


def detect_ssh_key_path() -> str:
    home_ssh = Path.home() / ".ssh"
    if home_ssh.is_dir():
        return str(home_ssh)
    if Path("/root/.ssh").is_dir():
        return "/root/.ssh"
    return str(home_ssh)  # report intended location even if missing


def path_exists(p: str) -> bool:
    if not p:
        return False
    try:
        return Path(p).exists()
    except OSError:
        return False


# ============================================================================
# easy-mode helpers
# ============================================================================

def looks_like_host(s: str) -> bool:
    """Cheap sanity check: IPv4 dotted-quad or hostname-ish string."""
    if not s:
        return False
    parts = s.split(".")
    if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
        return True
    if all(c.isalnum() or c in ".-_" for c in s) and any(c.isalpha() for c in s):
        return True
    return False


def extract_host_ip(sc_host: str) -> str:
    """Pull just the host/IP out of an existing SC_HOST URL. Returns '' if it
    looks like a placeholder."""
    if not sc_host or sc_host in PLACEHOLDERS or sc_host == "https://nodeip/rest/v1":
        return ""
    rest = sc_host
    if "://" in rest:
        rest = rest.split("://", 1)[1]
    if "/" in rest:
        rest = rest.split("/", 1)[0]
    return rest


def normalize_sc_host(ip: str) -> str:
    """Wrap a bare IP/hostname into the full HyperCore REST URL form. Tolerates
    the user pasting a complete URL by stripping protocol and path."""
    s = ip.strip()
    if "://" in s:
        s = s.split("://", 1)[1]
    if "/" in s:
        s = s.split("/", 1)[0]
    return f"https://{s}/rest/v1"


# ============================================================================
# schema — every variable in .env.template, with metadata for the UI
# ============================================================================

def build_schema() -> list[Section]:
    return [
        # -------- compose --------
        Section(
            key="compose",
            title="Docker Compose",
            blurb="Project name used to namespace the container network",
            fields=[
                Field(
                    key="COMPOSE_PROJECT_NAME",
                    label="Project name",
                    default="drsproject",
                    description=(
                        "Name Docker Compose uses to prefix container, network and "
                        "volume names. All services on the stack share this network."
                    ),
                    why_default=(
                        "'drsproject' is the historical name (Distributed Resource "
                        "Scheduler) — every container/volume in production already "
                        "uses this prefix."
                    ),
                    when_change=(
                        "Only when you run multiple independent stacks on the same "
                        "host and need to keep them isolated."
                    ),
                ),
            ],
        ),

        # -------- secrets & time --------
        Section(
            key="secrets",
            title="Secrets & time",
            blurb="Encryption key for stored cluster credentials, timezone for schedules",
            fields=[
                Field(
                    key="HCM_SECRET_KEY",
                    label="Credential encryption key",
                    type="token",
                    default="CHANGE_ME",
                    must_change=True,
                    description=(
                        "HyperCore clusters are registered in the dashboard (Settings -> Clusters); "
                        "their passwords are stored in PostgreSQL encrypted with this key. Every "
                        "service that talks to a cluster receives it."
                    ),
                    why_default=(
                        "There is no safe default for a key. The installer generates a 256-bit "
                        "random value; press G to generate."
                    ),
                    when_change=(
                        "Never casually: changing it makes every stored cluster password "
                        "unreadable until re-entered. Back it up with the database."
                    ),
                ),
                Field(
                    key="TZ",
                    label="Timezone",
                    default="UTC",
                    description="IANA timezone used for scheduled jobs and log timestamps (e.g. Europe/Amsterdam).",
                    why_default="UTC is unambiguous for logs shared across sites.",
                    when_change="Set your local zone if you want 'daily at 03:00' to mean local time.",
                ),
            ],
        ),

        # -------- InfluxDB --------
        Section(
            key="influxdb",
            title="InfluxDB",
            blurb="Time-series store for cluster + VM metrics",
            fields=[
                Field(
                    key="DOCKER_INFLUXDB_INIT_USERNAME",
                    label="Admin username",
                    default="admin",
                    description=(
                        "InfluxDB superuser created on the very first container start. "
                        "Used only for the initial setup — services authenticate via "
                        "INFLUX_TOKEN afterwards."
                    ),
                    why_default=(
                        "'admin' is the InfluxDB convention and is fine here because "
                        "the port is never exposed off-host in production."
                    ),
                    when_change=(
                        "Rarely. Only matters if you'll log into the InfluxDB UI."
                    ),
                ),
                Field(
                    key="DOCKER_INFLUXDB_INIT_PASSWORD",
                    label="Admin password",
                    type="password",
                    default="CHANGE_ME",
                    must_change=True,
                    description=(
                        "Password for the InfluxDB admin user. Set on first container "
                        "start; changing it later in .env has no effect on the running "
                        "database (you'd change it via the InfluxDB CLI)."
                    ),
                    why_default=(
                        "Placeholder — must be a real strong password before first start."
                    ),
                    when_change=(
                        "Always, before the very first 'docker compose up'."
                    ),
                ),
                Field(
                    key="INFLUX_TOKEN",
                    label="API token",
                    type="token",
                    default="CHANGE_ME",
                    must_change=True,
                    description=(
                        "Long-lived API token used by Collector, Balancer, Dashboard "
                        "and Sentinel to read/write metrics. InfluxDB creates this "
                        "token at first start using exactly the value you set here."
                    ),
                    why_default=(
                        "Placeholder — must be a strong random hex string. Generator "
                        "produces 64 hex chars (256 bits)."
                    ),
                    when_change=(
                        "Always. Press G to generate. If you ever need to rotate, "
                        "you must also update it inside InfluxDB and restart all "
                        "consumer services."
                    ),
                ),
                Field(
                    key="INFLUX_ORG",
                    label="Organisation",
                    default="hypercore",
                    description=(
                        "InfluxDB organisation name. Acts as a top-level namespace "
                        "above buckets."
                    ),
                    why_default=(
                        "'hypercore' is the project convention."
                    ),
                    when_change=(
                        "Only if you're sharing the InfluxDB instance with other "
                        "applications and need to namespace this stack separately."
                    ),
                ),
                Field(
                    key="INFLUX_BUCKET",
                    label="Primary bucket",
                    default="metrics",
                    description=(
                        "Bucket holding raw high-resolution metrics from the Collector."
                    ),
                    why_default=(
                        "'metrics' — short and matches the dashboard/Flux query code."
                    ),
                    when_change=(
                        "Renaming requires editing matching values inside InfluxDB. "
                        "Don't bother unless you have a strong reason."
                    ),
                ),
                Field(
                    key="INFLUX_PRIMARY_RETENTION",
                    label="Primary retention (days)",
                    type="int",
                    default="365",
                    description=(
                        "How many days of full-resolution metrics to keep in the "
                        "primary bucket before deletion."
                    ),
                    why_default=(
                        "365 days — balances disk usage with Sentinel's need for "
                        "year-over-year seasonality detection."
                    ),
                    when_change=(
                        "Lower (90-180) on small disks; raise only if you have lots of "
                        "storage and want richer historical analytics."
                    ),
                ),
                Field(
                    key="INFLUX_LONGTERM_BUCKET",
                    label="Long-term bucket",
                    default="metrics_longterm",
                    description=(
                        "Bucket for downsampled long-term metrics (used for multi-year "
                        "trend lines)."
                    ),
                    why_default=(
                        "Standard name — matches the InfluxDB init script."
                    ),
                    when_change="Almost never.",
                ),
                Field(
                    key="INFLUX_LONGTERM_RETENTION",
                    label="Long-term retention (days)",
                    type="int",
                    default="1825",
                    description=(
                        "Retention for the downsampled bucket (5 years by default)."
                    ),
                    why_default=(
                        "1825 = 5 years, enough for capacity-planning year-over-year "
                        "comparisons."
                    ),
                    when_change=(
                        "Reduce on small disks; raise if you need a longer baseline."
                    ),
                ),
            ],
        ),

        # -------- Grafana --------
        Section(
            key="grafana",
            title="Grafana (optional)",
            blurb="Optional metrics-explorer UI — only if you run the 'tools' profile",
            profiles={PROFILE_TOOLS},
            fields=[
                Field(
                    key="GF_SECURITY_ADMIN_PASSWORD",
                    label="Admin password",
                    type="password",
                    default="CHANGE_ME",
                    must_change=True,
                    description=(
                        "Password for the Grafana 'admin' user, used to log into the "
                        "Grafana web UI on port 3000."
                    ),
                    why_default=(
                        "Placeholder — Grafana would otherwise default to 'admin/admin' "
                        "and prompt at first login."
                    ),
                    when_change=(
                        "Always, before starting the tools profile. Press G to generate."
                    ),
                    profiles={PROFILE_TOOLS},
                ),
            ],
        ),

        # -------- collector --------
        Section(
            key="collector",
            title="Collector",
            blurb="Polls HyperCore for cluster + VM metrics and writes them to InfluxDB",
            fields=[
                Field(
                    key="SC_POLL_INTERVAL",
                    label="Fast poll (sec)",
                    type="int",
                    default="30",
                    description=(
                        "How often the Collector pulls per-VM and per-node CPU/RAM "
                        "metrics from the HyperCore REST API."
                    ),
                    why_default=(
                        "30s — matches HyperCore's own internal sampling cadence; "
                        "polling faster yields no extra resolution."
                    ),
                    when_change=(
                        "Raise (60-120) on very large clusters where API calls become "
                        "expensive. Don't go below 30."
                    ),
                ),
                Field(
                    key="SC_SLOW_POLL_INTERVAL",
                    label="Slow poll (sec)",
                    type="int",
                    default="300",
                    description=(
                        "Cadence for less-volatile metadata (VM specs, node hardware)."
                    ),
                    why_default=(
                        "300s — these values change rarely (only when an admin edits "
                        "a VM), so polling more often is wasted load."
                    ),
                    when_change=(
                        "Rarely. If you script frequent VM spec changes, lower it "
                        "so the dashboard sees them faster."
                    ),
                ),
            ],
        ),

        # -------- balancer master --------
        Section(
            key="balancer_master",
            title="Balancer — Master Toggles",
            blurb="The two big switches: dry-run safety net and log verbosity",
            fields=[
                Field(
                    key="SC_DRY_RUN",
                    label="Dry run",
                    type="bool",
                    default="true",
                    description=(
                        "When true the Balancer logs every migration it WOULD perform "
                        "but never actually moves a VM. Essential safety net for the "
                        "first day or two of a new install."
                    ),
                    why_default=(
                        "true — fail-safe. A misconfigured Balancer that immediately "
                        "starts shuffling VMs is much worse than one that does nothing."
                    ),
                    when_change=(
                        "Set false ONLY after you've watched the Balancer for at least "
                        "one full reactive cycle and confirmed its proposed moves are "
                        "sensible. The dashboard 'Activity' tab shows what it would do."
                    ),
                ),
                Field(
                    key="SC_VERBOSITY",
                    label="Verbosity (1-5)",
                    type="int",
                    default="3",
                    description=(
                        "Logging detail. 1 = errors only, 3 = decisions + outcomes, "
                        "5 = every metric sample and intermediate score."
                    ),
                    why_default=(
                        "3 — enough to see why the Balancer made each call without "
                        "drowning the log."
                    ),
                    when_change=(
                        "Bump to 5 when debugging an unexpected migration. Drop to 1 "
                        "in long-term steady-state to reduce log volume."
                    ),
                ),
            ],
        ),

        # -------- balancer reactive --------
        Section(
            key="balancer_reactive",
            title="Balancer — Reactive Engine",
            blurb="Live response thresholds: when does the Balancer act NOW?",
            fields=[
                Field(
                    key="SC_AVG_WINDOW_MINUTES",
                    label="Averaging window (min)",
                    type="int",
                    default="5",
                    description=(
                        "Length of the rolling window used to smooth instantaneous "
                        "spikes before deciding to migrate."
                    ),
                    why_default=(
                        "5 minutes — long enough to ignore single-poll spikes, short "
                        "enough to react before users notice contention."
                    ),
                    when_change=(
                        "Increase if you see migrations triggered by very short bursts. "
                        "Decrease if you want faster reaction at the cost of more churn."
                    ),
                ),
                Field(
                    key="SC_SAMPLE_INTERVAL_SECONDS",
                    label="Sample interval (sec)",
                    type="int",
                    default="30",
                    description=(
                        "How often the reactive engine evaluates whether a migration "
                        "is needed."
                    ),
                    why_default=(
                        "30s — matches the Collector cadence so each cycle has fresh "
                        "data."
                    ),
                    when_change=(
                        "Match this to SC_POLL_INTERVAL — there's no benefit to "
                        "evaluating faster than data arrives."
                    ),
                ),
                Field(
                    key="SC_RAM_LIMIT_PERCENT",
                    label="RAM hard ceiling (%)",
                    type="float",
                    default="85.0",
                    description=(
                        "Absolute upper bound on per-node RAM usage. The Balancer "
                        "will never place a VM on a node where doing so would push "
                        "RAM above this."
                    ),
                    why_default=(
                        "85% — leaves enough headroom for guest OS bursts and "
                        "HyperCore's own RAM overhead without wasting capacity."
                    ),
                    when_change=(
                        "Lower (75-80) if you've seen OOM events. Raising above 90 "
                        "is risky on a 3-node cluster: a node loss may leave nowhere "
                        "to evacuate VMs."
                    ),
                ),
                Field(
                    key="SC_RAM_UPPER_THRESHOLD_PERCENT",
                    label="RAM target ceiling (%)",
                    type="float",
                    default="65.0",
                    description=(
                        "Threshold that triggers proactive rebalancing — when a node "
                        "exceeds this, the Balancer looks for a VM to evacuate even "
                        "though there's no immediate emergency."
                    ),
                    why_default=(
                        "65% — gives 20pp of headroom below the hard ceiling, room to "
                        "absorb workload growth between checks."
                    ),
                    when_change=(
                        "Tighten (55-60) for very smooth distribution; relax (70-75) "
                        "to reduce migration churn on already-balanced clusters."
                    ),
                ),
                Field(
                    key="SC_CPU_UPPER_THRESHOLD_PERCENT",
                    label="CPU target ceiling (%)",
                    type="float",
                    default="50.0",
                    description=(
                        "CPU usage above this on a node triggers reactive balancing."
                    ),
                    why_default=(
                        "50% — tuned for steady-state. CPU is far more bursty than "
                        "RAM, so the threshold is much lower than the RAM target."
                    ),
                    when_change=(
                        "Raise (60-70) on workloads that legitimately run hot. Lower "
                        "if you want the Balancer to spread work earlier."
                    ),
                ),
                Field(
                    key="SC_MAX_VCPU_RATIO",
                    label="Max vCPU:pCPU ratio",
                    type="float",
                    default="2.0",
                    description=(
                        "Upper bound on assigned vCPU count divided by physical core "
                        "count per node. The Balancer refuses migrations that would "
                        "exceed this."
                    ),
                    why_default=(
                        "2.0 — conservative. Most general-purpose workloads tolerate "
                        "2:1 oversubscription comfortably."
                    ),
                    when_change=(
                        "Lower to 1.5 for latency-sensitive workloads. Raise to 3-4 "
                        "for VDI or other bursty fleets where most VMs sit idle."
                    ),
                ),
                Field(
                    key="SC_VM_MOVE_COOLDOWN_MINUTES",
                    label="Per-VM cooldown (min)",
                    type="int",
                    default="30",
                    description=(
                        "Minimum gap between two successive migrations of the SAME "
                        "VM. Prevents ping-pong."
                    ),
                    why_default=(
                        "30 min — a VM that just moved should stay put long enough "
                        "to refute the conditions that triggered the move."
                    ),
                    when_change=(
                        "Raise (60-120) if you still see the same VM bouncing. Drop "
                        "only when debugging."
                    ),
                ),
                Field(
                    key="SC_MIGRATION_COOLDOWN_MINUTES",
                    label="Cluster cooldown (min)",
                    type="int",
                    default="5",
                    description=(
                        "Minimum gap between any two migrations cluster-wide. Limits "
                        "the rate at which the Balancer churns the cluster."
                    ),
                    why_default=(
                        "5 min — fast enough to converge during a real imbalance, "
                        "slow enough to avoid flooding HyperCore with concurrent moves."
                    ),
                    when_change=(
                        "Raise (10-15) on very large clusters where simultaneous "
                        "migrations strain the storage fabric."
                    ),
                ),
                Field(
                    key="SC_EXCLUDE_NODE_IPS",
                    label="Excluded node IPs (CSV)",
                    default="",
                    description=(
                        "Comma-separated list of node IPs that the Balancer must not "
                        "migrate VMs onto OR off of. Used to quarantine nodes during "
                        "maintenance or for nodes hosting workloads pinned by policy."
                    ),
                    why_default=(
                        "Empty — by default the Balancer treats every node as a "
                        "valid placement target."
                    ),
                    when_change=(
                        "Add the management/witness node's IP if it should never "
                        "host workload VMs. Add a node temporarily during firmware "
                        "updates to drain it."
                    ),
                ),
                Field(
                    key="SC_CPU_TARGET_TOLERANCE_PERCENT",
                    label="CPU target tolerance (%)",
                    type="float",
                    default="5.0",
                    description=(
                        "How close to the target CPU level a node must already be "
                        "before the Balancer considers it 'good enough' and stops "
                        "looking for further moves."
                    ),
                    why_default=(
                        "5% — wide enough that small drift doesn't trigger pointless "
                        "migrations, narrow enough to keep the cluster well-balanced."
                    ),
                    when_change=(
                        "Lower for very tight balancing; raise to reduce migration "
                        "frequency at the cost of looser distribution."
                    ),
                ),
            ],
        ),

        # -------- balancer predictive --------
        Section(
            key="balancer_predictive",
            title="Balancer — Predictive Engine (Oracle)",
            blurb="Forecast-driven preemptive migrations using Prophet",
            enable_field="SC_PREDICTIVE_BALANCING_ENABLED",
            fields=[
                Field(
                    key="SC_PREDICTIVE_BALANCING_ENABLED",
                    label="Predictive enabled",
                    type="bool",
                    default="true",
                    description=(
                        "Master switch for the Oracle. When false the Balancer runs "
                        "purely reactively."
                    ),
                    why_default=(
                        "true — the Oracle's whole point is to migrate BEFORE a "
                        "predictable spike. Disabling it negates a major feature."
                    ),
                    when_change=(
                        "Disable if Prophet's CPU cost is unacceptable on the host, "
                        "or while debugging unexpected predictive moves."
                    ),
                ),
                Field(
                    key="SC_PREDICTIVE_INTERVAL_SECONDS",
                    label="Run interval (sec)",
                    type="int",
                    default="43200",
                    description=(
                        "How often the Oracle runs a full forecast pass across all "
                        "VMs. Each run is CPU-heavy."
                    ),
                    why_default=(
                        "43200 (12 hours) — usage patterns don't shift faster than "
                        "this, and twice-a-day forecasting catches the next 12h "
                        "window comfortably."
                    ),
                    when_change=(
                        "Don't drop below 6 hours unless you have a lot of CPU to "
                        "spare. Raising to 24 hours is fine for stable workloads."
                    ),
                ),
                Field(
                    key="SC_PREDICTIVE_THRESHOLD",
                    label="Trigger threshold (%)",
                    type="float",
                    default="80.0",
                    description=(
                        "Predicted node usage % above which the Oracle plans a "
                        "preemptive migration."
                    ),
                    why_default=(
                        "80% — high enough that we don't move VMs for shallow "
                        "predicted spikes, low enough to act before saturation."
                    ),
                    when_change=(
                        "Lower (70) for more aggressive preemption; raise (85-90) "
                        "if you only want intervention for serious predicted spikes."
                    ),
                ),
                Field(
                    key="SC_PREDICTIVE_MIN_HISTORY_HOURS",
                    label="Min history (hours)",
                    type="int",
                    default="336",
                    description=(
                        "Minimum age of metric history required before the Oracle "
                        "will produce a forecast for a given VM."
                    ),
                    why_default=(
                        "336h = 14 days. Enough to capture 2 weekday + 2 weekend "
                        "cycles, which is the minimum Prophet needs for weekly "
                        "seasonality."
                    ),
                    when_change=(
                        "Don't go below 168 (1 week). Increase to 720 (30 days) if "
                        "you want stronger monthly seasonality."
                    ),
                ),
                Field(
                    key="SC_PREDICTIVE_LOOKBACK_DAYS",
                    label="Lookback (days)",
                    type="int",
                    default="90",
                    description=(
                        "How many days of history Prophet ingests per forecast."
                    ),
                    why_default=(
                        "90 days — captures quarterly cycles without bloating "
                        "training time."
                    ),
                    when_change=(
                        "Raise to 180-365 for better long-cycle detection (slower "
                        "training). Drop to 30 only if memory is tight."
                    ),
                ),
                Field(
                    key="SC_PREDICTIVE_MAX_WORKERS",
                    label="Max parallel workers",
                    type="int",
                    default="8",
                    description=(
                        "Number of Prophet workers run in parallel. Each is CPU-bound."
                    ),
                    why_default=(
                        "8 — reasonable on a generous host. The balancer container's "
                        "CPU limit (1.5 in docker-compose) caps actual parallelism."
                    ),
                    when_change=(
                        "MUST be <= the balancer container's cpus: limit. Lower "
                        "(2-4) on small hosts; raise on dedicated boxes."
                    ),
                ),
                Field(
                    key="SC_PREDICTIVE_LEAD_TIME_HOURS",
                    label="Lead time (hours)",
                    type="int",
                    default="1",
                    description=(
                        "How far in advance of a predicted spike the Oracle should "
                        "kick off the preemptive migration."
                    ),
                    why_default=(
                        "1 hour — enough lead time for live migration to complete "
                        "and the VM to settle, without acting unhelpfully early."
                    ),
                    when_change=(
                        "Raise (2-3) on large VMs where migration takes longer."
                    ),
                ),
                Field(
                    key="SC_PREDICTIVE_RESERVATION_MINUTES",
                    label="Reservation window (min)",
                    type="int",
                    default="5",
                    description=(
                        "After the Oracle picks a target node, that node's headroom "
                        "is reserved for this many minutes so a parallel decision "
                        "doesn't double-book it."
                    ),
                    why_default=(
                        "5 min — long enough for a single migration to commit; short "
                        "enough that stale reservations don't block subsequent moves."
                    ),
                    when_change=(
                        "Raise (15-30) if migrations on your storage fabric routinely "
                        "exceed 5 minutes."
                    ),
                ),
            ],
        ),

        # -------- auto-update paths --------
        Section(
            key="autoupdate",
            title="Auto-update paths",
            blurb="Where the dashboard finds the repo + SSH key for git pull updates",
            fields=[
                Field(
                    key="SC_REPO_PATH",
                    label="Repo path on host",
                    type="path",
                    default="/opt/hypercore-manager",
                    autofill=detect_repo_path,
                    description=(
                        "Absolute host-side path to this repository. The dashboard "
                        "container mounts it as /repo and uses it for the in-UI "
                        "'Update' button which runs git pull + docker compose."
                    ),
                    why_default=(
                        "/opt/hypercore-manager is the canonical install location."
                    ),
                    when_change=(
                        "If you cloned the repo somewhere else, set this to the "
                        "actual cwd. The installer auto-detected your current path."
                    ),
                ),
                Field(
                    key="SC_SSH_KEY_PATH",
                    label="SSH key dir on host",
                    type="path",
                    default="/root/.ssh",
                    autofill=detect_ssh_key_path,
                    description=(
                        "Host-side directory containing the SSH key used to pull "
                        "from the git remote. Mounted read-only into the dashboard "
                        "container."
                    ),
                    why_default=(
                        "/root/.ssh — if the stack runs as root, that's where its "
                        "keys live."
                    ),
                    when_change=(
                        "Set to whichever account's ~/.ssh holds the deploy key. "
                        "The installer auto-detected your home directory's .ssh."
                    ),
                ),
            ],
        ),

        # -------- dashboard --------
        Section(
            key="dashboard",
            title="Dashboard",
            blurb="Web UI on port 5000 — login, session signing, staleness threshold",
            fields=[
                Field(
                    key="SC_DASHBOARD_STALE_SECONDS",
                    label="Stale threshold (sec)",
                    type="int",
                    default="120",
                    description=(
                        "How old a metric sample can be before the dashboard marks "
                        "it as 'stale' (greyed out)."
                    ),
                    why_default=(
                        "120s — 4x the default poll interval, so the dashboard only "
                        "complains when something is actually wrong."
                    ),
                    when_change=(
                        "Match this to ~4x your SC_POLL_INTERVAL."
                    ),
                ),
                Field(
                    key="SC_DASHBOARD_USER",
                    label="Initial admin username",
                    default="admin",
                    description=(
                        "Username for the first admin account, seeded into the "
                        "users table on first start. After that, manage users from "
                        "Settings → Users in the dashboard. Leave BLANK to disable "
                        "authentication entirely (dev/local mode)."
                    ),
                    why_default=(
                        "'admin' is conventional for the first user."
                    ),
                    when_change=(
                        "Set to a real operator's name, or blank for an unauthenticated "
                        "local-only deployment."
                    ),
                ),
                Field(
                    key="SC_DASHBOARD_PASSWORD",
                    label="Initial admin password",
                    type="password",
                    default="CHANGE_ME",
                    must_change=True,
                    description=(
                        "Password for the initial admin account. Used only on the "
                        "very first dashboard start; subsequent password changes "
                        "happen in the UI."
                    ),
                    why_default=(
                        "Placeholder."
                    ),
                    when_change=(
                        "Always, before first start. Press G to generate."
                    ),
                ),
                Field(
                    key="SC_DASHBOARD_SECRET_KEY",
                    label="Session signing key",
                    type="token",
                    default="CHANGE_ME",
                    must_change=True,
                    description=(
                        "Secret used to sign session cookies. If left blank or "
                        "regenerated, every existing user session is invalidated "
                        "and everyone has to log in again."
                    ),
                    why_default=(
                        "Placeholder — the dashboard will auto-generate a random one "
                        "at startup if blank, but sessions then don't survive "
                        "container restarts."
                    ),
                    when_change=(
                        "Generate once and keep stable. Press G."
                    ),
                ),
            ],
        ),

        # -------- sentinel — PostgreSQL (always on, used by everyone) --------
        Section(
            key="postgres",
            title="PostgreSQL",
            blurb="Shared config + audit store. Password used by every service.",
            fields=[
                Field(
                    key="SENTINEL_PG_PASSWORD",
                    label="DB password",
                    type="password",
                    default="CHANGE_ME",
                    must_change=True,
                    description=(
                        "Password for the 'sentinel' Postgres role. Despite the "
                        "name, this is used by Collector, Balancer, Dashboard AND "
                        "Sentinel — every service that talks to Postgres."
                    ),
                    why_default=(
                        "Placeholder."
                    ),
                    when_change=(
                        "Always, before first start. Press G. To rotate later you "
                        "must update Postgres itself, then update .env, then restart "
                        "every consumer."
                    ),
                ),
            ],
        ),

        # -------- sentinel forecasts --------
        Section(
            key="sentinel_forecast",
            title="Sentinel — Forecast horizons",
            blurb="Which forecast windows Sentinel computes",
            profiles={PROFILE_SENTINEL},
            fields=[
                Field(
                    key="SENTINEL_FORECAST_30D_ENABLED",
                    label="30-day forecast",
                    type="bool",
                    default="true",
                    description=(
                        "Short-horizon tactical forecast. Drives the 'next month' "
                        "view in the dashboard."
                    ),
                    why_default="true — cheap, useful for routine planning.",
                    when_change="Disable only to save the (small) compute cost.",
                    profiles={PROFILE_SENTINEL},
                ),
                Field(
                    key="SENTINEL_FORECAST_90D_ENABLED",
                    label="90-day forecast",
                    type="bool",
                    default="true",
                    description="Quarterly capacity forecast.",
                    why_default="true — used by the standard quarterly capacity report.",
                    when_change="Rarely disabled.",
                    profiles={PROFILE_SENTINEL},
                ),
                Field(
                    key="SENTINEL_FORECAST_180D_ENABLED",
                    label="180-day forecast",
                    type="bool",
                    default="true",
                    description="Half-year strategic forecast.",
                    why_default="true — feeds the budget-cycle planning views.",
                    when_change="Disable if you don't use the long-horizon dashboard.",
                    profiles={PROFILE_SENTINEL},
                ),
                Field(
                    key="SENTINEL_FORECAST_365D_ENABLED",
                    label="365-day forecast",
                    type="bool",
                    default="true",
                    description="Annual forecast — most CPU-expensive horizon.",
                    why_default="true — drives annual capacity planning.",
                    when_change=(
                        "Disable on resource-constrained hosts where 180d is enough."
                    ),
                    profiles={PROFILE_SENTINEL},
                ),
            ],
        ),

        # -------- sentinel anomaly --------
        Section(
            key="sentinel_anomaly",
            title="Sentinel — Anomaly filter",
            blurb="Whether to scrub IQR outliers before forecasting",
            profiles={PROFILE_SENTINEL},
            fields=[
                Field(
                    key="SENTINEL_ANOMALY_FILTER_ENABLED",
                    label="Filter enabled",
                    type="bool",
                    default="true",
                    description=(
                        "Strip outlier samples (using interquartile range) before "
                        "feeding the series to Prophet. Reduces noise from one-off "
                        "events like reboots or backups."
                    ),
                    why_default=(
                        "true — Prophet is sensitive to large outliers, and most "
                        "operational spikes are not predictive of future load."
                    ),
                    when_change=(
                        "Disable if you're investigating Sentinel's behaviour and "
                        "want to see the raw data flow through."
                    ),
                    profiles={PROFILE_SENTINEL},
                ),
            ],
        ),

        # -------- sentinel forecast tuning --------
        Section(
            key="sentinel_forecast_tuning",
            title="Sentinel — Forecast tuning",
            blurb="Quality gate + daily aggregation for cluster-level forecasts",
            profiles={PROFILE_SENTINEL},
            fields=[
                Field(
                    key="SENTINEL_FORECAST_MIN_R2_FOR_EXHAUSTION",
                    label="Min r² for exhaustion",
                    type="float",
                    default="0.5",
                    description=(
                        "Minimum r² a linear regression must achieve before a "
                        "positive slope is allowed to produce an exhaustion date. "
                        "Suppresses spurious 'will exhaust' alerts driven by noise "
                        "on near-flat series. 0.0 disables the gate entirely."
                    ),
                    why_default=(
                        "0.5 — the linear model must explain at least 50% of the "
                        "variance, which filters out trends dominated by jitter "
                        "without being so strict that real growth gets ignored."
                    ),
                    when_change=(
                        "Raise (0.7-0.8) to suppress more borderline forecasts. "
                        "Lower if you want exhaustion alerts on noisier series."
                    ),
                    profiles={PROFILE_SENTINEL},
                ),
                Field(
                    key="SENTINEL_FORECAST_DAILY_AGGREGATE",
                    label="Daily aggregate",
                    type="choice",
                    choices=["mean", "p95", "max"],
                    default="mean",
                    description=(
                        "How sub-day samples roll up into the daily data point used "
                        "by cluster CPU / RAM / storage forecasts. "
                        "mean = time-weighted average (steady workloads). "
                        "p95 = 95th percentile (sustained peak in spiky / VDI "
                        "fleets without skew from transient spikes). "
                        "max = single highest sample (worst-case visibility)."
                    ),
                    why_default=(
                        "mean — best fit for typical mixed workloads where the "
                        "average represents the true sustained demand."
                    ),
                    when_change=(
                        "Switch to p95 in VDI / call-centre / spiky environments "
                        "where the peak matters more than the average. Use max "
                        "only if you size strictly for the worst observed sample."
                    ),
                    profiles={PROFILE_SENTINEL},
                ),
            ],
        ),

        # -------- sentinel right-sizing --------
        Section(
            key="sentinel_rightsize",
            title="Sentinel — Right-sizing thresholds",
            blurb="When does a VM count as over- or under-provisioned?",
            profiles={PROFILE_SENTINEL},
            fields=[
                Field(
                    key="SENTINEL_CPU_OVER_PROVISIONED_THRESHOLD",
                    label="Over-provisioned (%)",
                    type="float",
                    default="20.0",
                    description=(
                        "VMs whose average CPU sits below this percentage of "
                        "allocated capacity are flagged as over-provisioned."
                    ),
                    why_default=(
                        "20% — most VMs idling below 20% are clearly over-sized; "
                        "raising the bar gives noisier recommendations."
                    ),
                    when_change=(
                        "Raise (30) for a stricter cull; lower (10) to be more "
                        "conservative about recommending downsizes."
                    ),
                    profiles={PROFILE_SENTINEL},
                ),
                Field(
                    key="SENTINEL_CPU_UNDER_PROVISIONED_AVG_THRESHOLD",
                    label="Under-prov avg (%)",
                    type="float",
                    default="80.0",
                    description=(
                        "VMs whose AVERAGE CPU exceeds this are flagged as "
                        "sustained under-provisioned."
                    ),
                    why_default=(
                        "80% — at this point the VM is genuinely starving rather "
                        "than just bursty."
                    ),
                    when_change=(
                        "Lower (70) to flag VMs sooner; raise (90) for stricter "
                        "criteria."
                    ),
                    profiles={PROFILE_SENTINEL},
                ),
                Field(
                    key="SENTINEL_CPU_UNDER_PROVISIONED_P95_THRESHOLD",
                    label="Under-prov P95 (%)",
                    type="float",
                    default="90.0",
                    description=(
                        "Catches the burst-pattern case: VMs hitting >90% at the "
                        "95th percentile but lower on average."
                    ),
                    why_default=(
                        "90% — clear evidence of burst saturation rather than "
                        "occasional load."
                    ),
                    when_change=(
                        "Lower if you care about more frequent burst events."
                    ),
                    profiles={PROFILE_SENTINEL},
                ),
            ],
        ),

        # -------- sentinel analysis window --------
        Section(
            key="sentinel_window",
            title="Sentinel — Analysis window",
            blurb="History depth used by right-sizing logic",
            profiles={PROFILE_SENTINEL},
            fields=[
                Field(
                    key="SENTINEL_ANALYSIS_WINDOW_DAYS",
                    label="Window (days)",
                    type="int",
                    default="14",
                    description=(
                        "Days of history used for right-sizing analysis."
                    ),
                    why_default=(
                        "14 — covers two full week cycles (weekdays + weekends), "
                        "the minimum to be confident a pattern is real."
                    ),
                    when_change=(
                        "Raise (28-30) for more conservative sizing recommendations. "
                        "Don't go below 14."
                    ),
                    profiles={PROFILE_SENTINEL},
                ),
                Field(
                    key="SENTINEL_RECURRING_SPIKE_MIN_OCCURRENCES",
                    label="Recurring spike threshold",
                    type="int",
                    default="3",
                    description=(
                        "How many times a CPU spike must recur within the analysis "
                        "window before Sentinel calls it a 'pattern' rather than "
                        "a one-off event."
                    ),
                    why_default=(
                        "3 — the classic minimum for pattern recognition."
                    ),
                    when_change=(
                        "Raise (5-7) to require stronger evidence; lower (2) for "
                        "earlier detection at the cost of more false positives."
                    ),
                    profiles={PROFILE_SENTINEL},
                ),
            ],
        ),

        # -------- sentinel retention --------
        Section(
            key="sentinel_retention",
            title="Sentinel — Data retention",
            blurb="How long to keep recommendations + analysis history in Postgres",
            profiles={PROFILE_SENTINEL},
            fields=[
                Field(
                    key="SENTINEL_RETENTION_MONTHS",
                    label="Retention (months)",
                    type="int",
                    default="12",
                    description=(
                        "Months of recommendation/analysis records kept in Postgres "
                        "before the housekeeping job deletes them."
                    ),
                    why_default=(
                        "12 — supports year-over-year comparisons and audit lookups "
                        "without unbounded growth."
                    ),
                    when_change=(
                        "Raise (24-36) for longer audit history; lower (6) on small "
                        "Postgres volumes."
                    ),
                    profiles={PROFILE_SENTINEL},
                ),
            ],
        ),

        # -------- sentinel intervals --------
        Section(
            key="sentinel_intervals",
            title="Sentinel — Collection intervals",
            blurb="How often Sentinel runs its collection / analysis / pruning loops",
            profiles={PROFILE_SENTINEL},
            fields=[
                Field(
                    key="SENTINEL_COLLECTION_INTERVAL_SECONDS",
                    label="Collection (sec)",
                    type="int",
                    default="300",
                    description=(
                        "How often Sentinel pulls real-time metrics from InfluxDB "
                        "for its analysis pipeline."
                    ),
                    why_default=(
                        "300s — 5 minutes is the classic monitoring cadence for "
                        "metrics that don't need sub-minute granularity."
                    ),
                    when_change=(
                        "Lower (120) if dashboard freshness matters more than load. "
                        "Don't go below SC_POLL_INTERVAL."
                    ),
                    profiles={PROFILE_SENTINEL},
                ),
                Field(
                    key="SENTINEL_TREND_INTERVAL_SECONDS",
                    label="Trend analysis (sec)",
                    type="int",
                    default="3600",
                    description=(
                        "How often Sentinel runs trend + forecast analysis. Each "
                        "run is much heavier than collection."
                    ),
                    why_default=(
                        "3600 (1 hour) — forecasts don't change meaningfully on "
                        "shorter timescales."
                    ),
                    when_change=(
                        "Raise to 7200-14400 on busy hosts; rarely worth lowering."
                    ),
                    profiles={PROFILE_SENTINEL},
                ),
                Field(
                    key="SENTINEL_VM_PRUNE_INTERVAL_SECONDS",
                    label="VM prune (sec)",
                    type="int",
                    default="900",
                    description=(
                        "How often Sentinel checks for VMs that no longer appear in "
                        "InfluxDB (deleted from cluster) and removes their stale "
                        "Postgres records. The 'active' window for this check is "
                        "always 2x this value, so brief Collector hiccups never "
                        "trigger a false purge."
                    ),
                    why_default=(
                        "900s (15 min) — frequent enough to keep the dashboard "
                        "tidy, slow enough that hiccups don't cause false purges."
                    ),
                    when_change=(
                        "Live-tunable from the dashboard. Raise if you have "
                        "frequent transient Collector outages."
                    ),
                    profiles={PROFILE_SENTINEL},
                ),
            ],
        ),

        # -------- sentinel API --------
        Section(
            key="sentinel_api",
            title="Sentinel — REST API",
            blurb="Bearer-token auth for the Sentinel REST API on port 8081",
            profiles={PROFILE_SENTINEL},
            fields=[
                Field(
                    key="SENTINEL_API_TOKEN",
                    label="API bearer token",
                    type="token",
                    default="CHANGE_ME",
                    must_change=True,
                    description=(
                        "Bearer token required to call the Sentinel REST API on "
                        "port 8081. The dashboard uses it via SENTINEL_API_URL. "
                        "Leave EMPTY to disable auth (dev/internal use only)."
                    ),
                    why_default=(
                        "Placeholder — must be replaced with a strong token, OR "
                        "explicitly emptied for an unauthenticated deployment."
                    ),
                    when_change=(
                        "Always for production. Press G to generate."
                    ),
                    profiles={PROFILE_SENTINEL},
                ),
            ],
        ),

        # -------- sentinel outcome tracker --------
        Section(
            key="sentinel_outcome",
            title="Sentinel — Outcome tracker",
            blurb="Did right-sizing recommendations actually get acted on?",
            profiles={PROFILE_SENTINEL},
            enable_field="SENTINEL_OUTCOME_TRACKER_ENABLED",
            fields=[
                Field(
                    key="SENTINEL_OUTCOME_TRACKER_ENABLED",
                    label="Tracker enabled",
                    type="bool",
                    default="true",
                    description=(
                        "Re-checks each recommendation against current VM specs "
                        "after a delay, so the dashboard can show 'recommendation "
                        "applied' / 'ignored' status."
                    ),
                    why_default=(
                        "true — closes the feedback loop between recommendation "
                        "and reality at negligible cost."
                    ),
                    when_change=(
                        "Disable if you find the metric noisy or untrustworthy."
                    ),
                    profiles={PROFILE_SENTINEL},
                ),
                Field(
                    key="SENTINEL_OUTCOME_CHECK_DELAY_HOURS",
                    label="Check delay (hours)",
                    type="int",
                    default="48",
                    description=(
                        "Hours to wait after a recommendation before checking "
                        "whether it was applied."
                    ),
                    why_default=(
                        "48 — gives operators a full business cycle (often a change "
                        "window) to act."
                    ),
                    when_change=(
                        "Shorten for quick-feedback environments; lengthen if your "
                        "change windows are weekly."
                    ),
                    profiles={PROFILE_SENTINEL},
                ),
                Field(
                    key="SENTINEL_OUTCOME_CHECK_INTERVAL_SECONDS",
                    label="Check interval (sec)",
                    type="int",
                    default="21600",
                    description=(
                        "How often the outcome tracker scans for recommendations "
                        "ready to be re-checked."
                    ),
                    why_default=(
                        "21600 (6 hours) — light cadence appropriate for a multi-day "
                        "delay."
                    ),
                    when_change=(
                        "Match to your check delay. Faster won't speed anything up "
                        "if delay is 48 hours."
                    ),
                    profiles={PROFILE_SENTINEL},
                ),
            ],
        ),

        # -------- sentinel webhooks --------
        Section(
            key="sentinel_webhooks",
            title="Sentinel — Webhooks",
            blurb="Outbound POST notifications for new recommendations / alerts",
            profiles={PROFILE_SENTINEL},
            enable_field="SENTINEL_WEBHOOK_URL",
            fields=[
                Field(
                    key="SENTINEL_WEBHOOK_URL",
                    label="Target URL",
                    default="",
                    description=(
                        "Target URL for outbound webhook POST requests. Leave EMPTY "
                        "to disable webhooks entirely."
                    ),
                    why_default=(
                        "Empty — webhooks are off by default; you have to opt in."
                    ),
                    when_change=(
                        "Set to a Slack/Teams/PagerDuty/custom-receiver URL when "
                        "you want push alerts."
                    ),
                    profiles={PROFILE_SENTINEL},
                ),
                Field(
                    key="SENTINEL_WEBHOOK_SECRET",
                    label="HMAC signing secret",
                    type="token",
                    default="CHANGE_ME",
                    must_change=True,
                    description=(
                        "HMAC-SHA256 signing secret. Recipients use this to verify "
                        "request authenticity. Leave EMPTY to send unsigned requests "
                        "(not recommended for production)."
                    ),
                    why_default=(
                        "Placeholder — set if webhooks are enabled."
                    ),
                    when_change=(
                        "Generate once, share with the receiver, keep stable."
                    ),
                    profiles={PROFILE_SENTINEL},
                ),
                Field(
                    key="SENTINEL_WEBHOOK_ENABLED",
                    label="Enabled flag",
                    type="bool",
                    default="true",
                    description=(
                        "Master enable/disable switch. Still requires URL to be set."
                    ),
                    why_default="true — the URL field is the real gate.",
                    when_change=(
                        "Set false to silence webhooks temporarily without losing "
                        "the URL/secret config."
                    ),
                    profiles={PROFILE_SENTINEL},
                ),
                Field(
                    key="SENTINEL_WEBHOOK_TIMEOUT_SECONDS",
                    label="Timeout (sec)",
                    type="int",
                    default="10",
                    description="HTTP timeout per delivery attempt.",
                    why_default="10s — handles slow receivers without hanging the dispatcher.",
                    when_change="Raise if your receiver is sluggish.",
                    profiles={PROFILE_SENTINEL},
                ),
                Field(
                    key="SENTINEL_WEBHOOK_MAX_RETRIES",
                    label="Max retries",
                    type="int",
                    default="3",
                    description="Maximum delivery attempts before marking as dead_letter.",
                    why_default="3 — covers transient network blips, gives up before backlog grows.",
                    when_change="Raise (5-10) for unreliable receivers.",
                    profiles={PROFILE_SENTINEL},
                ),
                Field(
                    key="SENTINEL_WEBHOOK_LOOKBACK_HOURS",
                    label="Startup lookback (hours)",
                    type="int",
                    default="24",
                    description=(
                        "On startup, how far back to look for undelivered events. "
                        "Prevents replaying a huge backlog after an extended outage."
                    ),
                    why_default="24 — replays a day of missed alerts but no more.",
                    when_change=(
                        "Lower if a long outage would otherwise replay alerts you'd "
                        "rather forget."
                    ),
                    profiles={PROFILE_SENTINEL},
                ),
                Field(
                    key="SENTINEL_WEBHOOK_DISPATCH_INTERVAL_SECONDS",
                    label="Dispatch interval (sec)",
                    type="int",
                    default="60",
                    description="How often the webhook dispatch loop wakes up.",
                    why_default="60 — once a minute is fine for most alert volumes.",
                    when_change="Lower for near-real-time alerts; raise to batch deliveries.",
                    profiles={PROFILE_SENTINEL},
                ),
            ],
        ),
    ]


# ============================================================================
# .env file IO
# ============================================================================

def parse_env(path: Path) -> dict[str, str]:
    """Parse a bash-style .env file into a dict. Tolerant of quotes and comments."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        values[key] = val
    return values


def render_env(sections: list[Section], values: dict[str, str], profiles: set[str]) -> str:
    """Render the .env file body as a string, in section order."""
    lines = [
        "# =============================================================================",
        "# HyperCore Manager — generated by the installer",
        f"# Generated at {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "# Do not commit this file to version control.",
        "# =============================================================================",
        "",
    ]
    for section in sections:
        if not (section.profiles & profiles):
            continue
        lines.append(f"# --- {section.title} ---")
        for f in section.fields:
            if not (f.profiles & profiles):
                continue
            val = values.get(f.key, f.default)
            if val is None:
                val = ""
            lines.append(f"{f.key}={val}")
        lines.append("")
    return "\n".join(lines) + "\n"


def write_env(path: Path, sections: list[Section], values: dict[str, str], profiles: set[str]) -> Optional[Path]:
    """Render and write .env. Backs up any existing file. Returns the backup path
    (or None if there wasn't one)."""
    backup: Optional[Path] = None
    if path.exists():
        ts = time.strftime("%Y%m%d-%H%M%S")
        backup = path.with_suffix(path.suffix + f".bak.{ts}")
        shutil.copy2(path, backup)

    path.write_text(render_env(sections, values, profiles), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return backup


# ============================================================================
# validation
# ============================================================================

def validate_value(field_def: Field, value: str) -> Optional[str]:
    """Return None if OK, else an error message."""
    if field_def.type == "int":
        try:
            int(value)
        except ValueError:
            return "Must be an integer."
    elif field_def.type == "float":
        try:
            float(value)
        except ValueError:
            return "Must be a number."
    elif field_def.type == "bool":
        if value.lower() not in ("true", "false"):
            return "Must be 'true' or 'false'."
    elif field_def.type == "choice":
        if field_def.choices and value not in field_def.choices:
            return f"Must be one of: {', '.join(field_def.choices)}"
    return None


def is_section_gated_off(section: Section, values: dict[str, str]) -> bool:
    """True when section.enable_field is set and currently empty/false."""
    if not section.enable_field:
        return False
    ev = values.get(section.enable_field, "")
    ef = next((f for f in section.fields if f.key == section.enable_field), None)
    if ef and ef.type == "bool" and ev.lower() == "false":
        return True
    if ef and ef.type != "bool" and not ev:
        return True
    return False


def section_status(section: Section, values: dict[str, str], profiles: set[str]) -> str:
    """Returns 'ok', 'warn' (placeholder remains), or 'skip' (profile not selected)."""
    if not (section.profiles & profiles):
        return "skip"
    if is_section_gated_off(section, values):
        return "ok"
    for f in section.fields:
        if not (f.profiles & profiles):
            continue
        v = values.get(f.key, f.default)
        if f.must_change and v in PLACEHOLDERS:
            return "warn"
        err = validate_value(f, v)
        if err:
            return "warn"
    return "ok"


def collect_validation_errors(sections: list[Section], values: dict[str, str], profiles: set[str]) -> list[str]:
    """Return human-readable errors for must_change placeholders + invalid values.
    Skips sections gated off by their enable_field."""
    errors: list[str] = []
    for s in sections:
        if not (s.profiles & profiles):
            continue
        if is_section_gated_off(s, values):
            continue
        for f in s.fields:
            if not (f.profiles & profiles):
                continue
            v = values.get(f.key, f.default)
            if f.must_change and v in PLACEHOLDERS:
                errors.append(f"  {f.key}: placeholder '{v}' must be replaced")
            err = validate_value(f, v)
            if err:
                errors.append(f"  {f.key}: {err}")
    return errors


def collect_path_warnings(
    sections: list[Section],
    values: dict[str, str],
    profiles: set[str],
    skip_keys: set[str] = frozenset(),
) -> list[str]:
    """Return warnings for type=path fields whose value doesn't exist on disk."""
    warnings: list[str] = []
    for s in sections:
        if not (s.profiles & profiles):
            continue
        for f in s.fields:
            if f.type != "path" or f.key in skip_keys:
                continue
            v = values.get(f.key, f.default)
            if v and not path_exists(v):
                warnings.append(f"  {f.key} = {v}  (does not exist on this host)")
    return warnings


# ============================================================================
# easy-mode autogen
# ============================================================================

def apply_easy_autogen(sections: list[Section], values: dict[str, str], profiles: set[str]) -> None:
    """Auto-generate every must_change secret/token still holding a placeholder
    in the active profile set, skipping the four operator-supplied keys and
    any section gated off by its enable_field."""
    for s in sections:
        if not (s.profiles & profiles):
            continue
        if is_section_gated_off(s, values):
            continue
        for f in s.fields:
            if not (f.profiles & profiles):
                continue
            if f.key in EASY_REQUIRED_KEYS or not f.must_change:
                continue
            v = values.get(f.key, f.default)
            if v not in PLACEHOLDERS:
                continue
            values[f.key] = gen_token(32) if f.type == "token" else gen_password(24)


def autofill_paths(sections: list[Section], values: dict[str, str]) -> None:
    """For every path field with an autofill function, replace empty/default
    values with the detected one."""
    for s in sections:
        for f in s.fields:
            if f.autofill and values.get(f.key, "") in ("", f.default):
                detected = f.autofill()
                if detected:
                    values[f.key] = detected


def seed_default_values(sections: list[Section]) -> dict[str, str]:
    """Build the starting values dict from each field's default."""
    values: dict[str, str] = {}
    for s in sections:
        for f in s.fields:
            values[f.key] = f.default
    return values
