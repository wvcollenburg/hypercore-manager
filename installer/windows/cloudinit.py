"""Cloud-init userData / metaData renderer for HyperCore deployments.

Cloud-init's #cloud-config format is YAML, but JSON is a strict subset of
YAML 1.2, so we emit JSON and prepend the magic comment. That avoids pulling
in PyYAML for a single render.

The userData stays small (under HyperCore's request body limit) by NOT
inlining the repo. The repo is shipped as a separate VirtualDisk attached
to the VM; the bootstrap script reads it raw from /dev/vd? and extracts.
The userData itself only carries:
  * The fully-populated .env (written to {repo_dest}/.env, mode 0600)
  * Apt installs (docker.io, compose plugin), systemd enable, docker compose up
  * The Linux user and password (cloud-init's chpasswd handles the hashing)
"""

from __future__ import annotations

import io
import json
import os
import secrets
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional


DEFAULT_REPO_DEST = "/opt/hypercore-manager"


@dataclass
class NetworkConfig:
    """Guest network configuration. DHCP by default; static fills in IP details.

    These are applied via a netplan drop-in (`/etc/netplan/99-hcm-static.yaml`)
    that the bootstrap script `netplan apply`s before running apt — so static
    deployments work even on networks without a DHCP server.
    """
    mode: str = "dhcp"          # 'dhcp' | 'static'
    address_cidr: str = ""      # e.g. '192.168.1.50/24'
    gateway: str = ""           # e.g. '192.168.1.1'
    dns: list[str] = field(default_factory=list)


def render_netplan(net: NetworkConfig) -> str:
    """Build a netplan v2 YAML body for static IPv4. Empty for DHCP."""
    if net.mode != "static":
        return ""
    eth: dict = {
        "match": {"name": "en*"},
        "set-name": "primary",
        "dhcp4": False,
        "addresses": [net.address_cidr],
    }
    if net.gateway:
        eth["routes"] = [{"to": "default", "via": net.gateway}]
    if net.dns:
        eth["nameservers"] = {"addresses": list(net.dns)}
    body = {"network": {"version": 2, "ethernets": {"primary": eth}}}
    return json.dumps(body, indent=2)

# files/dirs that should never end up in the deploy tarball
TARBALL_EXCLUDE_DIRS = {
    ".git", "__pycache__", ".claude", ".venv", "venv", "env",
    "node_modules", "tests", "dist", "build", ".pytest_cache",
}
TARBALL_EXCLUDE_FILES = {".env", ".DS_Store"}
TARBALL_EXCLUDE_GLOBS = ("*.pyc", "*.pyo", ".env.bak*", "*.qcow2", "*.img", "*.iso")


def build_repo_tarball(source_dir: Path, dest: Optional[Path] = None) -> bytes:
    """Bundle the repo into a tar.gz, returning the bytes (and writing to dest if given).
    Excludes git history, Python caches, secrets, and large image artefacts."""
    import fnmatch

    buf = io.BytesIO()
    src = source_dir.resolve()
    with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=9) as tar:
        for root, dirs, files in os.walk(src):
            dirs[:] = [d for d in dirs if d not in TARBALL_EXCLUDE_DIRS]
            for fname in files:
                if fname in TARBALL_EXCLUDE_FILES:
                    continue
                if any(fnmatch.fnmatch(fname, g) for g in TARBALL_EXCLUDE_GLOBS):
                    continue
                full = Path(root) / fname
                arcname = "hypercore-manager/" + str(full.relative_to(src)).replace(os.sep, "/")
                tar.add(full, arcname=arcname)
    data = buf.getvalue()
    if dest:
        dest.write_bytes(data)
    return data


def pad_to_sector(data: bytes, sector: int = 1024 * 1024) -> bytes:
    """Pad to a multiple of `sector` bytes (default 1 MiB) so the cluster
    accepts it as a virtual disk. Trailing zeros are harmless: tar -xz reads
    until the gzip end-of-stream marker and stops."""
    rem = len(data) % sector
    if rem == 0:
        return data
    return data + b"\x00" * (sector - rem)


