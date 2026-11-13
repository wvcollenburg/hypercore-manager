"""
guest_api.py — one-time console access for an outside engineer (plans/PLAN-roles.md, R4).

A guest is not a user. A grant gives one guest the console of one VM within a time window:
  - a link with a long random token (/guest/<token>) and, given out through another channel, a
    6-digit code; both are needed, only their hashes are stored, five wrong codes lock the grant;
  - redeeming sets a cookie of its own (hcm_guest, path /guest/), never the HCM session cookie, and
    binds the grant to that one browser;
  - the guest page is the console canvas and its key buttons, under the grantor's label: no VM name,
    no cluster, no menus; its assets come from /guest/ too, so a reverse proxy can publish /guest/
    and nothing else;
  - a guest only connects onto a console that shows a login or lock screen (the open-session
    classifier), unless the grantor allowed it explicitly;
  - granters see live guest sessions, can watch read-only, end them and revoke the grant. Everything
    is in the access audit and the console log (source 'guest' / 'watch').

Permissions: guest_console.request (ask) and guest_console.grant (give, approve, revoke, watch) on
the VM. Every /api/guest route checks the exact VM; the /guest/ routes accept only the guest cookie.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

from flask import Response, jsonify, make_response, redirect, render_template, request, send_from_directory, session
from itsdangerous import BadSignature, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash

import access_db
from access_db import AccessError

COOKIE = 'hcm_guest'
MAX_FAILED = 5
_VM = re.compile(r'^[0-9A-Za-z-]{1,64}$')


def _sha(v: str) -> str:
    return hashlib.sha256(v.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(v, what: str) -> datetime:
    try:
        t = datetime.fromisoformat(str(v).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        raise AccessError(f'{what} must be a date and time') from None
    if t.tzinfo is None:
        raise AccessError(f'{what} needs a time zone')
    return t


def _row(r) -> dict:
    out = {}
    for k, v in dict(r).items():
        if k in ('token_hash', 'code_hash', 'guest_nonce_hash'):
            continue
        out[k] = v.isoformat() if hasattr(v, 'isoformat') else (str(v) if k == 'cluster_id' else v)
    return out


def register(app, sock, requires, public, h):
    """h: helpers from app.py — access(), can_vm(perm, cid, vm), console_call(...), console_binding(create),
    client_ip(), relay_pipe(ws, ticket, binding), clusters(), vm_index_rows(cid)."""
    serializer = URLSafeTimedSerializer(app.secret_key, salt='hcm-guest-console')

    def actor() -> str:
        return session.get('username') or 'anonymous'

    def db():
        return access_db._connect()

    def audit(action: str, target: str, cid: str | None, detail: dict | None = None, who: str | None = None):
        node = h.access().tree.cluster_folder.get(cid) if cid and session.get('user_id') else None
        conn = db()
        try:
            with conn.cursor() as cur:
                if node is None and cid:
                    cur.execute('SELECT folder_id::text AS f FROM config.clusters WHERE cluster_id = %s', (cid,))
                    r = cur.fetchone()
                    node = r['f'] if r else None
                access_db.audit(cur, who or actor(), action, target, node_id=node, detail=detail)
            conn.commit()
        finally:
            conn.close()

    def grant(gid) -> dict | None:
        conn = db()
        try:
            with conn.cursor() as cur:
                cur.execute('SELECT * FROM console.guest_grants WHERE id = %s', (int(gid),))
                r = cur.fetchone()
                return dict(r, cluster_id=str(r['cluster_id'])) if r else None
        finally:
            conn.close()

    def update(gid: int, **fields) -> None:
        conn = db()
        try:
            with conn.cursor() as cur:
                cur.execute(f"UPDATE console.guest_grants SET {', '.join(f'{k} = %s' for k in fields)} WHERE id = %s",
                            [*fields.values(), gid])
            conn.commit()
        finally:
            conn.close()

    def label(g) -> str:
        return f"{g['guest_name']}{' (' + g['guest_company'] + ')' if g.get('guest_company') else ''} on {g.get('vm_name') or g['vm_uuid']}"

    def may_grant(g) -> bool:
        return h.can_vm('guest_console.grant', g['cluster_id'], g['vm_uuid'])

    def managed(gid) -> dict:
        try:
            g = grant(int(gid))
        except (TypeError, ValueError):
            g = None
        if not g or not may_grant(g):
            raise AccessError('not found', 404)
        return g

    def new_secrets() -> tuple[str, str]:
        return secrets.token_urlsafe(32), f'{secrets.randbelow(10**6):06d}'

    def https() -> bool:
        return request.is_secure or request.headers.get('X-Forwarded-Proto', '').split(',')[0].strip() == 'https'

    def link(token: str) -> str:
        """The guest's link, as the granter's browser reached HCM (behind a TLS proxy: https)."""
        base = (os.getenv('HCM_GUEST_BASE_URL') or '').strip().rstrip('/')
        if base:
            return f'{base}/guest/{token}'
        host = request.headers.get('X-Forwarded-Host') or request.host
        return f"{'https' if https() else request.scheme}://{host}/guest/{token}"

    def fields_from(b: dict) -> dict:
        cid = str(b.get('cluster_id') or '')
        vm = str(b.get('vm_uuid') or '')
        if not _VM.match(vm) or not re.fullmatch(r'[0-9a-fA-F-]{36}', cid):
            raise AccessError('cluster_id and vm_uuid required')
        name = str(b.get('guest_name') or '').strip()
        if not name or len(name) > 120:
            raise AccessError("the guest's name is required (at most 120 characters)")
        vf = _parse_time(b.get('valid_from') or _now().isoformat(), 'valid from')
        vu = _parse_time(b.get('valid_until'), 'valid until')
        if vu <= vf:
            raise AccessError('the window must end after it starts')
        if vu < _now():
            raise AccessError('the window is already over')
        if vu - vf > timedelta(days=14):
            raise AccessError('a guest window is at most 14 days')
        try:
            max_minutes = int(b.get('max_minutes') or 120)
            max_sessions = int(b.get('max_sessions') or 1)
        except (TypeError, ValueError):
            raise AccessError('session length and count must be numbers') from None
        if not 5 <= max_minutes <= 720 or not 1 <= max_sessions <= 20:
            raise AccessError('a session lasts 5-720 minutes; 1-20 sessions')
        vm_name = next((v.get('name') for v in h.vm_index_rows(cid) if v.get('uuid') == vm), None)
        if vm_name is None:
            raise AccessError('that VM is not on the cluster', 404)
        return {'cluster_id': cid, 'vm_uuid': vm, 'vm_name': vm_name,
                'label': (str(b.get('label') or '').strip() or 'Console')[:60],
                'guest_name': name, 'guest_company': (str(b.get('guest_company') or '').strip() or None),
                'reason': (str(b.get('reason') or '').strip()[:500] or None), 'valid_from': vf, 'valid_until': vu,
                'max_minutes': max_minutes, 'max_sessions': max_sessions,
                'allow_open_session': bool(b.get('allow_open_session'))}

    def insert(f: dict, status: str, token: str | None, code: str | None) -> int:
        conn = db()
        try:
            with conn.cursor() as cur:
                cols = list(f) + ['status', 'requested_by']
                vals = list(f.values()) + [status, actor()]
                if token:
                    cols += ['token_hash', 'code_hash', 'granted_by', 'granted_at']
                    vals += [_sha(token), generate_password_hash(code), actor(), _now()]
                cur.execute(f"INSERT INTO console.guest_grants ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING id", vals)
                gid = cur.fetchone()['id']
            conn.commit()
        finally:
            conn.close()
        return gid

    def live(gid=None) -> list:
        status, raw, _ = h.console_call('GET', 'live', query=f'grant={gid}' if gid else '', timeout=10)
        if status != 200:
            return []
        try:
            return json.loads(raw.decode() or '{}').get('live') or []
        except ValueError:
            return []

    # ------------------------------------------------------------------ granters and requesters
    @app.route('/api/guest/grants')
    @requires('guest_console.request', 'any')
    def guest_grants():
        acc = h.access()
        me = actor()
        conn = db()
        try:
            with conn.cursor() as cur:
                cur.execute("""SELECT g.*, c.cluster_name FROM console.guest_grants g JOIN config.clusters c ON c.cluster_id = g.cluster_id
                               WHERE g.created_at > NOW() - INTERVAL '60 days' OR g.status IN ('requested', 'active', 'locked')
                               ORDER BY (g.status = 'requested') DESC, g.created_at DESC LIMIT 500""")
                rows = cur.fetchall()
        finally:
            conn.close()
        sessions = {}
        for s in live():
            if s.get('grant_id'):
                sessions.setdefault(int(s['grant_id']), []).append(s)
        out = []
        for r in rows:
            g = dict(r, cluster_id=str(r['cluster_id']))
            can = h.can_vm('guest_console.grant', g['cluster_id'], g['vm_uuid'])
            if not can and g.get('requested_by') != me:
                continue
            d = _row(g)
            d['can_grant'] = can
            d['expired'] = g['valid_until'] < _now()
            d['live'] = [{'id': s['id'], 'source': s.get('source'), 'started': s.get('started')} for s in sessions.get(g['id'], [])]
            out.append(d)
        # who asked, gave or revoked: never an account above the reader (an MSP engineer stays unnamed)
        access_db.hide_names(out, ('requested_by', 'granted_by', 'revoked_by'), access_db.nameable_usernames(acc, me))
        return jsonify({'grants': out, 'pending': sum(1 for d in out if d['status'] == 'requested' and d['can_grant'])})

    @app.route('/api/guest/requests', methods=['POST'])
    @requires('guest_console.request', 'any')
    def guest_request():
        b = request.get_json(silent=True) or {}
        f = fields_from(b)
        if not h.can_vm('guest_console.request', f['cluster_id'], f['vm_uuid']):
            raise AccessError('not found', 404)
        f['allow_open_session'] = False                    # only a granter decides that
        gid = insert(f, 'requested', None, None)
        audit('guest.request', label(f), f['cluster_id'], {'window': [f['valid_from'].isoformat(), f['valid_until'].isoformat()],
                                                           'reason': f['reason']})
        return jsonify({'ok': True, 'id': gid}), 201

    @app.route('/api/guest/grants', methods=['POST'])
    @requires('guest_console.grant', 'any')
    def guest_grant_create():
        b = request.get_json(silent=True) or {}
        f = fields_from(b)
        if not h.can_vm('guest_console.grant', f['cluster_id'], f['vm_uuid']):
            raise AccessError('not found', 404)
        token, code = new_secrets()
        gid = insert(f, 'active', token, code)
        audit('guest.grant', label(f), f['cluster_id'], {'window': [f['valid_from'].isoformat(), f['valid_until'].isoformat()],
                                                         'max_minutes': f['max_minutes'], 'max_sessions': f['max_sessions'],
                                                         'allow_open_session': f['allow_open_session']})
        return jsonify({'ok': True, 'id': gid, 'link': link(token), 'code': code}), 201

    @app.route('/api/guest/grants/<gid>/approve', methods=['POST'])
    @requires('guest_console.grant', 'any')
    def guest_approve(gid):
        g = managed(gid)
        if g['status'] != 'requested':
            raise AccessError('only a request can be approved', 409)
        b = request.get_json(silent=True) or {}
        token, code = new_secrets()
        update(g['id'], status='active', token_hash=_sha(token), code_hash=generate_password_hash(code), granted_by=actor(),
               granted_at=_now(), allow_open_session=bool(b.get('allow_open_session')))
        audit('guest.approve', label(g), g['cluster_id'], {'requested_by': g['requested_by']})
        return jsonify({'ok': True, 'link': link(token), 'code': code})

    @app.route('/api/guest/grants/<gid>/reject', methods=['POST'])
    @requires('guest_console.grant', 'any')
    def guest_reject(gid):
        g = managed(gid)
        if g['status'] != 'requested':
            raise AccessError('only a request can be rejected', 409)
        update(g['id'], status='rejected', revoked_by=actor(), revoked_at=_now())
        audit('guest.reject', label(g), g['cluster_id'], {'requested_by': g['requested_by']})
        return jsonify({'ok': True})

    @app.route('/api/guest/grants/<gid>/revoke', methods=['POST'])
    @requires('guest_console.grant', 'any')
    def guest_revoke(gid):
        g = managed(gid)
        update(g['id'], status='revoked', revoked_by=actor(), revoked_at=_now(), guest_nonce_hash=None)
        h.console_call('POST', f"grants/{g['id']}/end", payload={'reason': 'guest access revoked'}, timeout=10)
        audit('guest.revoke', label(g), g['cluster_id'])
        return jsonify({'ok': True})

    @app.route('/api/guest/grants/<gid>/reissue', methods=['POST'])
    @requires('guest_console.grant', 'any')
    def guest_reissue(gid):
        """A new code (and link) for a locked or lost grant; the browser that redeemed it must redeem again."""
        g = managed(gid)
        if g['status'] not in ('active', 'locked'):
            raise AccessError('only an active or locked grant gets a new code', 409)
        token, code = new_secrets()
        update(g['id'], status='active', token_hash=_sha(token), code_hash=generate_password_hash(code), failed_codes=0,
               guest_nonce_hash=None)
        h.console_call('POST', f"grants/{g['id']}/end", payload={'reason': 'new code issued'}, timeout=10)
        audit('guest.reissue', label(g), g['cluster_id'])
        return jsonify({'ok': True, 'link': link(token), 'code': code})

    @app.route('/api/guest/grants/<gid>/end', methods=['POST'])
    @requires('guest_console.grant', 'any')
    def guest_end(gid):
        g = managed(gid)
        status, raw, _ = h.console_call('POST', f"grants/{g['id']}/end", payload={'reason': 'ended by the person who gave access'}, timeout=10)
        audit('guest.end_session', label(g), g['cluster_id'])
        return Response(raw, status=status, mimetype='application/json')

    @app.route('/api/guest/grants/<gid>/watch-ticket', methods=['POST'])
    @requires('guest_console.grant', 'any')
    def guest_watch_ticket(gid):
        g = managed(gid)
        status, raw, ctype = h.console_call('POST', 'tickets', payload={
            'cluster_id': g['cluster_id'], 'vm_uuid': g['vm_uuid'], 'username': actor(), 'source': 'watch',
            'view_only': True, 'binding': h.console_binding(True), 'client_ip': h.client_ip(), 'grant_id': g['id']}, timeout=30)
        return Response(raw, status=status, mimetype=ctype.split(';')[0])

    @app.route('/guest-watch/<gid>')
    @requires('guest_console.grant', 'any')
    def guest_watch_page(gid):
        g = managed(gid)
        return render_template('console.html', cluster_id=g['cluster_id'], vm_uuid=g['vm_uuid'], mode='watch',
                               title=g.get('vm_name') or g['vm_uuid'], subtitle=f"{g['guest_name']} · {g['label']}",
                               ticket_url=f"/api/guest/grants/{g['id']}/watch-ticket", ws_path='/console/ws', view_only=True,
                               asset_base='/static')

    # ------------------------------------------------------------------ the guest side
    def by_token(token: str) -> dict | None:
        if not token or len(token) > 100:
            return None
        conn = db()
        try:
            with conn.cursor() as cur:
                cur.execute('SELECT * FROM console.guest_grants WHERE token_hash = %s', (_sha(token),))
                r = cur.fetchone()
                return dict(r, cluster_id=str(r['cluster_id'])) if r else None
        finally:
            conn.close()

    def usable(g: dict | None) -> str | None:
        """Why this grant cannot be used right now (None: it can)."""
        if g is None or g['status'] in ('requested', 'revoked', 'rejected'):
            return 'invalid'
        if g['status'] == 'locked':
            return 'locked'
        now = _now()
        if now < g['valid_from']:
            return 'early'
        if now > g['valid_until']:
            return 'expired'
        return None

    def cookie_nonce(g: dict) -> str | None:
        raw = request.cookies.get(COOKIE)
        if not raw:
            return None
        try:
            data = serializer.loads(raw, max_age=15 * 86400)
        except BadSignature:
            return None
        if data.get('g') != g['id'] or not g.get('guest_nonce_hash') or _sha(data.get('n') or '') != g['guest_nonce_hash']:
            return None
        return data['n']

    def binding(g: dict, nonce: str) -> str:
        return _sha(f"guest:{g['id']}:{nonce}")

    def guest_page(status: int, **kw):
        resp = make_response(render_template('guest.html', **kw), status)
        return _harden(resp)

    def _harden(resp):
        resp.headers['Referrer-Policy'] = 'no-referrer'          # the token is in the URL
        resp.headers['X-Frame-Options'] = 'DENY'
        resp.headers['Cache-Control'] = 'no-store'
        resp.headers['X-Robots-Tag'] = 'noindex, nofollow'
        return resp

    @app.route('/guest/<token>', methods=['GET', 'POST'])
    @public('guest console: token + code, its own cookie, one VM')
    def guest_entry(token):
        g = by_token(token)
        why = usable(g)
        if why:
            return guest_page(404 if why == 'invalid' else 403, state=why,
                              starts=g['valid_from'].isoformat() if g and why == 'early' else None, label=(g or {}).get('label'))
        if request.method == 'POST':
            code = re.sub(r'\D', '', request.form.get('code', ''))
            if g['code_hash'] and len(code) == 6 and check_password_hash(g['code_hash'], code):
                nonce = secrets.token_urlsafe(24)
                update(g['id'], guest_nonce_hash=_sha(nonce), failed_codes=0, last_redeemed_at=_now(), last_redeemed_ip=h.client_ip())
                audit('guest.redeem', label(g), g['cluster_id'], {'ip': h.client_ip()}, who=f"guest:{g['guest_name']}")
                resp = redirect(f'/guest/{token}', code=303)
                resp.set_cookie(COOKIE, serializer.dumps({'g': g['id'], 'n': nonce}), path='/guest/', httponly=True,
                                samesite='Strict', secure=https(),
                                max_age=max(60, int((g['valid_until'] - _now()).total_seconds())))
                return _harden(resp)
            failed = (g['failed_codes'] or 0) + 1
            update(g['id'], failed_codes=failed, status='locked' if failed >= MAX_FAILED else g['status'])
            audit('guest.code_failed', label(g), g['cluster_id'], {'ip': h.client_ip(), 'attempt': failed},
                  who=f"guest:{g['guest_name']}")
            if failed >= MAX_FAILED:
                return guest_page(403, state='locked', label=g['label'])
            return guest_page(401, state='code', error=True, left=MAX_FAILED - failed, label=g['label'], token=token)
        if cookie_nonce(g) is None:
            return guest_page(200, state='code', label=g['label'], token=token)
        resp = make_response(render_template('console.html', cluster_id='', vm_uuid='', mode='guest', title=g['label'], subtitle='',
                                             ticket_url=f'/guest/{token}/ticket', ws_path='/guest/ws', view_only=False,
                                             asset_base='/guest'))
        return _harden(resp)

    @app.route('/guest/<token>/ticket', methods=['POST'])
    @public('guest console: the guest cookie bound to this grant')
    def guest_ticket(token):
        g = by_token(token)
        why = usable(g)
        if why:
            return jsonify({'error': 'this access is not valid now'}), 403
        nonce = cookie_nonce(g)
        if nonce is None:
            return jsonify({'error': 'enter the code first'}), 401
        if g['sessions_used'] >= g['max_sessions']:
            return jsonify({'error': 'every session of this access has been used'}), 403
        if not g['allow_open_session']:
            st, raw, _ = h.console_call('POST', 'check/verdict', payload={'cluster_id': g['cluster_id'], 'vm_uuid': g['vm_uuid']},
                                        timeout=60)
            verdict = {}
            try:
                verdict = json.loads(raw.decode() or '{}')
            except ValueError:
                pass
            if st != 200 or verdict.get('state') != 'locked':
                audit('guest.blocked_open_console', label(g), g['cluster_id'],
                      {'verdict': verdict.get('state'), 'detail': verdict.get('detail')}, who=f"guest:{g['guest_name']}")
                return jsonify({'error': 'The console is not ready for you yet: the owner has been notified. Try again in a few minutes.',
                                'not_ready': True}), 409
        minutes_left = (g['valid_until'] - _now()).total_seconds() / 60
        max_minutes = max(1, min(g['max_minutes'], int(minutes_left)))
        who = f"guest:{g['guest_name']}" + (f" ({g['guest_company']})" if g.get('guest_company') else '')
        status, raw, _ = h.console_call('POST', 'tickets', payload={
            'cluster_id': g['cluster_id'], 'vm_uuid': g['vm_uuid'], 'username': who, 'source': 'guest',
            'binding': binding(g, nonce), 'client_ip': h.client_ip(), 'max_minutes': max_minutes, 'grant_id': g['id']}, timeout=30)
        if status != 200:
            return jsonify({'error': 'the console cannot be opened right now'}), 502
        d = json.loads(raw.decode() or '{}')
        update(g['id'], sessions_used=g['sessions_used'] + 1)
        audit('guest.session', label(g), g['cluster_id'], {'ip': h.client_ip(), 'session': g['sessions_used'] + 1}, who=who)
        # the guest learns nothing about the VM: not its name, not the cluster
        return jsonify({'ticket': d['ticket'], 'max_minutes': max_minutes, 'idle_minutes': d.get('idle_minutes')})

    @sock.route('/guest/ws')
    @public('guest console websocket: the guest cookie and a ticket bound to it')
    def guest_ws(ws):
        ticket = request.args.get('ticket', '')
        raw = request.cookies.get(COOKIE)
        try:
            data = serializer.loads(raw or '', max_age=15 * 86400)
            g = grant(int(data['g']))
        except (BadSignature, KeyError, TypeError, ValueError):
            g = None
        if g is None or usable(g) or not g.get('guest_nonce_hash') or _sha(data.get('n') or '') != g['guest_nonce_hash'] or not ticket:
            ws.close(reason=4401, message='guest access required')
            return
        h.relay_pipe(ws, ticket, binding(g, data['n']))

    @app.route('/guest/vendor/novnc/<path:filename>')
    @public('static: the console viewer for the guest page')
    def guest_novnc(filename):
        return send_from_directory('static/vendor/novnc', filename)

    @app.route('/guest/lang/<filename>')
    @public('static: translations for the guest page')
    def guest_lang(filename):
        if not re.fullmatch(r'(en|nl|de|fr)\.js', filename):
            return Response('not found', status=404)
        return send_from_directory('static/lang', filename)
