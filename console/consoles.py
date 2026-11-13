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
    expires: float = field(default_factory=lambda: time.monotonic() + TICKET_TTL)


_tickets: dict[str, Ticket] = {}
active: dict[int, dict] = {}            # audit id -> live session info (for the admin view)


def mint(cluster_id: str, vm_uuid: str, vm_name: str, username: str, binding: str, client_ip: Optional[str]) -> str:
    now = time.monotonic()
    for k in [k for k, t in _tickets.items() if t.expires < now]:
        del _tickets[k]
    token = secrets.token_urlsafe(32)
    _tickets[token] = Ticket(cluster_id, vm_uuid, vm_name, username, binding, client_ip)
    return token


def consume(token: str, binding: str) -> Optional[Ticket]:
    """The ticket if it is valid for this binding; it is gone afterwards either way."""
    t = _tickets.pop(token or '', None)
    if t is None or t.expires < time.monotonic():
        return None
    if not binding or not hmac.compare_digest(t.binding, binding):
        return None
    return t


async def open_upstream(http: aiohttp.ClientSession, cluster_id: str, vm_uuid: str):
    """(websocket to HyperCore's console of this VM, target). Raises hc.ConsoleError."""
    target = await asyncio.to_thread(hc.console_target, cluster_id, vm_uuid)
    try:
        ws = await http.ws_connect(target.url, protocols=('binary',), ssl=target.ssl, heartbeat=30,
                                   max_msg_size=0, timeout=aiohttp.ClientWSTimeout(ws_close=5),
                                   autoping=True)
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
        raise hc.ConsoleError(f'console connection to the cluster failed: {e}', 502) from None
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

    sid = await asyncio.to_thread(store.session_start, ticket.cluster_id, ticket.vm_uuid, target.vm_name,
                                  ticket.username, client_ip=ticket.client_ip)
    started = time.monotonic()
    state = {'last_input': started, 'in': 0, 'out': 0}
    active[sid] = {'id': sid, 'cluster_id': ticket.cluster_id, 'vm_uuid': ticket.vm_uuid,
                   'vm_name': target.vm_name, 'username': ticket.username, 'started': time.time()}
    log.info('console opened', extra={'session': sid, 'user': ticket.username, 'vm': target.vm_name,
                                      'vm_uuid': ticket.vm_uuid, 'cluster_id': ticket.cluster_id,
                                      'client_ip': ticket.client_ip})

    async def browser_to_cluster():
        async for msg in down:
            if msg.type == aiohttp.WSMsgType.BINARY:
                data = msg.data
            elif msg.type == aiohttp.WSMsgType.TEXT:
                data = msg.data.encode()
            else:
                break
            state['in'] += len(data)
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
            await asyncio.sleep(5)
            now = time.monotonic()
            if now - state['last_input'] > IDLE_MINUTES * 60:
                return f'idle for {IDLE_MINUTES:g} minutes'
            if now - started > MAX_HOURS * 3600:
                return f'maximum duration of {MAX_HOURS:g} hours reached'

    tasks = [asyncio.create_task(c()) for c in (browser_to_cluster, cluster_to_browser, limits)]
    reason = 'ended'
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            try:
                reason = t.result() or reason
            except Exception as e:  # noqa: BLE001 — a dropped socket mid-send
                reason = f'connection error: {e}'[:200]
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await up.close()
        if not down.closed:
            await down.close(code=4000 if 'idle' in reason or 'maximum' in reason else 1000,
                             message=reason[:120].encode())
        active.pop(sid, None)
        await asyncio.to_thread(store.session_end, sid, reason, state['in'], state['out'])
        log.info('console closed', extra={'session': sid, 'user': ticket.username, 'vm': target.vm_name,
                                          'reason': reason, 'seconds': round(time.monotonic() - started)})
    return down


async def screenshot(http: aiohttp.ClientSession, cluster_id: str, vm_uuid: str, *, username: str,
                     source: str = 'user', wake: str = 'none'):
    """(PIL image, vm name) of the VM's console right now, audited as a screenshot session."""
    up, target = await open_upstream(http, cluster_id, vm_uuid)
    sid = await asyncio.to_thread(store.session_start, cluster_id, vm_uuid, target.vm_name, username,
                                  source=source, kind='screenshot')
    reason = 'ok'
    try:
        img = await rfb.screenshot(up, wake=wake)
        return img, target.vm_name
    except rfb.RFBError as e:
        reason = f'failed: {e}'[:200]
        raise hc.ConsoleError(f'screenshot failed: {e}', 502) from None
    finally:
        await up.close()
        await asyncio.to_thread(store.session_end, sid, reason)
