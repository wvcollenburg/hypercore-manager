"""
proxy_image.py — the proxy image travels from this installation to the proxy VMs, pushed.

The dashboard container builds the proxy image together with the rest of the stack and can
export it (`docker save`) over the compose network. This module streams that export to a
proxy VM: to the bootstrap receiver of a freshly deployed VM, or to a running proxy's
/update hook. The VM loads the tarball and restarts its container on the new tag. The proxy
version therefore always matches the installation that drives it, no registry involved.

Pushing instead of pulling: the backup service already holds an authenticated channel to every
proxy, and a VM on a customer VLAN never needs a route to the stack or to the internet.
"""
from __future__ import annotations

import logging
import tempfile
import time
from typing import Callable, Iterable

import requests

import config as cfg_module
import db
from proxy_client import ProxyClient, ProxyError

log = logging.getLogger('backup.proxy_image')
IMAGE_NAME = 'hcm-proxy'
_cache: dict = {'at': 0.0, 'info': None}


def _dashboard() -> tuple[str, dict]:
    cfg = cfg_module.load()
    headers = {'Authorization': f'Bearer {cfg.api_token}'} if cfg.api_token else {}
    return cfg.dashboard_url.rstrip('/'), headers


def stack_image(max_age: float = 60.0) -> dict:
    """{version, tag, available, detail} of the proxy image this installation carries, from the
    dashboard. Cached briefly: the proxies panel asks on every refresh."""
    now = time.monotonic()
    if _cache['info'] is not None and now - _cache['at'] < max_age:
        return _cache['info']
    url, headers = _dashboard()
    try:
        r = requests.get(f'{url}/internal/proxy-image/version', headers=headers, timeout=10)
        info = r.json() if r.status_code == 200 else {'available': False, 'detail': f'dashboard answered {r.status_code}'}
    except (requests.RequestException, ValueError) as e:
        info = {'available': False, 'detail': f'dashboard not reachable: {e}'}
    info.setdefault('version', None)
    info.setdefault('tag', f"{IMAGE_NAME}:{info.get('version')}" if info.get('version') else None)
    _cache.update(at=now, info=info)
    return info


def stream_stack_image(chunk_size: int = 1 << 20) -> tuple[str, Iterable[bytes]]:
    """(tag, body) of the dashboard's export. `docker save` does not know its size up front, and
    the receivers want a Content-Length, so the export is spooled to a temporary file here
    (tens of MiB) and handed over as a file object; it is deleted when closed."""
    url, headers = _dashboard()
    try:
        r = requests.get(f'{url}/internal/proxy-image', headers=headers, stream=True, timeout=(10, 600))
    except requests.RequestException as e:
        raise ProxyError(f'the dashboard could not export the proxy image: {e}') from e
    if r.status_code != 200:
        try:
            detail = r.json().get('error') or r.text[:200]
        except ValueError:
            detail = r.text[:200]
        raise ProxyError(f'the dashboard could not export the proxy image ({r.status_code}): {detail}')
    tag = r.headers.get('X-Image-Tag') or ''
    if not tag:
        raise ProxyError('the dashboard export carries no image tag')
    spool = tempfile.TemporaryFile(prefix='hcm-proxy-image-')
    for chunk in r.iter_content(chunk_size=chunk_size):
        spool.write(chunk)
    if not spool.tell():
        spool.close()
        raise ProxyError('the dashboard export is empty (docker save failed on the dashboard host)')
    spool.seek(0)
    return tag, spool


def push(client: ProxyClient, source: Callable[[], tuple[str, Iterable[bytes]]] | None = None) -> dict:
    """Push the installation's proxy image to one proxy (or bootstrap receiver). Returns the
    proxy's answer with the tag added."""
    tag, body = (source or stream_stack_image)()
    try:
        answer = client.push_image(tag, body)
    finally:
        if hasattr(body, 'close'):
            body.close()
    answer['tag'] = tag
    return answer


