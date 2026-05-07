"""Build the Windows deploy wizard into a single .exe via PyInstaller.

Usage:
    pip install pyinstaller certifi
    python -m installer.windows.build

Output:
    dist/HyperCoreDeploy.exe        — the wizard binary (~15-25 MB)
    dist/HyperCoreDeploy-<ver>.zip  — versioned distribution zip (.exe + README)

The hypercore-manager repo is bundled INTO the .exe as a tar.gz produced from
the working tree at build time. At deploy time the wizard reads it from the
PyInstaller temp dir (sys._MEIPASS), so the .exe is fully self-contained — no
git checkout required on the deploy host. The Ubuntu cloud image is downloaded
on first use and cached under %LOCALAPPDATA%.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ENTRY = REPO_ROOT / "installer" / "windows" / "wizard.py"
DIST = REPO_ROOT / "dist"
BUILD = REPO_ROOT / "build"


def _read_version() -> str:
    """Best-effort version: short git SHA, else 'dev'."""
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT, stderr=subprocess.DEVNULL
        )
        return out.decode().strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "dev"


def _build_payload_tarball(out_path: Path) -> int:
    """Bundle the repo into a tar.gz at out_path. Returns the file size."""
    sys.path.insert(0, str(REPO_ROOT))
    from installer.windows.cloudinit import build_repo_tarball
    data = build_repo_tarball(REPO_ROOT)
    out_path.write_bytes(data)
    return len(data)


def main() -> int:
    if sys.platform != "win32":
        print("WARNING: this build script is intended for Windows.", file=sys.stderr)

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller is not installed. Run:  pip install pyinstaller", file=sys.stderr)
        return 1

    if BUILD.exists():
        shutil.rmtree(BUILD, ignore_errors=True)
    if DIST.exists():
        shutil.rmtree(DIST, ignore_errors=True)

    # 1. Build the repo tarball that gets baked into the .exe.
    payload_dir = REPO_ROOT / "build" / "payload"
    payload_dir.mkdir(parents=True, exist_ok=True)
    payload_tarball = payload_dir / "repo.tar.gz"
    size = _build_payload_tarball(payload_tarball)
    print(f"Built payload tarball: {payload_tarball.name}  ({size / 1024:.1f} KB)")

    # 2. Run PyInstaller. --add-data uses ';' on Windows, ':' elsewhere.
    sep = ";" if sys.platform == "win32" else ":"
    icon_path = REPO_ROOT / "installer" / "favicon.ico"
    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        # UPX-packed PyInstaller binaries trip Windows Defender heuristics
        # ("Wacatac"/"Sabsik" false positives), which surfaces to end users
        # as "Windows cannot access the specified device, path or file".
        # Skip UPX until the .exe is code-signed.
        "--noupx",
        "--onefile",
        "--windowed",
        "--name", "HyperCoreDeploy",
        "--add-data", f"{payload_tarball}{sep}payload",
        "--collect-submodules", "installer",
        "--paths", str(REPO_ROOT),
    ]
    if icon_path.exists():
        # --icon: shows in Windows Explorer / Start menu / taskbar.
        # --add-data: bundles the .ico under sys._MEIPASS/installer/ so the
        #            wizard can call self.iconbitmap() at runtime for the
        #            title-bar icon (separate from the .exe-file icon).
        args += ["--icon", str(icon_path)]
        args += ["--add-data", f"{icon_path}{sep}installer"]
    else:
        print(f"WARNING: {icon_path} missing — building with default icon.", file=sys.stderr)
    args.append(str(ENTRY))
    print("Running:", " ".join(str(a) for a in args))
    rc = subprocess.call(args, cwd=REPO_ROOT)
    if rc != 0:
        return rc

    exe = DIST / "HyperCoreDeploy.exe"
    if not exe.exists():
        print(f"Expected {exe} but it wasn't produced.", file=sys.stderr)
        return 1

    version = _read_version()
    zip_path = DIST / f"HyperCoreDeploy-{version}.zip"
    readme_src = REPO_ROOT / "installer" / "windows" / "README.md"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(exe, arcname="HyperCoreDeploy.exe")
        if readme_src.exists():
            z.write(readme_src, arcname="README.md")
    print(f"Wrote {zip_path} ({zip_path.stat().st_size / 1024 / 1024:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
