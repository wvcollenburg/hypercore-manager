"""Ubuntu cloud-image downloader / cache.

Cache lives in %LOCALAPPDATA%/HyperCoreDeploy/images. We download once per
release and reuse on subsequent deploys. SHA-256 is verified against
SHA256SUMS published next to the image.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import ssl
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional


# Ubuntu's "release" channel is stable; "current" follows latest minor
DEFAULT_IMAGE_NAME = "ubuntu-24.04-server-cloudimg-amd64.img"
DEFAULT_IMAGE_URL = f"https://cloud-images.ubuntu.com/releases/24.04/release/{DEFAULT_IMAGE_NAME}"
DEFAULT_SHA256_URL = "https://cloud-images.ubuntu.com/releases/24.04/release/SHA256SUMS"


def _build_ssl_context() -> ssl.SSLContext:
    """Frozen PyInstaller builds on Windows can't reach the OS cert store, so
    stdlib SSL fails with 'unable to get local issuer certificate'. Prefer
    certifi's bundled CA (which PyInstaller picks up automatically), and fall
    back to the system default for source runs where certifi isn't installed.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


_SSL_CONTEXT = _build_ssl_context()


class ImageCacheError(Exception):
    pass


def cache_dir() -> Path:
    """Per-user cache directory. Falls back to a temp dir on non-Windows hosts."""
    appdata = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME")
    if appdata:
        d = Path(appdata) / "HyperCoreDeploy" / "images"
    else:
        d = Path(tempfile.gettempdir()) / "hypercore-deploy-images"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _download(url: str, dest: Path, progress: Optional[Callable[[int, int], None]] = None) -> None:
    """Stream a download to dest with progress callback."""
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=60, context=_SSL_CONTEXT) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            with open(tmp, "wb") as out:
                while True:
                    chunk = resp.read(1024 * 256)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    if progress and total:
                        progress(done, total)
        tmp.replace(dest)
    except urllib.error.URLError as e:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise ImageCacheError(f"download failed: {url} ({e.reason})") from e
    except Exception:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise


def _expected_sha256(sha_url: str, image_name: str) -> Optional[str]:
    """Pull SHA256SUMS and look up the image's hash. Returns None if unavailable."""
    try:
        with urllib.request.urlopen(sha_url, timeout=30, context=_SSL_CONTEXT) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.URLError:
        return None
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        digest, name = parts[0], parts[-1].lstrip("*")
        if name == image_name:
            return digest
    return None


def _hash_file(path: Path, progress: Optional[Callable[[int, int], None]] = None) -> str:
    h = hashlib.sha256()
    size = path.stat().st_size
    done = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
            done += len(chunk)
            if progress and size:
                progress(done, size)
    return h.hexdigest()


def ensure_image(
    *,
    image_url: str = DEFAULT_IMAGE_URL,
    sha256_url: str = DEFAULT_SHA256_URL,
    image_name: str = DEFAULT_IMAGE_NAME,
    progress: Optional[Callable[[str, int, int], None]] = None,
    verify: bool = True,
) -> Path:
    """Return a path to a cached Ubuntu cloud image, downloading it if needed.

    progress is called as `progress(stage, bytes_done, bytes_total)` where stage
    is one of: 'download', 'verify'.
    """
    target = cache_dir() / image_name

    expected = _expected_sha256(sha256_url, image_name) if verify else None

    def _stage(name: str):
        return (lambda d, t: progress(name, d, t)) if progress else None

    if target.exists():
        if not verify or not expected:
            return target
        actual = _hash_file(target, _stage("verify"))
        if actual == expected:
            return target
        # corrupt or stale — re-download
        target.unlink(missing_ok=True)

    _download(image_url, target, _stage("download"))
    if verify and expected:
        actual = _hash_file(target, _stage("verify"))
        if actual != expected:
            target.unlink(missing_ok=True)
            raise ImageCacheError(
                f"SHA-256 mismatch for {image_name}: expected {expected}, got {actual}"
            )
    return target


def clear_cache() -> None:
    """Delete the cached image directory entirely."""
    d = cache_dir()
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