def prepare(proxy_id: int, *, force: bool = False, client_factory=None, info=stack_image) -> dict:
    """The checks before a push, quick enough for an API call: the proxy exists and is remote,
    the installation has an amd64 image, the proxy answers, is behind (or force), has the hook
    and is idle. Returns {proxy, client, current, stack} or {skipped}; raises ProxyError."""
    p = db.get_proxy(proxy_id, with_secret=True)
    if not p:
        raise ProxyError('proxy not found')
    if p.get('kind') == 'local':
        return {'skipped': 'the proxy in this stack is updated with the stack'}
    if client_factory is None:
        from incremental import proxy_client_for               # local import: incremental imports db and jobs
        client_factory = proxy_client_for
    stack = info()
    if not stack.get('available'):
        raise ProxyError(f"the installation has no proxy image to push: {stack.get('detail') or 'the dashboard did not report one'}")
    if stack.get('arch') not in (None, 'amd64'):
        raise ProxyError(f"the installation's proxy image is built for {stack['arch']}; a HyperCore VM needs amd64 "
                         '(build the stack on an x86_64 host, or build the proxy image with --platform linux/amd64)')
    client = client_factory(p)
    ident = client.identity()
    current = ident.get('version')
    if current == stack['version'] and not force:
        return {'skipped': 'already on this version', 'version': current}
    if not (ident.get('capabilities') or {}).get('update'):
        raise ProxyError(f"proxy runs {current}, the installation {stack['version']}; it has no update hook, update its container by hand")
    if client.active_jobs():
        raise ProxyError('a job is running on this proxy; retry when it is idle')
    return {'proxy': p, 'client': client, 'current': current, 'stack': stack}


def update_proxy(proxy_id: int, *, force: bool = False, client_factory=None, source=None,
                 info=stack_image, sleep=time.sleep, wait_seconds: float = 240) -> dict:
    """Bring one registered proxy to the installation's version: prepare(), push, then wait for it
    to come back on the new version. Raises ProxyError when the proxy cannot be updated this
    way; returns {'ok', 'version', 'skipped'?}."""
    pre = prepare(proxy_id, force=force, client_factory=client_factory, info=info)
    if pre.get('skipped'):
        return {'ok': True, **pre}
    client, current, stack = pre['client'], pre['current'], pre['stack']
    db.update_proxy(proxy_id, status='updating', status_detail=f"updating {current} -> {stack['version']}")
    push_error = None
    try:
        answer = push(client, source)
        log.info('proxy %s: image %s pushed (%s bytes), waiting for the restart', proxy_id, answer.get('tag'), answer.get('bytes'))
    except ProxyError as e:
        # the VM may have swapped the container before the answer got out: the version decides
        push_error = e
        log.warning('proxy %s: push answered with an error (%s); watching for the new version anyway', proxy_id, e)
    # the host loads the tarball and restarts the container: poll until the new version answers
    deadline = time.monotonic() + wait_seconds
    seen = None
    while time.monotonic() < deadline:
        sleep(5)
        try:
            ident = client.identity()
        except ProxyError:
            continue                                            # the container is being swapped
        seen = ident.get('version')
        if seen == stack['version']:
            db.update_proxy(proxy_id, status='ok', status_detail=None, version=seen, capabilities=ident.get('capabilities'))
            return {'ok': True, 'version': seen}
    if push_error is not None:
        db.update_proxy(proxy_id, status='error', status_detail=f'update failed: {push_error}')
        raise push_error
    db.update_proxy(proxy_id, status='error', status_detail=f"pushed {stack['version']} but the proxy still reports {seen or 'nothing'}; "
                                                              '/var/log/hcm-proxy-update.log on the VM says why')
    raise ProxyError(f"the proxy did not come back on {stack['version']} within {wait_seconds:.0f}s")


def update_all(**kw) -> list[dict]:
    """After the stack restarted (every update does), bring every idle remote proxy along. A busy
    or hand-built proxy is reported, not forced."""
    out = []
    for p in db.get_proxies():
        if p.get('kind') == 'local' or not p.get('enabled'):
            continue
        try:
            r = update_proxy(p['id'], **kw)
            out.append({'id': p['id'], 'name': p['name'], **r})
        except ProxyError as e:
            out.append({'id': p['id'], 'name': p['name'], 'ok': False, 'error': str(e)})
    for r in out:
        if r.get('ok') and not r.get('skipped'):
            log.info("proxy '%s' updated to %s", r['name'], r.get('version'))
        elif not r.get('ok'):
            log.warning("proxy '%s' not updated: %s", r['name'], r.get('error'))
    return out