def render_meta_data(*, instance_id: str, hostname: str) -> str:
    """NoCloud meta-data: instance-id + local-hostname."""
    return json.dumps({
        "instance-id": instance_id,
        "local-hostname": hostname,
    }, indent=2)


def render_user_data(
    *,
    hostname: str,
    timezone: str = "UTC",
    linux_user: str,
    linux_password: str,
    ssh_authorized_keys: Iterable[str] = (),
    repo_dest: str = DEFAULT_REPO_DEST,
    env_contents: str,
    profiles: Iterable[str],
    network_config: Optional[NetworkConfig] = None,
) -> str:
    """Build the #cloud-config body that drives first-boot provisioning.

    Note: the repo is NOT in this payload. It rides on a separate VirtualDisk
    that the bootstrap script extracts from at first boot. Keeping userData
    small (a few KB instead of MBs) keeps us under HyperCore's API body limit.

    Apt installs and systemd setup all happen in the bootstrap script (not via
    cloud-init's `packages:` directive) so a static-IP deployment can reconfigure
    the network BEFORE apt runs — this works on networks without a DHCP server.
    """
    profile_args: list[str] = []
    profiles_set = set(profiles)
    if "sentinel" in profiles_set:
        profile_args += ["--profile", "sentinel"]
    if "tools" in profiles_set:
        profile_args += ["--profile", "tools"]
    profile_str = " ".join(profile_args)

    static_net = network_config and network_config.mode == "static"

    write_files = [
        {
            "path": f"{repo_dest}/.env",
            "permissions": "0600",
            "owner": "root:root",
            "content": env_contents,
        },
        {
            "path": "/usr/local/sbin/hypercore-bootstrap.sh",
            "permissions": "0755",
            "owner": "root:root",
            "content": _bootstrap_script(repo_dest, linux_user, profile_str),
        },
    ]
    if static_net:
        write_files.append({
            "path": "/etc/netplan/99-hcm-static.yaml",
            "permissions": "0600",
            "owner": "root:root",
            "content": render_netplan(network_config),
        })

    config = {
        "hostname": hostname,
        "manage_etc_hosts": True,
        "timezone": timezone,
        "users": [
            {
                # don't add the user to the 'docker' group here — it doesn't
                # exist until apt installs docker.io, and on some cloud-init
                # versions referencing a missing group aborts the user-create
                # module (which then blocks runcmd). Bootstrap runs
                # `usermod -aG docker` after the package install
                "name": linux_user,
                "groups": ["sudo"],
                "shell": "/bin/bash",
                "sudo": "ALL=(ALL) NOPASSWD:ALL",
                "lock_passwd": False,
                "ssh_authorized_keys": list(ssh_authorized_keys),
            },
        ],
        "ssh_pwauth": True,
        "chpasswd": {
            "expire": False,
            "users": [
                {"name": linux_user, "password": linux_password, "type": "text"},
            ],
        },
        "write_files": write_files,
        "runcmd": [
            ["bash", "/usr/local/sbin/hypercore-bootstrap.sh"],
        ],
        "final_message": (
            "HyperCore Manager bootstrap finished after $UPTIME seconds. "
            "The web dashboard should be reachable on port 5000."
        ),
    }
    return "#cloud-config\n" + json.dumps(config, indent=2)


