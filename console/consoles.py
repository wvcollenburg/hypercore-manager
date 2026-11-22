"""
consoles.py — one-time console tickets, the websocket relay and server-side screenshots.

Ticket flow (the browser never sees HyperCore's session):
  1. The console page asks the dashboard for a ticket. The dashboard checks the user (admin) and
     the cluster, and asks this service to mint one for (cluster, VM, user, binding). `binding` is a
     hash of the user's HCM browser session; the VM is checked to be running before a ticket exists.
  2. The page opens the dashboard's websocket with the ticket. The dashboard checks the HCM session
     again, recomputes the binding from it and connects here with ticket + binding.
  3. The ticket is consumed on first use (valid or not), expires after TICKET_TTL seconds and only
     opens the one VM it names. A stolen ticket is worthless after use, after 30 s, and in any other
     browser session.

The relay pumps bytes both ways, closes after CONSOLE_IDLE_MINUTES without user input (screen-update
requests from an idle noVNC do not count) or after CONSOLE_MAX_HOURS, and writes every session to
console.sessions (who, VM, from where, when, how long, why it ended).
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Optional

import aiohttp
from aiohttp import web

import hc
import rfb
import store

log = logging.getLogger('console.relay')

TICKET_TTL = 30
IDLE_MINUTES = float(os.getenv('CONSOLE_IDLE_MINUTES', '15') or 15)
MAX_HOURS = float(os.getenv('CONSOLE_MAX_HOURS', '8') or 8)


@dataclass
class Ticket:
    cluster_id: str
    vm_uuid: str
    vm_name: str
    username: str
    binding: str
    client_ip: Optional[str]
    source: str = 'user'                 # user | guest (an outsider, plans/PLAN-roles.md R4) | watch (read-only)
    view_only: bool = False              # input from the browser is dropped here, whatever the page does
    max_minutes: Optional[float] = None  # shorter than MAX_HOURS for a guest grant
    grant_id: Optional[int] = None
    expires: float = field(default_factory=lambda: time.monotonic() + TICKET_TTL)


_tickets: dict[str, Ticket] = {}
active: dict[int, dict] = {}            # audit id -> live session info (for the admin view)
_ending: dict[int, str] = {}            # audit id -> why it was ended from outside (revoked, ended by ...)


def mint(cluster_id: str, vm_uuid: str, vm_name: str, username: str, binding: str, client_ip: Optional[str], *,
         source: str = 'user', view_only: bool = False, max_minutes: Optional[float] = None,
         grant_id: Optional[int] = None) -> str:
    now = time.monotonic()
    for k in [k for k, t in _tickets.items() if t.expires < now]:
        del _tickets[k]
    token = secrets.token_urlsafe(32)
    _tickets[token] = Ticket(cluster_id, vm_uuid, vm_name, username, binding, client_ip, source=source,
                             view_only=view_only, max_minutes=max_minutes, grant_id=grant_id)
    return token


def end(session_id: int, reason: str) -> bool:
    """Ask a live session to close (a revoked guest grant, 'end session' by a granter)."""
    if session_id not in active:
        return False
    _ending[session_id] = reason[:120]
    return True


def consume(token: str, binding: str) -> Optional[Ticket]:
    """The ticket if it is valid for this binding; it is gone afterwards either way."""
    t = _tickets.pop(token or '', None)
    if t is None or t.expires < time.monotonic():
        return None
    if not binding or not hmac.compare_digest(t.binding, binding):
        return None
    return t


_SESSION_RE = re.compile(r'(sessionID=)[^&\s\'"]*', re.I)


def mask_session(text: str) -> str:
    """`text` with any HyperCore sessionID in it masked."""
    return _SESSION_RE.sub(r'\1***', text)


def _upstream_failure(e: BaseException) -> str:
    """What went wrong connecting to the cluster's console, never its URL: that carries HyperCore's
    sessionID, and this text goes to the log, the check details on the dashboard and API errors.
    aiohttp's WSServerHandshakeError (a ClientResponseError) renders as status, message and URL."""
    if isinstance(e, aiohttp.ClientResponseError):
        return f'the cluster answered HTTP {e.status}'
    if isinstance(e, asyncio.TimeoutError):
        return 'timed out'
    if isinstance(e, OSError):                       # connect errors: host, port and the OS error
        return mask_session(str(e))
    return type(e).__name__


async def open_upstream(http: aiohttp.ClientSession, cluster_id: str, vm_uuid: str):
    """(websocket to HyperCore's console of this VM, target). Raises hc.ConsoleError."""
    target = await asyncio.to_thread(hc.console_target, cluster_id, vm_uuid)
    try:
        ws = await http.ws_connect(target.url, protocols=('binary',), ssl=target.ssl, heartbeat=30,
                                   max_msg_size=0, timeout=aiohttp.ClientWSTimeout(ws_close=5),
                                   autoping=True)
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
        raise hc.ConsoleError(f'console connection to the cluster failed: {_upstream_failure(e)}', 502) from None
    return ws, target


