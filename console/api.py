"""
api.py — internal REST + websocket API of the console service (port 8084, compose network only).

Only the dashboard calls it, with a bearer token derived from HCM_SECRET_KEY (hcm_crypto.derived_token
('console')); the dashboard does the user checks (admin, cluster visible) and names the user in the body.
"""
from __future__ import annotations

import asyncio
import base64
import hmac
from datetime import date, datetime

import aiohttp
from aiohttp import web

import checker
import classify
import cluster_registry
import consoles
import hc
import store

WAKE = ('none', 'pointer', 'shift')


def _plain(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, (bytes, memoryview)):
        return None
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_plain(x) for x in v]
    return v


def _json(data, status: int = 200) -> web.Response:
    return web.json_response(_plain(data), status=status)


def _err(msg: str, status: int = 400) -> web.Response:
    return web.json_response({'error': msg}, status=status)


def _cluster(request: web.Request, body: dict | None = None) -> str:
    cid = ((body or {}).get('cluster_id') or request.query.get('cluster') or '').strip()
    if not cluster_registry.is_uuid(cid):
        raise web.HTTPBadRequest(text='{"error": "cluster required"}', content_type='application/json')
    return cid


def _vm(request: web.Request, body: dict | None = None) -> str:
    vm = ((body or {}).get('vm_uuid') or request.query.get('vm') or '').strip()
    if not vm or len(vm) > 64 or not all(c.isalnum() or c == '-' for c in vm):
        raise web.HTTPBadRequest(text='{"error": "vm_uuid required"}', content_type='application/json')
    return vm


async def _body(request: web.Request) -> dict:
    try:
        b = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    return b if isinstance(b, dict) else {}


