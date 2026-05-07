"""HyperCore Manager — Windows deploy wizard.

Tk GUI that provisions a fresh Ubuntu VM on a Scale Computing HyperCore
cluster, drops the hypercore-manager stack onto it via cloud-init, and
boots it. Easy mode collects four inputs and autogenerates everything
else; advanced mode exposes VM sizing, networking, and a free-form .env
override.

PyInstaller entry point — see installer/windows/build.py.
"""

from __future__ import annotations

import io
import os
import queue
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Callable, Optional

# ---------------------------------------------------------------------------
# Path bootstrap so this file works from a PyInstaller bundle, the repo root,
# or `python -m installer.windows.wizard`.
# ---------------------------------------------------------------------------
_THIS = Path(__file__).resolve()
_REPO = _THIS.parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

# When frozen by PyInstaller, the bundled repo tarball lives next to the
# extracted scripts under sys._MEIPASS / "payload" / "repo.tar.gz". Outside
# the bundle (dev runs) we'll build the tarball from the live repo.
_BUNDLED_PAYLOAD: Optional[Path] = None
_ICON_PATH: Optional[Path] = None
if getattr(sys, "frozen", False):
    _meipass = Path(getattr(sys, "_MEIPASS", ""))  # type: ignore[arg-type]
    candidate = _meipass / "payload" / "repo.tar.gz"
    if candidate.exists():
        _BUNDLED_PAYLOAD = candidate
    icon_candidate = _meipass / "installer" / "favicon.ico"
    if icon_candidate.exists():
        _ICON_PATH = icon_candidate
else:
    icon_candidate = _REPO / "installer" / "favicon.ico"
    if icon_candidate.exists():
        _ICON_PATH = icon_candidate

from installer.core import (  # noqa: E402  (sys.path bootstrap above)
    EASY_PROFILES,
    EASY_REQUIRED_KEYS,
    PLACEHOLDERS,
    PROFILE_CORE,
    PROFILE_SENTINEL,
    PROFILE_TOOLS,
    Field as SchemaField,
    Section as SchemaSection,
    apply_easy_autogen,
    build_schema,
    extract_host_ip,
    gen_password,
    gen_token,
    is_section_gated_off,
    looks_like_host,
    normalize_sc_host,
    parse_env,
    render_env,
    seed_default_values,
    section_status,
    validate_value,
)
from installer.windows.cloudinit import (  # noqa: E402
    DEFAULT_REPO_DEST,
    NetworkConfig,
    build_repo_tarball,
    make_instance_id,
    pad_to_sector,
    render_meta_data,
    render_user_data,
)
from installer.windows.hypercore import HyperCoreClient, HyperCoreError  # noqa: E402
from installer.windows.image_cache import (  # noqa: E402
    DEFAULT_IMAGE_NAME,
    DEFAULT_IMAGE_URL,
    DEFAULT_SHA256_URL,
    ImageCacheError,
    ensure_image,
)


# =============================================================================
# Wizard state
# =============================================================================

@dataclass
class WizardState:
    mode: str = "easy"  # 'easy' | 'advanced'

    # HyperCore cluster
    cluster_ip: str = ""
    cluster_user: str = "admin"
    cluster_pass: str = ""
    verify_tls: bool = False

    # VM
    vm_name: str = "hypercore-manager"
    vcpus: int = 4
    mem_gib: int = 8
    disk_gib: int = 80
    vlan: int = 0
    timezone: str = "UTC"

    # Network
    net_mode: str = "dhcp"  # 'dhcp' | 'static'
    net_address_cidr: str = ""
    net_gateway: str = ""
    net_dns: str = "8.8.8.8, 1.1.1.1"

    # Linux user
    linux_user: str = "hcm"
    linux_pass: str = ""
    ssh_pubkey: str = ""

    # Dashboard credential (autogen by default; settable on the advanced page)
    dashboard_pass: str = ""

    # Profile + values are the single source of truth for the .env we'll write.
    # Easy mode populates these from defaults+autogen. Advanced mode also lets
    # the operator edit them directly via AdvancedSettingsPage.
    profiles: set[str] = field(default_factory=lambda: set(EASY_PROFILES))
    values: dict[str, str] = field(default_factory=dict)
    schema: list[SchemaSection] = field(default_factory=list)
    advanced_initialized: bool = False


# =============================================================================
# Helpers
# =============================================================================

PAD = {"padx": 12, "pady": 6}


def _sanitize_hostname(name: str) -> str:
    """RFC 1123-ish hostname: a-z 0-9 hyphens, max 63 chars, can't start/end with -."""
    out = "".join(c.lower() if c.isalnum() else "-" for c in name)
    out = out.strip("-")
    out = "".join(c for c in out if c.isalnum() or c == "-")
    return out[:63] or "hypercore-manager"


def _humanize_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _is_ipv4(s: str) -> bool:
    parts = s.split(".")
    if len(parts) != 4:
        return False
    for p in parts:
        if not p.isdigit() or len(p) > 3:
            return False
        if not (0 <= int(p) <= 255):
            return False
    return True


def _is_ipv4_cidr(s: str) -> bool:
    if "/" not in s:
        return False
    addr, _, prefix = s.partition("/")
    if not _is_ipv4(addr):
        return False
    if not prefix.isdigit():
        return False
    n = int(prefix)
    return 0 <= n <= 32


def _parse_dns(s: str) -> list[str]:
    return [item.strip() for item in s.replace(";", ",").split(",") if item.strip()]


class _ConversionHeartbeat:
    """Background ticker that logs '...still converting (Ns elapsed)' every
    `interval` seconds while a server-side operation blocks the wizard.

    Used during HyperCore's qcow2-conversion phase: the upload's bytes-sent
    counter reaches 100 %, then the wizard sits on the HTTP response for
    minutes while the cluster imports the image. Without this heartbeat the
    wizard window looks frozen.
    """

    def __init__(self, log_fn: Callable[[str], None], label: str, interval: float = 30.0) -> None:
        self._log = log_fn
        self._label = label
        self._interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._started_at: float = 0.0

    def start(self) -> None:
        if self._thread is not None:
            return
        self._started_at = time.monotonic()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"hb-{self._label}",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            elapsed = int(time.monotonic() - self._started_at)
            self._log(f"  {self._label}: still converting ({elapsed}s)")


# =============================================================================
# Page framework
# =============================================================================

class WizardPage(ttk.Frame):
    title: str = "Page"
    subtitle: str = ""

    def __init__(self, master: "Wizard", state: WizardState) -> None:
        super().__init__(master)
        self.state = state
        self.wizard = master

    def on_show(self) -> None:
        """Called every time the page becomes visible. Override to refresh widgets."""

    def on_next(self) -> bool:
        """True = proceed; False = stay (e.g. validation failed)."""
        return True

    def on_back(self) -> bool:
        return True