async def relay(request: web.Request, http: aiohttp.ClientSession) -> web.WebSocketResponse:
    """GET /api/v1/relay?ticket=&binding= (websocket), called by the dashboard."""
    ticket = consume(request.query.get('ticket', ''), request.query.get('binding', ''))
    down = web.WebSocketResponse(max_msg_size=0, heartbeat=30)
    await down.prepare(request)
    if ticket is None:
        await down.close(code=4401, message=b'invalid or expired console ticket')
        return down
    try:
        up, target = await open_upstream(http, ticket.cluster_id, ticket.vm_uuid)
    except hc.ConsoleError as e:
        log.warning('console for %s on %s refused: %s', ticket.vm_uuid, ticket.cluster_id, e)
        await down.close(code=4502, message=str(e)[:120].encode())
        return down

    try:
        sid = await asyncio.to_thread(store.session_start, ticket.cluster_id, ticket.vm_uuid, target.vm_name,
                                      ticket.username, client_ip=ticket.client_ip, source=ticket.source,
                                      guest_grant_id=ticket.grant_id)
    except Exception:  # noqa: BLE001 — no audit record, no console; the cluster's websocket must not stay open
        log.exception('console session for %s could not be recorded; not opened', ticket.vm_uuid)
        await up.close()
        await down.close(code=4502, message=b'the console session could not be recorded')
        return down
    started = time.monotonic()
    state = {'last_input': started, 'in': 0, 'out': 0}
    max_s = min(MAX_HOURS * 3600, (ticket.max_minutes or 1e9) * 60)
    active[sid] = {'id': sid, 'cluster_id': ticket.cluster_id, 'vm_uuid': ticket.vm_uuid,
                   'vm_name': target.vm_name, 'username': ticket.username, 'started': time.time(),
                   'source': ticket.source, 'view_only': ticket.view_only, 'grant_id': ticket.grant_id}
    log.info('console opened', extra={'session': sid, 'user': ticket.username, 'vm': target.vm_name,
                                      'vm_uuid': ticket.vm_uuid, 'cluster_id': ticket.cluster_id,
                                      'client_ip': ticket.client_ip})

    view_filter = rfb.ViewOnlyFilter() if ticket.view_only else None

    async def browser_to_cluster():
        async for msg in down:
            if msg.type == aiohttp.WSMsgType.BINARY:
                data = msg.data
            elif msg.type == aiohttp.WSMsgType.TEXT:
                data = msg.data.encode()
            else:
                break
            state['in'] += len(data)
            if view_filter is not None:
                data = view_filter.feed(data)  # read-only: only screen-update traffic reaches the VM
                if not data:
                    continue
            if rfb.is_user_input(data, True):
                state['last_input'] = time.monotonic()
            await up.send_bytes(data)
        return 'closed by the browser'

    async def cluster_to_browser():
        async for msg in up:
            if msg.type != aiohttp.WSMsgType.BINARY:
                break
            state['out'] += len(msg.data)
            await down.send_bytes(msg.data)
        return 'closed by the cluster'

    async def limits():
        while True:
            await asyncio.sleep(2)
            now = time.monotonic()
            if sid in _ending:
                return _ending.pop(sid)
            if not ticket.view_only and now - state['last_input'] > IDLE_MINUTES * 60:
                return f'idle for {IDLE_MINUTES:g} minutes'
            if now - started > max_s:
                return f'maximum duration of {max_s / 60:g} minutes reached'

    tasks = [asyncio.create_task(c()) for c in (browser_to_cluster, cluster_to_browser, limits)]
    reason = 'ended'
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            try:
                reason = t.result() or reason
            except Exception as e:  # noqa: BLE001 — a dropped socket mid-send
                reason = mask_session(f'connection error: {e}')[:200]
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await up.close()
        if not down.closed:
            await down.close(code=4000 if 'idle' in reason or 'maximum' in reason else 1000,
                             message=reason[:120].encode())
        active.pop(sid, None)
        _ending.pop(sid, None)
        await asyncio.to_thread(store.session_end, sid, reason, state['in'], state['out'])
        log.info('console closed', extra={'session': sid, 'user': ticket.username, 'vm': target.vm_name,
                                          'reason': reason, 'seconds': round(time.monotonic() - started)})
    return down


async def screenshot(http: aiohttp.ClientSession, cluster_id: str, vm_uuid: str, *, username: str,
                     source: str = 'user', wake: str = 'none'):
    """(PIL image, vm name) of the VM's console right now, audited as a screenshot session."""
    up, target = await open_upstream(http, cluster_id, vm_uuid)
    sid, reason = None, 'ok'
    try:
        sid = await asyncio.to_thread(store.session_start, cluster_id, vm_uuid, target.vm_name, username,
                                      source=source, kind='screenshot')
        img = await rfb.screenshot(up, wake=wake)
        return img, target.vm_name
    except rfb.RFBError as e:
        reason = f'failed: {e}'[:200]
        raise hc.ConsoleError(f'screenshot failed: {e}', 502) from None
    finally:
        await up.close()                                # also when the audit record could not be written
        if sid is not None:
            await asyncio.to_thread(store.session_end, sid, reason)