def build(token: str, http: aiohttp.ClientSession) -> web.Application:
    @web.middleware
    async def auth(request: web.Request, handler):
        if request.path != '/health':
            got = request.headers.get('Authorization', '')
            if not token or not hmac.compare_digest(got, f'Bearer {token}'):
                return _err('Unauthorized', 401)
        try:
            return await handler(request)
        except hc.ConsoleError as e:
            return _err(str(e), e.status)

    app = web.Application(middlewares=[auth])
    r = app.router

    async def health(_):
        return _json({'ok': True, 'ocr': classify.ocr_available()})

    # ------------------------------------------------------------ consoles
    async def ticket(request):
        b = await _body(request)
        cid, vm = _cluster(request, b), _vm(request, b)
        binding = str(b.get('binding') or '')
        if len(binding) < 32:
            return _err('binding required')
        target = await asyncio.to_thread(hc.console_target, cid, vm)    # VM running, cluster reachable
        source = b.get('source') if b.get('source') in ('user', 'guest', 'watch') else 'user'
        try:
            max_minutes = float(b['max_minutes']) if b.get('max_minutes') else None
            grant_id = int(b['grant_id']) if b.get('grant_id') else None
        except (TypeError, ValueError):
            return _err('max_minutes and grant_id must be numbers')
        t = consoles.mint(cid, vm, target.vm_name, str(b.get('username') or '?'), binding, b.get('client_ip'),
                          source=source, view_only=bool(b.get('view_only')) or source == 'watch',
                          max_minutes=max_minutes, grant_id=grant_id)
        return _json({'ticket': t, 'ttl': consoles.TICKET_TTL, 'vm_name': target.vm_name,
                      'idle_minutes': consoles.IDLE_MINUTES, 'max_hours': consoles.MAX_HOURS,
                      'max_minutes': max_minutes, 'view_only': bool(b.get('view_only')) or source == 'watch'})

    async def live(request):
        """The console sessions open right now (optionally of one guest grant)."""
        grant = request.query.get('grant')
        rows = [dict(s) for s in consoles.active.values() if not grant or str(s.get('grant_id')) == grant]
        return _json({'live': rows})

    async def end_session(request):
        b = await _body(request)
        sid = int(request.match_info['sid'])
        ok = consoles.end(sid, str(b.get('reason') or 'ended by an administrator'))
        return _json({'ok': ok}, 200 if ok else 404)

    async def end_grant(request):
        b = await _body(request)
        gid = int(request.match_info['gid'])
        ended = [s['id'] for s in list(consoles.active.values()) if s.get('grant_id') == gid
                 and consoles.end(s['id'], str(b.get('reason') or 'guest access ended'))]
        return _json({'ended': ended})

    async def verdict(request):
        """Locked, open or unknown, right now: a guest only connects onto a console showing a login or
        lock screen (the open-session classifier)."""
        b = await _body(request)
        cid, vm = _cluster(request, b), _vm(request, b)
        settings = await asyncio.to_thread(store.get_settings, cid)
        return _json(await checker.check_vm(http, cid, vm, settings, username='guest pre-check', source='system'))

    async def relay(request):
        return await consoles.relay(request, http)

    async def sessions(request):
        cid = request.query.get('cluster') or None
        if cid and not cluster_registry.is_uuid(cid):
            return _err('bad cluster')
        rows = await asyncio.to_thread(store.list_sessions, cid,
                                       include_system=request.query.get('include_system') in ('1', 'true'),
                                       vm_uuid=request.query.get('vm') or None,
                                       limit=int(request.query.get('limit') or 200))
        live = {s['id'] for s in consoles.active.values()}
        for row in rows:
            row['live'] = row['id'] in live
        return _json({'sessions': rows})

    async def screenshot(request):
        b = await _body(request)
        cid, vm = _cluster(request, b), _vm(request, b)
        settings = await asyncio.to_thread(store.get_settings, cid)
        img, _ = await consoles.screenshot(http, cid, vm, username=str(b.get('username') or '?'),
                                           wake=settings.get('wake') or 'none')
        return web.Response(body=await asyncio.to_thread(classify.png, img), content_type='image/png')

    async def bootcheck(request):
        """Test restores: has this restored VM booted? {verdict: pass|fail|wait, detail, png (base64)}. Wakes the
        screen with a Shift tap (harmless on a login screen; a Windows lock screen shows its sign-in prompt)."""
        import base64
        b = await _body(request)
        cid, vm = _cluster(request, b), _vm(request, b)
        try:
            img, _ = await consoles.screenshot(http, cid, vm, username='test restore', source='system', wake='shift')
        except hc.ConsoleError as e:
            return _json({'verdict': 'wait', 'detail': str(e), 'png': None})
        verdict, detail = await asyncio.to_thread(classify.boot_verdict, img, classify.ocr_available())
        data = await asyncio.to_thread(classify.png, img)
        return _json({'verdict': verdict, 'detail': detail, 'png': base64.b64encode(data).decode()})

    # --------------------------------------------------- open-session check
    async def check_get(request):
        cid = _cluster(request)
        settings, vms = await asyncio.gather(asyncio.to_thread(store.get_settings, cid),
                                             asyncio.to_thread(store.check_vms, cid))
        return _json({'settings': settings, 'vms': vms, 'ocr': classify.ocr_available()})

    async def check_settings(request):
        b = await _body(request)
        cid = _cluster(request, b)
        try:
            interval = int(b.get('interval_minutes', 30))
        except (TypeError, ValueError):
            return _err('interval_minutes must be a whole number')
        if not 5 <= interval <= 1440:
            return _err('interval_minutes must be between 5 and 1440')
        wake = b.get('wake', 'none')
        if wake not in WAKE:
            return _err('wake must be one of ' + ', '.join(WAKE))
        await asyncio.to_thread(store.set_settings, cid, bool(b.get('enabled')), interval, wake,
                                bool(b.get('keep_images')), b.get('username'))
        return _json({'ok': True})

    async def check_run(request):
        b = await _body(request)
        cid, vm = _cluster(request, b), _vm(request, b)
        settings = await asyncio.to_thread(store.get_settings, cid)
        result = await checker.check_vm(http, cid, vm, settings, username=str(b.get('username') or '?'), source='user')
        return _json(result)

    async def check_learn(request):
        b = await _body(request)
        cid, vm = _cluster(request, b), _vm(request, b)
        return _json(await checker.learn(http, cid, vm, str(b.get('username') or '?')))

    async def check_exclude(request):
        b = await _body(request)
        cid, vm = _cluster(request, b), _vm(request, b)
        await asyncio.to_thread(store.set_excluded, cid, vm, bool(b.get('excluded')), b.get('username'))
        return _json({'ok': True})

    async def check_image(request):
        cid, vm = _cluster(request), _vm(request)
        hit = await asyncio.to_thread(store.get_image, cid, vm)
        if not hit:
            return _err('no screenshot kept for this VM', 404)
        return web.Response(body=hit[0], content_type='image/png', headers={'Cache-Control': 'no-store'})

    async def check_refs(request):
        cid, vm = _cluster(request), _vm(request)
        rows = await asyncio.to_thread(store.list_refs, cid, vm)
        for row in rows:
            thumb = row.pop('thumb', None)
            row['thumb'] = ('data:image/png;base64,' + base64.b64encode(bytes(thumb)).decode()) if thumb else None
        return _json({'refs': rows})

    async def check_ref_delete(request):
        cid = _cluster(request)
        ok = await asyncio.to_thread(store.delete_ref, cid, int(request.match_info['ref_id']))
        return _json({'ok': ok}, 200 if ok else 404)

    async def vm_info(request):
        cid, vm = _cluster(request), _vm(request)
        return _json(await asyncio.to_thread(hc.vm_details, cid, vm))

    async def vm_search(request):
        cid = _cluster(request)
        return _json({'vms': await asyncio.to_thread(hc.vm_index, cid, request.query.get('fresh') == '1')})

    async def flags(request):
        cid = _cluster(request)
        return _json({'open': await asyncio.to_thread(store.open_flags, cid)})

    r.add_get('/health', health)
    r.add_post('/api/v1/tickets', ticket)
    r.add_get('/api/v1/relay', relay)
    r.add_get('/api/v1/sessions', sessions)
    r.add_get('/api/v1/live', live)
    r.add_post(r'/api/v1/sessions/{sid:\d+}/end', end_session)
    r.add_post(r'/api/v1/grants/{gid:\d+}/end', end_grant)
    r.add_post('/api/v1/check/verdict', verdict)
    r.add_post('/api/v1/screenshot', screenshot)
    r.add_post('/api/v1/bootcheck', bootcheck)
    r.add_get('/api/v1/check', check_get)
    r.add_put('/api/v1/check/settings', check_settings)
    r.add_post('/api/v1/check/run', check_run)
    r.add_post('/api/v1/check/learn', check_learn)
    r.add_post('/api/v1/check/exclude', check_exclude)
    r.add_get('/api/v1/check/image', check_image)
    r.add_get('/api/v1/check/refs', check_refs)
    r.add_delete(r'/api/v1/check/refs/{ref_id:\d+}', check_ref_delete)
    r.add_get('/api/v1/check/flags', flags)
    r.add_get('/api/v1/vm', vm_info)
    r.add_get('/api/v1/vm-index', vm_search)
    return app
