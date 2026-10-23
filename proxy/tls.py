"""
tls.py — HTTPS for proxies reached over a network (remote proxy VMs).

PROXY_TLS=off   plain HTTP (the proxy container inside the stack, on the compose network)
PROXY_TLS=auto  a self-signed certificate is generated on first start under <data_dir>/tls and
                kept; the backup service pins its SHA-256 fingerprint when the proxy is registered
PROXY_TLS=cert  PROXY_TLS_CERT / PROXY_TLS_KEY point at a certificate the customer provides
"""
from __future__ import annotations

import hashlib
import os
import socket
import subprocess


def ensure_self_signed(tls_dir: str, run=subprocess.run) -> tuple[str, str]:
    """Create <tls_dir>/proxy.crt + proxy.key once (10 years, RSA 2048, hostname + IPs as SANs)."""
    os.makedirs(tls_dir, exist_ok=True)
    crt, key = os.path.join(tls_dir, 'proxy.crt'), os.path.join(tls_dir, 'proxy.key')
    if os.path.exists(crt) and os.path.exists(key):
        return crt, key
    host = socket.gethostname()
    sans = [f'DNS:{host}', 'DNS:localhost', 'IP:127.0.0.1']
    try:
        for ip in {i[4][0] for i in socket.getaddrinfo(host, None, socket.AF_INET)}:
            sans.append(f'IP:{ip}')
    except socket.gaierror:
        pass
    r = run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '3650', '-keyout', key, '-out', crt,
             '-subj', f'/CN={host}', '-addext', f'subjectAltName={",".join(sans)}'], capture_output=True, text=True)
    if getattr(r, 'returncode', 0):
        raise RuntimeError(f'certificate generation failed: {(r.stderr or r.stdout or "").strip()[:300]}')
    os.chmod(key, 0o600)
    return crt, key


def fingerprint(cert_path: str) -> str | None:
    """sha256 of the DER certificate, lowercase hex, as the backup service pins it."""
    try:
        import ssl
        with open(cert_path) as fh:
            pem = fh.read()
        der = ssl.PEM_cert_to_DER_cert(pem)
        return hashlib.sha256(der).hexdigest()
    except (OSError, ValueError):
        return None


def context_for(cfg):
    """(ssl context tuple for Flask, fingerprint) or (None, None) for plain HTTP."""
    mode = (cfg.tls or 'off').lower()
    if mode == 'off':
        return None, None
    if mode == 'cert':
        crt, key = cfg.tls_cert, cfg.tls_key
        if not (crt and key and os.path.exists(crt) and os.path.exists(key)):
            raise RuntimeError('PROXY_TLS=cert needs PROXY_TLS_CERT and PROXY_TLS_KEY pointing at existing files')
    else:
        crt, key = ensure_self_signed(os.path.join(cfg.data_dir, 'tls'))
    return (crt, key), fingerprint(crt)