def _bootstrap_script(repo_dest: str, user: str, profile_str: str) -> str:
    """First-boot script. Idempotent so a re-run after reboot is safe.

    The repo arrives on a secondary virtio block device — not as a filesystem,
    just the raw tar.gz bytes (zero-padded to a 1 MiB boundary). We find the
    device by scanning for the gzip magic (0x1f 0x8b) at byte 0, then pipe
    through tar -xz which stops at the gzip end-of-stream marker, ignoring
    the trailing zeros.
    """
    return f"""#!/bin/bash
set -euo pipefail
exec > >(tee -a /var/log/hypercore-bootstrap.log) 2>&1
echo "=== HyperCore Manager bootstrap @ $(date -Iseconds) ==="

REPO_DEST="{repo_dest}"
LINUX_USER="{user}"

# 1. Apply static network config first if present, so apt below has network
#    even on subnets without a DHCP server.
if [ -f /etc/netplan/99-hcm-static.yaml ]; then
    echo "Applying static netplan config"
    rm -f /etc/netplan/50-cloud-init.yaml /etc/netplan/00-installer-config.yaml || true
    chmod 600 /etc/netplan/99-hcm-static.yaml
    netplan apply || true
    sleep 3
fi

# 2. Wait until DNS works (handles slow link-up after netplan apply).
for i in 1 2 3 4 5 6 7 8 9 10; do
    if getent hosts archive.ubuntu.com >/dev/null 2>&1; then break; fi
    echo "waiting for network ($i/10)"
    sleep 3
done

# 3. Install docker + supporting tools.
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends \\
    docker.io docker-compose-v2 git tar ca-certificates qemu-guest-agent

# We deliberately do NOT start qemu-guest-agent here. Starting it now would
# cause HyperCore to publish the VM's IP via netDevs.ipv4Addresses while
# docker compose is still building images — the deploy wizard would see
# the IP and declare success even though the dashboard isn't reachable yet.
# Instead we enable+start it at the very end of this script (after
# `docker compose up -d`), turning "agent reports IP" into "stack is up".

# Add the Linux user to the docker group now that the group exists.
if id "$LINUX_USER" >/dev/null 2>&1 && getent group docker >/dev/null 2>&1; then
    usermod -aG docker "$LINUX_USER"
fi

# 4. Extract the repo on first run only. Re-runs (after reboot) skip this.
mkdir -p "$REPO_DEST"

find_payload_dev() {{
    # Non-root virtio/scsi block devices whose first two bytes are gzip magic 1F 8B.
    for dev in /dev/vd? /dev/sd? /dev/xvd?; do
        [ -b "$dev" ] || continue
        if grep -q "^${{dev}} " /proc/mounts 2>/dev/null; then continue; fi
        if grep -q "^${{dev}}[0-9] " /proc/mounts 2>/dev/null; then continue; fi
        magic=$(dd if="$dev" bs=2 count=1 2>/dev/null | od -An -tx1 | tr -d ' \\n')
        if [ "$magic" = "1f8b" ]; then
            echo "$dev"
            return 0
        fi
    done
    return 1
}}

if [ ! -f "$REPO_DEST/docker-compose.yaml" ]; then
    PAYLOAD_DEV=$(find_payload_dev) || {{
        echo "FATAL: could not find payload disk (no block device with gzip magic)" >&2
        exit 1
    }}
    echo "Extracting repo from $PAYLOAD_DEV"
    tar -xzf "$PAYLOAD_DEV" -C "$REPO_DEST" --strip-components=1
fi

# 5. Permissions.
chown -R "$LINUX_USER":"$LINUX_USER" "$REPO_DEST"
chmod 600 "$REPO_DEST/.env"

# 6. Bring up the stack.
systemctl enable --now docker
cd "$REPO_DEST"
docker compose {profile_str} pull --ignore-pull-failures || true
docker compose {profile_str} build
docker compose {profile_str} up -d

# 7. Now — and only now — start qemu-guest-agent. The agent publishes the
#    VM's IP to HyperCore, which the deploy wizard polls as its "success"
#    signal. By holding off until the stack is actually up, that IP-report
#    means the dashboard is reachable rather than just "VM has booted".
echo "Starting qemu-guest-agent — wizard will see IP shortly"
systemctl enable --now qemu-guest-agent || true

echo "=== bootstrap done @ $(date -Iseconds) ==="
"""


def make_instance_id(hostname: str) -> str:
    """Stable-ish instance-id: hostname + a short random suffix so cloud-init
    doesn't think this is the same instance as a previous deploy."""
    suffix = secrets.token_hex(4)
    return f"{hostname}-{suffix}"