class Wizard(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("HyperCore Manager — Deploy Wizard")
        self.geometry("980x680")
        self.minsize(860, 600)

        if _ICON_PATH:
            # `default=` makes the icon apply to child Toplevels (dialogs) too.
            try:
                self.iconbitmap(default=str(_ICON_PATH))
            except tk.TclError:
                # Some platforms / Tcl builds reject .ico — fall back to silent skip.
                pass

        try:
            ttk.Style().theme_use("vista")
        except tk.TclError:
            pass

        self.state_obj = WizardState()
        self.pages: list[WizardPage] = []
        self.idx = 0

        # Title strip
        self.header = ttk.Frame(self)
        self.header.pack(fill="x")
        self.header_title = ttk.Label(self.header, font=("Segoe UI", 14, "bold"))
        self.header_title.pack(anchor="w", padx=16, pady=(12, 0))
        self.header_subtitle = ttk.Label(self.header, foreground="#555")
        self.header_subtitle.pack(anchor="w", padx=16, pady=(0, 8))

        ttk.Separator(self).pack(fill="x")

        self.body = ttk.Frame(self)
        self.body.pack(fill="both", expand=True)

        # Footer
        ttk.Separator(self).pack(fill="x")
        self.footer = ttk.Frame(self)
        self.footer.pack(fill="x", side="bottom")
        self.btn_back = ttk.Button(self.footer, text="< Back", command=self.go_back)
        self.btn_back.pack(side="left", padx=12, pady=10)
        self.btn_next = ttk.Button(self.footer, text="Next >", command=self.go_next)
        self.btn_next.pack(side="right", padx=12, pady=10)
        self.btn_cancel = ttk.Button(self.footer, text="Cancel", command=self._on_close)
        self.btn_cancel.pack(side="right", pady=10)

        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_pages()
        self._show(0)

    # ----- Page assembly -----

    def _build_pages(self) -> None:
        st = self.state_obj
        self.pages = [
            WelcomePage(self, st),
            ModePage(self, st),
            ConnectionPage(self, st),
            VMPage(self, st),                   # shown for both, with extras hidden in easy
            AdvancedSettingsPage(self, st),     # shown only in advanced mode
            ReviewPage(self, st),
            DeployPage(self, st),
        ]
        for p in self.pages:
            p.place(in_=self.body, x=0, y=0, relwidth=1, relheight=1)
            p.lower()

    def _visible_pages(self) -> list[int]:
        """Indices of pages that are reachable given the current mode."""
        idxs = []
        for i, p in enumerate(self.pages):
            if isinstance(p, AdvancedSettingsPage) and self.state_obj.mode != "advanced":
                continue
            idxs.append(i)
        return idxs

    def _show(self, idx: int) -> None:
        self.idx = idx
        page = self.pages[idx]
        page.lift()
        self.header_title.configure(text=page.title)
        self.header_subtitle.configure(text=page.subtitle)
        page.on_show()

        visible = self._visible_pages()
        pos = visible.index(idx)
        self.btn_back.state(["disabled"] if pos == 0 else ["!disabled"])
        if isinstance(page, DeployPage):
            self.btn_back.state(["disabled"])
            self.btn_next.configure(text="Finish", command=page.on_finish_click)
            self.btn_next.state(["disabled"])  # enabled when deploy completes
        elif isinstance(page, ReviewPage):
            self.btn_next.configure(text="Deploy", command=self.go_next)
            self.btn_next.state(["!disabled"])
        else:
            self.btn_next.configure(text="Next >", command=self.go_next)
            self.btn_next.state(["!disabled"])

    def go_next(self) -> None:
        if not self.pages[self.idx].on_next():
            return
        visible = self._visible_pages()
        pos = visible.index(self.idx)
        if pos + 1 < len(visible):
            self._show(visible[pos + 1])

    def go_back(self) -> None:
        if not self.pages[self.idx].on_back():
            return
        visible = self._visible_pages()
        pos = visible.index(self.idx)
        if pos > 0:
            self._show(visible[pos - 1])

    def _on_close(self) -> None:
        page = self.pages[self.idx]
        if isinstance(page, DeployPage) and page.is_running():
            if not messagebox.askyesno("Cancel", "Deploy is in progress. Quit anyway?"):
                return
        self.destroy()


# =============================================================================
# Pages
# =============================================================================

class WelcomePage(WizardPage):
    title = "HyperCore Manager — Deploy Wizard"
    subtitle = "Provision a fresh VM with the full stack via cloud-init"

    def __init__(self, master: Wizard, state: WizardState) -> None:
        super().__init__(master, state)
        msg = (
            "This wizard will:\n"
            "    1.  Connect to your HyperCore cluster.\n"
            "    2.  Upload an Ubuntu 24.04 cloud image (cached after the first run).\n"
            "    3.  Create a new VM and inject cloud-init userData containing the\n"
            "         hypercore-manager repo, a populated .env, and a first-boot\n"
            "         script that installs Docker and starts the stack.\n"
            "    4.  Power the VM on.\n\n"
            "When the bootstrap finishes the dashboard is reachable on port 5000\n"
            "of the VM's IP.\n\n"
            "You can re-run this wizard against the same cluster to deploy more\n"
            "VMs (give each a unique name)."
        )
        ttk.Label(self, text=msg, justify="left").pack(anchor="w", **PAD)


class ModePage(WizardPage):
    title = "Choose installation mode"
    subtitle = "Easy: 4 inputs, autogen everything else.  Advanced: full control."

    def __init__(self, master: Wizard, state: WizardState) -> None:
        super().__init__(master, state)
        self.var = tk.StringVar(value=state.mode)

        easy = ttk.Radiobutton(
            self, text="Easy mode  (recommended)", variable=self.var, value="easy",
            command=self._sync,
        )
        easy.pack(anchor="w", **PAD)
        ttk.Label(self, foreground="#555", justify="left",
                  text="    HyperCore IP, credentials, VM name, Linux password.\n"
                       "    Strong secrets generated automatically; sensible defaults\n"
                       "    for VM size, network, retention, etc.").pack(anchor="w", padx=12)

        adv = ttk.Radiobutton(
            self, text="Advanced mode", variable=self.var, value="advanced",
            command=self._sync,
        )
        adv.pack(anchor="w", padx=12, pady=(20, 6))
        ttk.Label(self, foreground="#555", justify="left",
                  text="    Same as easy, plus: VM vCPU/RAM/disk size, VLAN tag,\n"
                       "    and a category-by-category editor for every value in .env\n"
                       "    (mirrors the curses TUI's advanced flow, with field-level\n"
                       "    explanations).").pack(anchor="w", padx=12)

    def _sync(self) -> None:
        self.state.mode = self.var.get()


class ConnectionPage(WizardPage):
    title = "HyperCore cluster"
    subtitle = "REST API connection — IP only, the wizard adds https:// and /rest/v1"

    def __init__(self, master: Wizard, state: WizardState) -> None:
        super().__init__(master, state)
        grid = ttk.Frame(self)
        grid.pack(fill="x", **PAD)
        grid.columnconfigure(1, weight=1)

        ttk.Label(grid, text="Cluster IP").grid(row=0, column=0, sticky="w", pady=4)
        self.ip_var = tk.StringVar(value=state.cluster_ip)
        ttk.Entry(grid, textvariable=self.ip_var).grid(row=0, column=1, sticky="ew")

        ttk.Label(grid, text="Username").grid(row=1, column=0, sticky="w", pady=4)
        self.user_var = tk.StringVar(value=state.cluster_user)
        ttk.Entry(grid, textvariable=self.user_var).grid(row=1, column=1, sticky="ew")

        ttk.Label(grid, text="Password").grid(row=2, column=0, sticky="w", pady=4)
        self.pw_var = tk.StringVar(value=state.cluster_pass)
        self.pw_entry = ttk.Entry(grid, textvariable=self.pw_var, show="*")
        self.pw_entry.grid(row=2, column=1, sticky="ew")

        self.show_pw = tk.IntVar()
        ttk.Checkbutton(grid, text="Show", variable=self.show_pw,
                        command=self._toggle_pw).grid(row=2, column=2, padx=(8, 0))

        self.tls_var = tk.IntVar(value=int(state.verify_tls))
        ttk.Checkbutton(grid, text="Verify TLS certificate (off for self-signed)",
                        variable=self.tls_var).grid(row=3, column=1, sticky="w", pady=8)

        # Test button + status
        action = ttk.Frame(self)
        action.pack(fill="x", **PAD)
        self.test_btn = ttk.Button(action, text="Test connection", command=self._test)
        self.test_btn.pack(side="left")
        self.test_status = ttk.Label(action, text="", foreground="#555")
        self.test_status.pack(side="left", padx=12)

    def _toggle_pw(self) -> None:
        self.pw_entry.configure(show="" if self.show_pw.get() else "*")

    def _commit(self) -> None:
        self.state.cluster_ip = self.ip_var.get().strip()
        self.state.cluster_user = self.user_var.get().strip()
        self.state.cluster_pass = self.pw_var.get()
        self.state.verify_tls = bool(self.tls_var.get())

    def _test(self) -> None:
        self._commit()
        ip = self.state.cluster_ip
        if not ip or not looks_like_host(extract_host_ip(ip) or ip):
            self.test_status.configure(text="Enter a valid IP first.", foreground="#a00")
            return
        host = normalize_sc_host(ip)
        self.test_btn.state(["disabled"])
        self.test_status.configure(text="Connecting…", foreground="#555")
        self.update_idletasks()
        try:
            client = HyperCoreClient(host, verify_tls=self.state.verify_tls, timeout=10)
            client.login(self.state.cluster_user, self.state.cluster_pass)
            self.test_status.configure(text="OK — credentials accepted.", foreground="#080")
        except HyperCoreError as e:
            self.test_status.configure(text=f"Failed: {e}", foreground="#a00")
        except Exception as e:  # network errors, TLS, etc.
            self.test_status.configure(text=f"Failed: {e}", foreground="#a00")
        finally:
            self.test_btn.state(["!disabled"])

    def on_next(self) -> bool:
        self._commit()
        host_only = extract_host_ip(self.state.cluster_ip) or self.state.cluster_ip
        if not host_only or not looks_like_host(host_only):
            messagebox.showwarning("Cluster IP", "Please enter a valid IP or hostname.")
            return False
        if not self.state.cluster_user.strip():
            messagebox.showwarning("Username", "Please enter a username.")
            return False
        if not self.state.cluster_pass:
            messagebox.showwarning("Password", "Please enter a password.")
            return False
        # Normalise the IP into URL form so downstream code can use it directly.
        self.state.cluster_ip = host_only
        return True


class VMPage(WizardPage):
    title = "VM, Linux user, and network"
    subtitle = "Identity, the local Linux account, and how the VM gets on the network"

    def __init__(self, master: Wizard, state: WizardState) -> None:
        super().__init__(master, state)
        grid = ttk.Frame(self)
        grid.pack(fill="x", **PAD)
        grid.columnconfigure(1, weight=1)

        # --- Always visible: identity + Linux account ---
        ttk.Label(grid, text="VM name").grid(row=0, column=0, sticky="w", pady=4)
        self.name_var = tk.StringVar(value=state.vm_name)
        ttk.Entry(grid, textvariable=self.name_var).grid(row=0, column=1, sticky="ew", columnspan=2)

        ttk.Label(grid, text="Linux user").grid(row=1, column=0, sticky="w", pady=4)
        self.user_var = tk.StringVar(value=state.linux_user)
        ttk.Entry(grid, textvariable=self.user_var).grid(row=1, column=1, sticky="ew", columnspan=2)

        ttk.Label(grid, text="Linux password").grid(row=2, column=0, sticky="w", pady=4)
        self.pw_var = tk.StringVar(value=state.linux_pass)
        self.pw_entry = ttk.Entry(grid, textvariable=self.pw_var, show="*")
        self.pw_entry.grid(row=2, column=1, sticky="ew")
        ttk.Button(grid, text="Generate", command=self._gen_pw, width=10).grid(row=2, column=2, padx=(8, 0))

        self.show_pw = tk.IntVar()
        ttk.Checkbutton(grid, text="Show password", variable=self.show_pw,
                        command=self._toggle_pw).grid(row=3, column=1, sticky="w")

        ttk.Label(grid, text="SSH public key (optional)").grid(row=4, column=0, sticky="w", pady=(12, 4))
        ssh_frame = ttk.Frame(grid)
        ssh_frame.grid(row=4, column=1, columnspan=2, sticky="ew", pady=(12, 4))
        ssh_frame.columnconfigure(0, weight=1)
        self.ssh_var = tk.StringVar(value=state.ssh_pubkey)
        ttk.Entry(ssh_frame, textvariable=self.ssh_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(ssh_frame, text="Browse…", command=self._pick_ssh, width=10).grid(row=0, column=1, padx=(8, 0))

        # --- Always visible: Network ---
        net = ttk.LabelFrame(self, text="Network")
        net.pack(fill="x", **PAD)
        net.columnconfigure(1, weight=1)
        net.columnconfigure(3, weight=1)

        ttk.Label(net, text="VLAN tag").grid(row=0, column=0, sticky="w", padx=8, pady=4)
        self.vlan_var = tk.IntVar(value=state.vlan)
        ttk.Spinbox(net, from_=0, to=4094, textvariable=self.vlan_var, width=6).grid(row=0, column=1, sticky="w")
        ttk.Label(net, text="(0 = untagged)", foreground="#555").grid(row=0, column=2, columnspan=2, sticky="w")

        ttk.Label(net, text="IP addressing").grid(row=1, column=0, sticky="w", padx=8, pady=(8, 4))
        self.netmode_var = tk.StringVar(value=state.net_mode)
        ttk.Radiobutton(net, text="DHCP", variable=self.netmode_var, value="dhcp",
                        command=self._sync_netmode).grid(row=1, column=1, sticky="w")
        ttk.Radiobutton(net, text="Static IP", variable=self.netmode_var, value="static",
                        command=self._sync_netmode).grid(row=1, column=2, columnspan=2, sticky="w")

        ttk.Label(net, text="Address (CIDR)").grid(row=2, column=0, sticky="w", padx=8, pady=4)
        self.addr_var = tk.StringVar(value=state.net_address_cidr)
        self.addr_entry = ttk.Entry(net, textvariable=self.addr_var)
        self.addr_entry.grid(row=2, column=1, columnspan=3, sticky="ew", padx=(0, 8))

        ttk.Label(net, text="Gateway").grid(row=3, column=0, sticky="w", padx=8, pady=4)
        self.gw_var = tk.StringVar(value=state.net_gateway)
        self.gw_entry = ttk.Entry(net, textvariable=self.gw_var)
        self.gw_entry.grid(row=3, column=1, columnspan=3, sticky="ew", padx=(0, 8))

        ttk.Label(net, text="DNS servers").grid(row=4, column=0, sticky="w", padx=8, pady=4)
        self.dns_var = tk.StringVar(value=state.net_dns)
        self.dns_entry = ttk.Entry(net, textvariable=self.dns_var)
        self.dns_entry.grid(row=4, column=1, columnspan=3, sticky="ew", padx=(0, 8))

        ttk.Label(net, foreground="#555",
                  text="    Examples: 192.168.1.50/24 ,  192.168.1.1 ,  8.8.8.8, 1.1.1.1").grid(
                      row=5, column=0, columnspan=4, sticky="w", padx=8, pady=(0, 6))

        # --- Advanced-only: VM resources ---
        self.adv_frame = ttk.LabelFrame(self, text="VM resources (advanced)")
        self.adv_frame.pack(fill="x", **PAD)
        adv = self.adv_frame
        adv.columnconfigure(1, weight=1)
        adv.columnconfigure(3, weight=1)

        ttk.Label(adv, text="vCPUs").grid(row=0, column=0, sticky="w", padx=8, pady=4)
        self.vcpu_var = tk.IntVar(value=state.vcpus)
        ttk.Spinbox(adv, from_=1, to=64, textvariable=self.vcpu_var, width=6).grid(row=0, column=1, sticky="w")

        ttk.Label(adv, text="RAM (GB)").grid(row=0, column=2, sticky="w", padx=(20, 8))
        self.mem_var = tk.IntVar(value=state.mem_gib)
        ttk.Spinbox(adv, from_=2, to=512, textvariable=self.mem_var, width=6).grid(row=0, column=3, sticky="w")

        ttk.Label(adv, text="Disk (GB)").grid(row=1, column=0, sticky="w", padx=8, pady=4)
        self.disk_var = tk.IntVar(value=state.disk_gib)
        ttk.Spinbox(adv, from_=20, to=2048, textvariable=self.disk_var, width=6).grid(row=1, column=1, sticky="w")

        ttk.Label(self, foreground="#555", justify="left",
                  text="The Linux user is created with passwordless sudo and is added to the docker group.\n"
                       "Static IP applies a netplan override before apt runs, so it works on networks\n"
                       "without DHCP. The wizard host still needs DNS to fetch the Ubuntu cloud image.").pack(
            anchor="w", padx=12, pady=(4, 12))

        self._sync_netmode()

    def on_show(self) -> None:
        # VM resources are advanced-only; network frame is always shown.
        if self.state.mode == "easy":
            self.adv_frame.pack_forget()
        else:
            self.adv_frame.pack(fill="x", **PAD)

    def _sync_netmode(self) -> None:
        static = self.netmode_var.get() == "static"
        state = ["!disabled"] if static else ["disabled"]
        for w in (self.addr_entry, self.gw_entry, self.dns_entry):
            w.state(state)

    def _gen_pw(self) -> None:
        self.pw_var.set(gen_password(20))
        if not self.show_pw.get():
            self.show_pw.set(1)
            self._toggle_pw()

    def _toggle_pw(self) -> None:
        self.pw_entry.configure(show="" if self.show_pw.get() else "*")

    def _pick_ssh(self) -> None:
        path = filedialog.askopenfilename(
            title="Select SSH public key",
            filetypes=[("Public keys", "*.pub"), ("All files", "*.*")],
            initialdir=str(Path.home() / ".ssh") if (Path.home() / ".ssh").exists() else None,
        )
        if path:
            try:
                self.ssh_var.set(Path(path).read_text(encoding="utf-8").strip())
            except OSError as e:
                messagebox.showerror("Read failed", str(e))

    def on_next(self) -> bool:
        name = self.name_var.get().strip()
        if not name:
            messagebox.showwarning("VM name", "Please enter a name for the VM.")
            return False
        user = self.user_var.get().strip()
        if not user or not user.replace("_", "").replace("-", "").isalnum() or not user[0].isalpha():
            messagebox.showwarning("Linux user", "Linux user must start with a letter and contain only letters, digits, hyphens, or underscores.")
            return False
        if not self.pw_var.get():
            messagebox.showwarning("Linux password", "Please enter or generate a Linux password.")
            return False

        # Network
        try:
            vlan = int(self.vlan_var.get())
        except (ValueError, tk.TclError):
            messagebox.showwarning("VLAN", "VLAN tag must be an integer between 0 and 4094.")
            return False
        if not (0 <= vlan <= 4094):
            messagebox.showwarning("VLAN", "VLAN tag must be between 0 and 4094.")
            return False

        net_mode = self.netmode_var.get()
        if net_mode == "static":
            addr = self.addr_var.get().strip()
            gw = self.gw_var.get().strip()
            dns_raw = self.dns_var.get().strip()
            if not _is_ipv4_cidr(addr):
                messagebox.showwarning("IP address", "Address must be in CIDR form, e.g. 192.168.1.50/24.")
                return False
            if not _is_ipv4(gw):
                messagebox.showwarning("Gateway", "Gateway must be an IPv4 address, e.g. 192.168.1.1.")
                return False
            dns = _parse_dns(dns_raw)
            for d in dns:
                if not _is_ipv4(d):
                    messagebox.showwarning("DNS", f"'{d}' is not a valid IPv4 address.")
                    return False
            self.state.net_address_cidr = addr
            self.state.net_gateway = gw
            self.state.net_dns = dns_raw
        self.state.net_mode = net_mode

        if self.state.mode == "advanced":
            try:
                self.state.vcpus = int(self.vcpu_var.get())
                self.state.mem_gib = int(self.mem_var.get())
                self.state.disk_gib = int(self.disk_var.get())
            except (ValueError, tk.TclError):
                messagebox.showwarning("Invalid resources", "vCPU, RAM, and disk must be integers.")
                return False

        self.state.vm_name = name
        self.state.linux_user = user
        self.state.linux_pass = self.pw_var.get()
        self.state.ssh_pubkey = self.ssh_var.get().strip()
        self.state.vlan = vlan
        return True


class AdvancedSettingsPage(WizardPage):
    """Master-detail editor for the full .env schema. Mirrors the curses TUI's
    main menu + section form: section list on the left, focused section's
    fields top-right, About panel for the focused field bottom-right."""

    title = "Advanced settings"
    subtitle = "Browse every setting by category — same data the curses TUI shows"

    GLYPH_OK = " ●"      # ready (changed from default OR default is fine)
    GLYPH_WARN = " ▲"    # placeholder still present
    GLYPH_SKIP = " ─"    # section disabled by profile

    def __init__(self, master: Wizard, state: WizardState) -> None:
        super().__init__(master, state)

        # ---- top: profile toggles ----
        prof_frame = ttk.Frame(self)
        prof_frame.pack(fill="x", padx=12, pady=(8, 4))
        ttk.Label(prof_frame, text="Profiles:", font=("Segoe UI", 10, "bold")).pack(side="left")

        self._var_core = tk.IntVar(value=1)
        ttk.Checkbutton(prof_frame, text="Core (always on)",
                        variable=self._var_core, state="disabled").pack(side="left", padx=(8, 0))
        self._var_sentinel = tk.IntVar(value=int(PROFILE_SENTINEL in state.profiles))
        ttk.Checkbutton(prof_frame, text="Sentinel (recommended)",
                        variable=self._var_sentinel,
                        command=self._on_profile_toggle).pack(side="left", padx=(8, 0))
        self._var_tools = tk.IntVar(value=int(PROFILE_TOOLS in state.profiles))
        ttk.Checkbutton(prof_frame, text="Grafana (optional)",
                        variable=self._var_tools,
                        command=self._on_profile_toggle).pack(side="left", padx=(8, 0))

        # ---- main split ----
        split = ttk.Frame(self)
        split.pack(fill="both", expand=True, padx=12, pady=(4, 8))
        split.columnconfigure(0, weight=0, minsize=240)
        split.columnconfigure(1, weight=1)
        split.rowconfigure(0, weight=1)

        # ---- left: section list ----
        left = ttk.LabelFrame(split, text="Sections")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self._section_list = tk.Listbox(left, activestyle="dotbox", exportselection=False,
                                         font=("Segoe UI", 10), bd=0, highlightthickness=0)
        self._section_list.pack(side="left", fill="both", expand=True, padx=4, pady=4)
        sb = ttk.Scrollbar(left, orient="vertical", command=self._section_list.yview)
        sb.pack(side="right", fill="y")
        self._section_list.configure(yscrollcommand=sb.set)
        self._section_list.bind("<<ListboxSelect>>", self._on_section_select)

        # ---- right: split into fields (top) + about (bottom) ----
        right = ttk.Frame(split)
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(0, weight=3)
        right.rowconfigure(1, weight=2)
        right.columnconfigure(0, weight=1)

        fields_outer = ttk.LabelFrame(right, text="Fields")
        fields_outer.grid(row=0, column=0, sticky="nsew", pady=(0, 4))
        self._fields_canvas = tk.Canvas(fields_outer, highlightthickness=0, bd=0)
        self._fields_canvas.pack(side="left", fill="both", expand=True)
        sb2 = ttk.Scrollbar(fields_outer, orient="vertical",
                            command=self._fields_canvas.yview)
        sb2.pack(side="right", fill="y")
        self._fields_canvas.configure(yscrollcommand=sb2.set)
        self._fields_inner = ttk.Frame(self._fields_canvas)
        self._fields_canvas_window = self._fields_canvas.create_window(
            (0, 0), window=self._fields_inner, anchor="nw"
        )
        self._fields_inner.bind(
            "<Configure>",
            lambda e: self._fields_canvas.configure(scrollregion=self._fields_canvas.bbox("all")),
        )
        self._fields_canvas.bind(
            "<Configure>",
            lambda e: self._fields_canvas.itemconfigure(self._fields_canvas_window, width=e.width),
        )
        # Mouse-wheel scrolling on the canvas
        self._fields_canvas.bind("<Enter>", lambda _e: self._fields_canvas.bind_all(
            "<MouseWheel>", self._on_mousewheel))
        self._fields_canvas.bind("<Leave>", lambda _e: self._fields_canvas.unbind_all("<MouseWheel>"))

        about_frame = ttk.LabelFrame(right, text="About this field")
        about_frame.grid(row=1, column=0, sticky="nsew")
        self._about_text = tk.Text(about_frame, wrap="word", font=("Segoe UI", 9),
                                   bd=0, padx=8, pady=6, height=8,
                                   state="disabled", background="#fafafa")
        self._about_text.pack(side="left", fill="both", expand=True)
        sb3 = ttk.Scrollbar(about_frame, orient="vertical",
                            command=self._about_text.yview)
        sb3.pack(side="right", fill="y")
        self._about_text.configure(yscrollcommand=sb3.set)

        # Working state
        self._visible_sections: list[SchemaSection] = []
        self._field_rows: list["_FieldRow"] = []
        self._focused_field: Optional[SchemaField] = None

    # --------------------------------------------------------------- on_show

    def on_show(self) -> None:
        self._ensure_state_initialized()
        self._sync_profiles_from_vars()
        self._rebuild_section_list(select_first=True)

    def _ensure_state_initialized(self) -> None:
        """First time we open this page, seed values from the schema + the
        operator-supplied connection/dashboard inputs. Subsequent visits keep
        whatever the operator edited."""
        st = self.state
        if st.advanced_initialized:
            return
        st.schema = build_schema()
        st.values = seed_default_values(st.schema)
        # Inputs already collected on earlier pages — pre-fill them so the
        # operator doesn't have to retype.
        if st.cluster_ip:
            st.values["SC_HOST"] = normalize_sc_host(st.cluster_ip)
        if st.cluster_user:
            st.values["SC_USERNAME"] = st.cluster_user
        if st.cluster_pass:
            st.values["SC_PASSWORD"] = st.cluster_pass
        # Linux paths — defaults from schema are already correct (/opt/...,
        # /root/.ssh) since seed_default_values copies them. We deliberately
        # do NOT call autofill_paths() here (would substitute Windows paths).
        st.values["SC_REPO_PATH"] = "/opt/hypercore-manager"
        st.values["SC_SSH_KEY_PATH"] = "/root/.ssh"
        # Autogen secrets up-front so the operator sees them in the editor.
        if not st.dashboard_pass:
            st.dashboard_pass = gen_password(20)
        st.values["SC_DASHBOARD_PASSWORD"] = st.dashboard_pass
        apply_easy_autogen(st.schema, st.values, st.profiles)
        st.advanced_initialized = True

    # ---------------------------------------------------------- profiles

    def _sync_profiles_from_vars(self) -> None:
        st = self.state
        st.profiles = {PROFILE_CORE}
        if self._var_sentinel.get():
            st.profiles.add(PROFILE_SENTINEL)
        if self._var_tools.get():
            st.profiles.add(PROFILE_TOOLS)

    def _on_profile_toggle(self) -> None:
        self._sync_profiles_from_vars()
        # Re-run autogen: enabling Tools might have introduced a new must_change
        # field (Grafana password) that needs a generated value.
        apply_easy_autogen(self.state.schema, self.state.values, self.state.profiles)
        self._rebuild_section_list(keep_selection=True)

    # ---------------------------------------------------- section list

    def _profile_visible_sections(self) -> list[SchemaSection]:
        return [s for s in self.state.schema if s.profiles & self.state.profiles]

    def _rebuild_section_list(self, *, select_first: bool = False, keep_selection: bool = False) -> None:
        prev_idx = self._section_list.curselection()
        prev_section_key: Optional[str] = None
        if keep_selection and prev_idx and self._visible_sections:
            try:
                prev_section_key = self._visible_sections[prev_idx[0]].key
            except IndexError:
                prev_section_key = None

        self._visible_sections = self._profile_visible_sections()
        self._section_list.delete(0, "end")
        for s in self._visible_sections:
            glyph = self._glyph_for(s)
            self._section_list.insert("end", f"{glyph}  {s.title}")

        # Restore selection
        target = 0
        if keep_selection and prev_section_key:
            for i, s in enumerate(self._visible_sections):
                if s.key == prev_section_key:
                    target = i
                    break
        if select_first or keep_selection:
            if self._visible_sections:
                self._section_list.selection_clear(0, "end")
                self._section_list.selection_set(target)
                self._section_list.see(target)
                self._show_section(self._visible_sections[target])

    def _glyph_for(self, section: SchemaSection) -> str:
        st = section_status(section, self.state.values, self.state.profiles)
        if st == "warn":
            return self.GLYPH_WARN
        if st == "skip":
            return self.GLYPH_SKIP
        return self.GLYPH_OK

    def _on_section_select(self, _event=None) -> None:
        sel = self._section_list.curselection()
        if not sel:
            return
        i = sel[0]
        if 0 <= i < len(self._visible_sections):
            self._show_section(self._visible_sections[i])

    # ---------------------------------------------------- section editor

    def _show_section(self, section: SchemaSection) -> None:
        # Clear existing rows
        for row in self._field_rows:
            row.frame.destroy()
        self._field_rows = []

        gated_off = is_section_gated_off(section, self.state.values)

        # Section blurb
        blurb = ttk.Label(self._fields_inner, text=section.blurb, foreground="#555",
                          wraplength=520, justify="left")
        blurb.grid(row=0, column=0, columnspan=3, sticky="w", padx=8, pady=(6, 8))

        if gated_off:
            note = ttk.Label(self._fields_inner,
                             text=f"NOTE: section gated off by '{section.enable_field}'. "
                                  f"Other fields are stored but unused.",
                             foreground="#a86", wraplength=520, justify="left")
            note.grid(row=1, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 6))

        row_idx = 2
        for f in section.fields:
            if not (f.profiles & self.state.profiles):
                continue
            row = _FieldRow(self._fields_inner, f, self.state, on_change=self._on_field_change,
                            on_focus=self._on_field_focus)
            row.frame.grid(row=row_idx, column=0, columnspan=3, sticky="ew", padx=8, pady=2)
            self._fields_inner.columnconfigure(0, weight=1)
            self._field_rows.append(row)
            row_idx += 1

        # Default focus + About panel content
        if self._field_rows:
            self._on_field_focus(self._field_rows[0].field_def)

    def _on_field_change(self, _field: SchemaField, _new_value: str) -> None:
        # Repaint every row's status (a field can affect section gating, etc.)
        # then refresh the section glyphs.
        for row in self._field_rows:
            row.repaint_status()
        # Update glyphs in the section list without losing selection.
        sel = self._section_list.curselection()
        for i, s in enumerate(self._visible_sections):
            new_label = f"{self._glyph_for(s)}  {s.title}"
            old_label = self._section_list.get(i)
            if new_label != old_label:
                self._section_list.delete(i)
                self._section_list.insert(i, new_label)
        if sel:
            self._section_list.selection_clear(0, "end")
            self._section_list.selection_set(sel[0])

    def _on_field_focus(self, field_def: SchemaField) -> None:
        self._focused_field = field_def
        self._render_about(field_def)

    def _render_about(self, f: SchemaField) -> None:
        body_lines: list[tuple[str, str]] = []
        body_lines.append(("h", f.label + f"  ({f.key}, type={f.type})"))
        if f.description:
            body_lines.append(("p", f.description))
        body_lines.append(("k", "Default:"))
        body_lines.append(("p", f.default if f.default else "(empty)"))
        if f.why_default:
            body_lines.append(("k", "Why this default:"))
            body_lines.append(("p", f.why_default))
        if f.when_change:
            body_lines.append(("k", "When you'd change it:"))
            body_lines.append(("p", f.when_change))

        self._about_text.configure(state="normal")
        self._about_text.delete("1.0", "end")
        for kind, txt in body_lines:
            if kind == "h":
                self._about_text.insert("end", txt + "\n", "h")
            elif kind == "k":
                self._about_text.insert("end", txt + "\n", "k")
            else:
                self._about_text.insert("end", txt + "\n\n")
        self._about_text.tag_configure("h", font=("Segoe UI", 10, "bold"), foreground="#234")
        self._about_text.tag_configure("k", font=("Segoe UI", 9, "bold"), foreground="#456",
                                       spacing1=4)
        self._about_text.configure(state="disabled")
        self._about_text.yview_moveto(0)

    # ---------------------------------------------------- mouse-wheel

    def _on_mousewheel(self, event) -> None:
        # Cross-platform-ish: Windows uses event.delta in multiples of 120
        delta = -1 if event.delta > 0 else 1
        self._fields_canvas.yview_scroll(delta, "units")

    # --------------------------------------------------- next/back hooks

    def on_next(self) -> bool:
        st = self.state
        # Bring back any in-flight value from the row widgets (StringVar sync
        # already keeps state.values current, but cover the spinbox-without-tab
        # case by forcing a final sync).
        for row in self._field_rows:
            row.commit_to_state()
        # Validate every active field; bail if any error or unresolved
        # must_change placeholder.
        from installer.core import collect_validation_errors  # local import OK
        errors = collect_validation_errors(st.schema, st.values, st.profiles)
        if errors:
            messagebox.showwarning(
                "Settings need attention",
                "The following fields still need to be resolved:\n\n" +
                "\n".join(errors[:12]) +
                ("\n…" if len(errors) > 12 else "") +
                "\n\nLook for sections marked ▲ in the list.",
            )
            return False
        # Keep dashboard_pass in sync with whatever's in values now.
        if st.values.get("SC_DASHBOARD_PASSWORD"):
            st.dashboard_pass = st.values["SC_DASHBOARD_PASSWORD"]
        return True


# ----------------------------------------------------------------------------
# Field row — one widget per field type
# ----------------------------------------------------------------------------

class _FieldRow:
    STATUS_OK = "●"
    STATUS_WARN = "▲"
    STATUS_ERR = "✕"

    def __init__(self, parent: ttk.Frame, field_def: SchemaField, state: WizardState,
                 on_change: Callable[[SchemaField, str], None],
                 on_focus: Callable[[SchemaField], None]) -> None:
        self.field_def = field_def
        self.state = state
        self._on_change = on_change
        self._on_focus = on_focus
        self.frame = ttk.Frame(parent)
        self.frame.columnconfigure(2, weight=1)

        # Status indicator (live)
        self._status_lbl = ttk.Label(self.frame, text="•", width=2)
        self._status_lbl.grid(row=0, column=0, sticky="w")

        # Label
        self._label = ttk.Label(self.frame, text=field_def.label, width=24, anchor="w")
        self._label.grid(row=0, column=1, sticky="w", padx=(0, 6))
        self._label.bind("<Button-1>", lambda _e: self._on_focus(field_def))

        # Build the widget appropriate for this field type
        current = state.values.get(field_def.key, field_def.default)
        self._var = tk.StringVar(value=current)
        self._var.trace_add("write", self._on_var_changed)
        self._build_value_widget(field_def, current)

        self.repaint_status()

    # ------------------------------------------------------------ widgets

    def _build_value_widget(self, f: SchemaField, current: str) -> None:
        ftype = f.type
        if ftype == "bool":
            # Checkbox aligned with column 2
            self._bool_var = tk.IntVar(value=1 if str(current).lower() == "true" else 0)
            self._bool_var.trace_add("write", self._on_bool_changed)
            cb = ttk.Checkbutton(self.frame, variable=self._bool_var,
                                 text="enabled" if self._bool_var.get() else "disabled",
                                 command=self._on_bool_check)
            cb.grid(row=0, column=2, sticky="w")
            cb.bind("<FocusIn>", lambda _e: self._on_focus(f))
            self._cb = cb
        elif ftype == "choice" and f.choices:
            cmb = ttk.Combobox(self.frame, textvariable=self._var, values=list(f.choices),
                               state="readonly", width=28)
            cmb.grid(row=0, column=2, sticky="ew")
            cmb.bind("<FocusIn>", lambda _e: self._on_focus(f))
        elif ftype == "int":
            sp = ttk.Spinbox(self.frame, textvariable=self._var, from_=-2147483648, to=2147483647,
                              width=12)
            sp.grid(row=0, column=2, sticky="w")
            sp.bind("<FocusIn>", lambda _e: self._on_focus(f))
        elif ftype in ("password", "token"):
            entry = ttk.Entry(self.frame, textvariable=self._var, show="•", width=36)
            entry.grid(row=0, column=2, sticky="ew")
            entry.bind("<FocusIn>", lambda _e: self._on_focus(f))
            self._entry = entry
            ttk.Button(self.frame, text="Generate", width=10,
                       command=self._generate).grid(row=0, column=3, padx=(6, 0))
            ttk.Button(self.frame, text="Show", width=6,
                       command=self._toggle_reveal).grid(row=0, column=4, padx=(4, 0))
        elif ftype == "path":
            entry = ttk.Entry(self.frame, textvariable=self._var, width=36)
            entry.grid(row=0, column=2, sticky="ew")
            entry.bind("<FocusIn>", lambda _e: self._on_focus(f))
            ttk.Button(self.frame, text="Reset", width=8,
                       command=self._reset_to_default).grid(row=0, column=3, padx=(6, 0))
        else:  # text / float / fallback
            entry = ttk.Entry(self.frame, textvariable=self._var, width=36)
            entry.grid(row=0, column=2, sticky="ew")
            entry.bind("<FocusIn>", lambda _e: self._on_focus(f))

    # ----------------------------------------------------- variable hooks

    def _on_var_changed(self, *_a) -> None:
        new = self._var.get()
        self.state.values[self.field_def.key] = new
        self._on_change(self.field_def, new)
        self.repaint_status()

    def _on_bool_changed(self, *_a) -> None:
        # Mirror the bool var into the underlying string var so write_env emits
        # 'true'/'false' consistently.
        self._var.set("true" if self._bool_var.get() else "false")

    def _on_bool_check(self) -> None:
        self._cb.configure(text="enabled" if self._bool_var.get() else "disabled")

    # ---------------------------------------------------------- buttons

    def _generate(self) -> None:
        f = self.field_def
        new = gen_token(32) if f.type == "token" else gen_password(20)
        self._var.set(new)

    def _toggle_reveal(self) -> None:
        if hasattr(self, "_entry"):
            current = str(self._entry.cget("show"))
            self._entry.configure(show="" if current else "•")

    def _reset_to_default(self) -> None:
        self._var.set(self.field_def.default)

    # ---------------------------------------------------------- status

    def repaint_status(self) -> None:
        f = self.field_def
        v = self.state.values.get(f.key, f.default)
        if f.must_change and v in PLACEHOLDERS:
            text, fg = self.STATUS_WARN, "#c80"
        else:
            err = validate_value(f, v)
            if err:
                text, fg = self.STATUS_ERR, "#a22"
            elif v != f.default and v != "":
                text, fg = self.STATUS_OK, "#080"
            else:
                text, fg = "·", "#888"
        self._status_lbl.configure(text=text, foreground=fg)

    def commit_to_state(self) -> None:
        # Force-sync (covers Spinbox values that weren't committed via Enter/Tab)
        self.state.values[self.field_def.key] = self._var.get()


class ReviewPage(WizardPage):
    title = "Review and deploy"
    subtitle = "Verify the plan — nothing has been sent to the cluster yet"

    def __init__(self, master: Wizard, state: WizardState) -> None:
        super().__init__(master, state)
        self.summary = scrolledtext.ScrolledText(self, font=("Segoe UI", 10), wrap="word", height=18)
        self.summary.pack(fill="both", expand=True, padx=12, pady=8)
        self.summary.configure(state="disabled")

    def on_show(self) -> None:
        s = self.state
        # Auto-generate dashboard password now if still blank.
        if not s.dashboard_pass:
            s.dashboard_pass = gen_password(20)

        if s.net_mode == "static":
            net_summary = [
                f"  IP                 static  {s.net_address_cidr}",
                f"  Gateway            {s.net_gateway}",
                f"  DNS                {s.net_dns}",
            ]
        else:
            net_summary = ["  IP                 DHCP"]

        lines = [
            f"Cluster              {normalize_sc_host(s.cluster_ip)}",
            f"  Username           {s.cluster_user}",
            f"  Verify TLS         {'yes' if s.verify_tls else 'no (self-signed OK)'}",
            "",
            f"VM name              {s.vm_name}",
            f"  Hostname           {_sanitize_hostname(s.vm_name)}",
            f"  vCPU / RAM / Disk  {s.vcpus} vCPU  /  {s.mem_gib} GB  /  {s.disk_gib} GB",
            f"  VLAN               {s.vlan if s.vlan else '0 (untagged)'}",
            *net_summary,
            "",
            f"Linux user           {s.linux_user}",
            f"  Password           {'*' * 8}  (collected on previous page)",
            f"  SSH key            {'(supplied)' if s.ssh_pubkey else '(none)'}",
            "",
            f"Dashboard password   {s.dashboard_pass}",
            "  ↑ this was auto-generated. Copy it now — it'll also land in .env on the VM.",
            "",
            f"Mode                 {s.mode.upper()}",
            f"Profiles             {', '.join(sorted(s.profiles))}",
        ]
        if s.mode == "advanced" and s.advanced_initialized:
            lines.append("")
            lines.append("Advanced settings are in effect — values come from your AdvancedSettingsPage edits.")

        self.summary.configure(state="normal")
        self.summary.delete("1.0", "end")
        self.summary.insert("1.0", "\n".join(lines))
        self.summary.configure(state="disabled")


class DeployPage(WizardPage):
    title = "Deploying"
    subtitle = "Talking to the cluster — keep this window open"

    def __init__(self, master: Wizard, state: WizardState) -> None:
        super().__init__(master, state)
        self.progress = ttk.Progressbar(self, mode="determinate", maximum=100)
        self.progress.pack(fill="x", padx=12, pady=(12, 4))

        self.status = ttk.Label(self, text="Ready.", foreground="#555")
        self.status.pack(anchor="w", padx=12, pady=4)

        self.log = scrolledtext.ScrolledText(self, font=("Consolas", 9), wrap="word", height=18)
        self.log.pack(fill="both", expand=True, padx=12, pady=8)
        self.log.configure(state="disabled")

        self._q: queue.Queue = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._done = False
        self._success = False

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def on_show(self) -> None:
        if self._thread is not None:
            return  # already started; user navigated back+forward (shouldn't happen with disabled back)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self.after(100, self._poll_queue)

    def _emit(self, kind: str, payload) -> None:
        self._q.put((kind, payload))

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self._q.get_nowait()
                if kind == "log":
                    self._append(payload)
                elif kind == "status":
                    self.status.configure(text=payload, foreground="#333")
                elif kind == "progress":
                    self.progress.configure(value=payload)
                elif kind == "done":
                    self._done = True
                    self._success = bool(payload)
                    self.wizard.btn_next.state(["!disabled"])
                    self.status.configure(
                        text="Done — VM is booting." if self._success else "Failed — see log above.",
                        foreground="#080" if self._success else "#a00",
                    )
        except queue.Empty:
            pass
        if not self._done:
            self.after(150, self._poll_queue)

    def _append(self, line: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", line.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def on_finish_click(self) -> None:
        self.wizard.destroy()

    # ----- helpers -----

    def _wait_for_guest_ip(
        self,
        client: HyperCoreClient,
        vm_uuid: str,
        *,
        timeout_s: float,
        log: Callable[[str], None],
        pct: Callable[[int], None],
    ) -> Optional[str]:
        """Poll the cluster for the agent-reported IPv4 of `vm_uuid`. Returns
        the first usable IPv4 (skips loopback / link-local), or None on timeout.
        Creeps the progress bar between 85 and 99 % over the whole wait window.

        The agent only starts at the very END of the bootstrap (after
        `docker compose up -d`), so seeing an IP here means the stack is
        actually running — not just that the VM has booted."""
        # Helpful narration so the operator knows what's happening behind the
        # silence. Print at known phase boundaries (in seconds) — these are
        # estimates, the real timing depends on bandwidth + cluster speed.
        narration = [
            (0,    "  Phase: cloud-init starting on the VM…"),
            (60,   "  Phase: apt-get update + installing Docker, qemu-guest-agent, etc."),
            (180,  "  Phase: extracting repo, docker compose pull/build (the slow bit)…"),
            (480,  "  Phase: still building images — patience pays off…"),
            (720,  "  Phase: build is taking a while. SSH in and tail the bootstrap log if you'd like to see what's happening:"),
            (720,  "         ssh <user>@<vm-ip>  'sudo tail -F /var/log/hypercore-bootstrap.log'"),
        ]
        narrated_at: set[int] = set()

        deadline = time.monotonic() + timeout_s
        start = time.monotonic()
        attempt = 0
        last_err: Optional[str] = None
        while time.monotonic() < deadline:
            attempt += 1
            elapsed = int(time.monotonic() - start)
            for at, msg in narration:
                if elapsed >= at and at not in narrated_at:
                    log(msg)
                    narrated_at.add(at)
            try:
                ips = client.vm_ipv4(vm_uuid)
                usable = [
                    ip for ip in ips
                    if ip and not ip.startswith("127.") and not ip.startswith("169.254.")
                ]
                if usable:
                    log(f"  poll {attempt:>3} ({elapsed}s): agent reports {', '.join(usable)}")
                    return usable[0]
                if attempt % 6 == 1:  # ~once a minute
                    log(f"  poll {attempt:>3} ({elapsed}s): no IP yet — agent only starts after the stack is up")
            except HyperCoreError as exc:
                last_err = str(exc)
                if attempt % 6 == 1:
                    log(f"  poll {attempt:>3} ({elapsed}s): API error: {exc}")
            # 85 → 99 over the whole wait window (1 % per ~85 s on default timeout)
            pct(min(99, 85 + (attempt // 9)))
            time.sleep(10)
        if last_err:
            log(f"  last API error before giving up: {last_err}")
        return None

    # ----- worker -----

    def _run(self) -> None:
        s = self.state

        def log(msg: str) -> None:
            self._emit("log", msg)

        def status(msg: str) -> None:
            self._emit("status", msg)

        def pct(p: int) -> None:
            self._emit("progress", min(100, max(0, p)))

        try:
            log(f"Cluster: {normalize_sc_host(s.cluster_ip)}")
            log(f"VM: {s.vm_name}  ({s.vcpus} vCPU / {s.mem_gib} GB RAM / {s.disk_gib} GB disk)")

            # Progress allocation (revised to leave the last ~15 % for the
            # post-power-on wait — when docker compose is building images on
            # the VM and we have nothing to do but poll for the agent's IP).
            #   0–10  prep (render env, build tarball, build cloud-init)
            #  10–23  download Ubuntu cloud image
            #  23–26  authenticate to cluster
            #  26–60  upload boot disk           ← biggest single step
            #  60–68  upload payload disk
            #  68–85  create VM, attach disks, NIC, power-on
            #  85–99  wait for guest agent to report IP

            # 1. Render env from the schema + wizard inputs.
            status("Rendering .env…")
            env_body = _build_env_for_state(s)
            log(f".env rendered: {len(env_body)} bytes")
            pct(4)

            # 2. Bundle the repo (or read the one PyInstaller baked into the .exe).
            if _BUNDLED_PAYLOAD is not None:
                status("Loading bundled repo tarball…")
                tar_bytes = _BUNDLED_PAYLOAD.read_bytes()
                log(f"Repo tarball (bundled): {_humanize_bytes(len(tar_bytes))}")
            else:
                status("Bundling repo into a tarball…")
                tar_bytes = build_repo_tarball(_REPO)
                log(f"Repo tarball (built from {_REPO}): {_humanize_bytes(len(tar_bytes))}")
            payload_bytes = pad_to_sector(tar_bytes, 1024 * 1024)
            log(f"Payload disk (padded to 1 MiB sector): {_humanize_bytes(len(payload_bytes))}")
            pct(7)

            # 3. Build cloud-init payload — small now, no inline tarball.
            status("Building cloud-init userData…")
            hostname = _sanitize_hostname(s.vm_name)
            net_config = NetworkConfig(
                mode=s.net_mode,
                address_cidr=s.net_address_cidr,
                gateway=s.net_gateway,
                dns=_parse_dns(s.net_dns) if s.net_mode == "static" else [],
            )
            log(f"Network: {s.net_mode}" + (f" {s.net_address_cidr} via {s.net_gateway}" if s.net_mode == "static" else ""))
            user_data = render_user_data(
                hostname=hostname,
                timezone=s.timezone,
                linux_user=s.linux_user,
                linux_password=s.linux_pass,
                ssh_authorized_keys=[s.ssh_pubkey] if s.ssh_pubkey else (),
                repo_dest=DEFAULT_REPO_DEST,
                env_contents=env_body,
                network_config=net_config,
                profiles=s.profiles,
            )
            meta_data = render_meta_data(instance_id=make_instance_id(hostname), hostname=hostname)
            log(f"userData: {_humanize_bytes(len(user_data))}")
            pct(10)

            # 4. Ensure cloud image is on disk.
            status("Fetching Ubuntu cloud image (cached after first run)…")

            def img_progress(stage: str, done: int, total: int) -> None:
                if total:
                    p = 10 + int(13 * done / total)
                    pct(p)
                    if done == total or done % (10 * 1024 * 1024) < 256 * 1024:
                        log(f"  {stage}: {_humanize_bytes(done)} / {_humanize_bytes(total)}")

            img_path = ensure_image(progress=img_progress)
            img_size = img_path.stat().st_size
            log(f"Image: {img_path}  ({_humanize_bytes(img_size)})")
            pct(23)

            # 5. Connect.
            status("Authenticating to HyperCore…")
            client = HyperCoreClient(normalize_sc_host(s.cluster_ip), verify_tls=s.verify_tls, timeout=60)
            client.login(s.cluster_user, s.cluster_pass)
            log("Login OK")
            pct(26)

            # 6. Upload Ubuntu cloud image as the boot disk.
            status("Uploading cloud image to cluster…")
            boot_upload_name = s.vm_name + "-" + DEFAULT_IMAGE_NAME
            boot_hb = _ConversionHeartbeat(log, "boot disk", interval=30)

            def boot_progress(done: int, total: int) -> None:
                if total:
                    p = 26 + int(34 * done / total)
                    pct(p)
                    if done == total or done % (20 * 1024 * 1024) < 1024 * 1024:
                        log(f"  boot disk: {_humanize_bytes(done)} / {_humanize_bytes(total)}")
                    if done == total:
                        log("  Converting to VSD…")
                        boot_hb.start()

            try:
                with open(img_path, "rb") as f:
                    boot_vdisk_uuid = client.upload_virtual_disk(boot_upload_name, f, img_size, progress=boot_progress)
            finally:
                boot_hb.stop()
            log(f"Boot VirtualDisk uuid={boot_vdisk_uuid}")
            pct(60)

            # 7. Upload the repo tarball as a second VirtualDisk (raw bytes).
            status("Uploading repo payload as a second virtual disk…")
            payload_upload_name = s.vm_name + "-payload.img"
            payload_hb = _ConversionHeartbeat(log, "payload disk", interval=30)

            def payload_progress(done: int, total: int) -> None:
                if total:
                    p = 60 + int(8 * done / total)
                    pct(p)
                    if done == total:
                        log(f"  payload: {_humanize_bytes(done)} / {_humanize_bytes(total)}")
                        log("  Converting to VSD…")
                        payload_hb.start()

            try:
                payload_stream = io.BytesIO(payload_bytes)
                payload_vdisk_uuid = client.upload_virtual_disk(
                    payload_upload_name, payload_stream, len(payload_bytes), progress=payload_progress,
                )
            finally:
                payload_hb.stop()
            log(f"Payload VirtualDisk uuid={payload_vdisk_uuid}")
            pct(68)

            # 8. Create VM with cloud-init data.
            status("Creating VM with cloud-init data…")
            vm_uuid = client.create_vm(
                name=s.vm_name,
                description="Provisioned by HyperCore Manager Deploy Wizard",
                vcpus=s.vcpus,
                mem_bytes=s.mem_gib * 1024 * 1024 * 1024,
                cloudinit_user_data=user_data,
                cloudinit_meta_data=meta_data,
                operating_system="os_other",
                machine_type_keyword="uefi",
                attach_guest_tools_iso=False,
                tags="hypercore-manager,wizard",
            )
            log(f"VirDomain uuid={vm_uuid}")
            pct(73)

            # 9. Attach the boot disk (grown to the requested capacity).
            status("Attaching boot disk to VM…")
            disk_capacity = s.disk_gib * 1024 * 1024 * 1024
            boot_block_uuid = client.attach_virtual_disk(
                boot_vdisk_uuid, vm_uuid, type_="VIRTIO_DISK", capacity_bytes=disk_capacity,
            )
            log(f"Boot BlockDevice uuid={boot_block_uuid}")
            pct(78)

            # 10. Attach the payload disk (exact size — don't grow).
            status("Attaching payload disk…")
            payload_block_uuid = client.attach_virtual_disk(
                payload_vdisk_uuid, vm_uuid, type_="VIRTIO_DISK",
                capacity_bytes=len(payload_bytes),
            )
            log(f"Payload BlockDevice uuid={payload_block_uuid}")
            pct(81)

            # 11. Set the boot disk as the (sole) boot device.
            client.update_vm(vm_uuid, {"bootDevices": [boot_block_uuid]})
            log("Boot device set")

            # 12. Create a NIC.
            status("Adding network interface…")
            nic_uuid = client.create_nic(vm_uuid, type_="VIRTIO", vlan=s.vlan)
            log(f"NetDevice uuid={nic_uuid}  vlan={s.vlan}")
            pct(84)

            # 13. Power on.
            status("Powering on…")
            client.power_on(vm_uuid)
            log("Power on issued")
            pct(85)

            # 14. Wait for the qemu-guest-agent to report an IPv4 — but the
            #     bootstrap deliberately holds off on starting the agent until
            #     AFTER `docker compose up -d` finishes. So the IP-report here
            #     is genuinely "the dashboard is reachable now", not just
            #     "the VM has booted".
            log("")
            log("Waiting for the VM's first-boot bootstrap to finish.")
            log("The cloud-init runcmd is, in order:")
            log("   1) apply network config         (~10 s)")
            log("   2) apt-get install Docker + tools  (~1-2 min)")
            log("   3) extract repo from payload disk")
            log("   4) docker compose pull / build / up   (~3-6 min — by far the longest)")
            log("   5) start qemu-guest-agent          ← we see the IP appear here")
            log("")
            log("Polling the cluster every 10 s for the agent's IP report.")
            status("Waiting for container builds to complete…")

            ip = self._wait_for_guest_ip(client, vm_uuid, timeout_s=1200, log=log, pct=pct)

            log("")
            log("=" * 64)
            if ip:
                log(f"  VM IP:           {ip}")
                log(f"  Dashboard URL:   http://{ip}:5000")
                log(f"  Login:           admin  /  {s.dashboard_pass}")
                log("=" * 64)
                log("")
                log("Stack is up. Open the dashboard URL above in a browser.")
            else:
                log("  Guest agent did not report an IP within 20 minutes.")
                log("  The VM was created and powered on, but either the bootstrap")
                log("  is still running (very slow link?) or it failed somewhere.")
                log("  Open the VM console in the cluster UI and check:")
                log("      sudo systemctl status qemu-guest-agent")
                log("      sudo tail -F /var/log/hypercore-bootstrap.log")
                log(f"  Dashboard login (once the stack is up):  admin  /  {s.dashboard_pass}")
                log("=" * 64)
            pct(100)
            self._emit("done", True)
        except (HyperCoreError, ImageCacheError) as e:
            log("")
            log(f"ERROR: {e}")
            self._emit("done", False)
        except Exception as e:  # noqa: BLE001
            import traceback
            log("")
            log("UNEXPECTED ERROR:")
            log(traceback.format_exc())
            self._emit("done", False)


# =============================================================================
# Glue: build a populated .env from the WizardState
# =============================================================================

def _build_env_for_state(state: WizardState) -> str:
    """Render .env using the schema. In advanced mode, the operator's edits in
    AdvancedSettingsPage are the source of truth (they live on state.values).
    In easy mode we seed defaults + autogen secrets in one shot."""
    if state.mode == "advanced" and state.advanced_initialized:
        # The advanced page has already populated state.schema, state.values,
        # and state.profiles. Just emit.
        return render_env(state.schema, state.values, state.profiles)

    # Easy mode (or advanced before the operator visited the settings page).
    sections = build_schema()
    values = seed_default_values(sections)

    # Operator-supplied keys
    values["SC_HOST"] = normalize_sc_host(state.cluster_ip)
    values["SC_USERNAME"] = state.cluster_user
    values["SC_PASSWORD"] = state.cluster_pass
    if not state.dashboard_pass:
        state.dashboard_pass = gen_password(20)
    values["SC_DASHBOARD_PASSWORD"] = state.dashboard_pass

    # Force Linux paths regardless of where the wizard is running. Schema
    # defaults are right for the target VM (/opt/hypercore-manager and
    # /root/.ssh). DO NOT call autofill_paths() — that would substitute
    # Path.cwd() from the Windows host, which Docker on the Linux VM then
    # rejects as "invalid characters for a local volume name".
    values["SC_REPO_PATH"] = DEFAULT_REPO_DEST
    values["SC_SSH_KEY_PATH"] = "/root/.ssh"

    state.profiles = set(EASY_PROFILES)
    apply_easy_autogen(sections, values, state.profiles)

    # Cache schema/values back onto state so a later AdvancedSettingsPage visit
    # picks them up (e.g. operator switched easy → advanced via Back).
    state.schema = sections
    state.values = values

    return render_env(sections, values, state.profiles)


# =============================================================================
# Entry point
# =============================================================================

def main() -> int:
    app = Wizard()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
