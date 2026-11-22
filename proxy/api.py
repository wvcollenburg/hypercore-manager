"""
api.py — REST API of the backup proxy (port 8083), driven by the backup service.

  GET  /health                liveness
  GET  /identity              hostname, MAC addresses, capabilities (token required)
  POST /jobs/<kind>           kind in backup | flatten | synthesize | verify | sweep | prune | delete_chain | stage | bench | netcheck -> {id}
  GET  /jobs                  recent jobs
  GET  /jobs/<id>             state, progress, result or error
  DELETE /jobs/<id>           request cancellation
  POST /mounts                mount a restore point read-only {target, chain_dir, seq, keys?} -> {id, filesystems, locked}
                              (422 {error, locked} when only encrypted volumes without a fitting key were found)
  GET  /mounts/<id>/ls?path=  directory listing ('/' lists the filesystems)
  GET  /mounts/<id>/file?path= stream one file
  GET  /mounts/<id>/zip?paths=a,b  stream a zip of files/directories
  GET  /mounts/<id>/iso?paths=a,b[&size=1]  stream an ISO 9660 image of the selection (or just its size)
  DELETE /mounts/<id>         unmount
  GET  /support/bundle        zip of the proxy's log, jobs, mounts and system state (support.py)

Authentication: Bearer token (PROXY_API_TOKEN). An empty token disables auth, for development only.
"""
from __future__ import annotations

import hmac
import logging
import os
import socket
import ssl
import threading
import time
import unicodedata
from functools import wraps
from urllib.parse import quote

from flask import Flask, Response, jsonify, request
from werkzeug.serving import WSGIRequestHandler
from werkzeug.serving import make_server as _make_server

import identity as identity_mod
import mounts as mounts_mod
import support

log = logging.getLogger('proxy.api')

_REQUIRED = {
    'backup': ('target', 'folder', 'vm', 'point', 'devices'),
    'flatten': ('target', 'chain_dir', 'seq', 'out_dir'),
    'synthesize': ('target', 'chain_dir', 'seq', 'new_chain_dir'),
    'verify': ('target', 'chain_dir'),
    'sweep': ('target', 'folder'),
    'prune': ('target', 'chain_dir', 'keep_min_seq'),
    'delete_chain': ('target', 'chain_dir'),
    'stage': ('target', 'chain_dir', 'seq', 'devices'),
    'bench': ('target',),
    'netcheck': ('mac', 'probe', 'targets'),
}


def attachment(name: str) -> str:
    """Content-Disposition for a download whose name may be anything a guest can name a file: an HTTP
    header is Latin-1, and a name outside it ('Rapport – Q3.docx', 'prijs €.txt') killed the response.
    RFC 6266: an ASCII fallback for old clients plus filename* with the UTF-8 name percent-encoded."""
    fallback = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode('ascii')
    fallback = ''.join('_' if c in '"\\' or ord(c) < 32 or ord(c) == 127 else c for c in fallback).strip() or 'download'
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"


def create_app(cfg, engine, mounts=None, tls_fingerprint=None) -> Flask:
    app = Flask('proxy.api')
    app.config['API_TOKEN'] = cfg.api_token
    app.config['TLS_FINGERPRINT'] = tls_fingerprint

    def require_token(f):
        @wraps(f)
        def wrapper(*a, **kw):
            token = app.config.get('API_TOKEN', '')
            if token:
                auth = request.headers.get('Authorization', '')
                if not auth.startswith('Bearer ') or not hmac.compare_digest(auth[7:].encode(), token.encode()):
                    return jsonify({'error': 'Unauthorized'}), 401
            return f(*a, **kw)
        return wrapper

    @app.get('/health')
    def health():
        return jsonify({'status': 'ok', 'service': 'hcm-proxy', 'version': identity_mod.VERSION})

    @app.get('/identity')
    @require_token
    def ident():
        free = getattr(getattr(engine.backend, 'pool', None), 'free', None)
        ident = identity_mod.identity(cfg, engine.backend.name, free)
        ident['tls_fingerprint'] = app.config.get('TLS_FINGERPRINT')
        return jsonify(ident)

    @app.post('/update')
    @require_token
    def update_image():
        """HyperCore Manager pushes its proxy image (a docker-save tarball; X-Image-Tag names the tag
        inside). It lands in the update directory the VM watches: the host loads it and restarts
        this container on the new tag, so the answer here is 'accepted', not 'done'."""
        d = cfg.update_dir
        if not d or not os.path.isdir(d):
            return jsonify({'error': 'this proxy has no update hook (PROXY_UPDATE_DIR); update the container by hand'}), 501
        tag = (request.headers.get('X-Image-Tag') or '').strip()
        if not tag:
            return jsonify({'error': 'X-Image-Tag is required'}), 400
        if any(j.get('state') in ('queued', 'running') for j in engine.list()):
            return jsonify({'error': 'a job is running on this proxy; retry when it is idle'}), 409
        part = os.path.join(d, 'image.tar.part')
        n = 0
        with open(part, 'wb') as fh:
            while True:
                chunk = request.stream.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
                n += len(chunk)
        if not n:
            os.unlink(part)
            return jsonify({'error': 'a tarball body is required'}), 400
        os.replace(part, os.path.join(d, 'image.tar'))
        with open(os.path.join(d, 'image.tag'), 'w') as fh:
            fh.write(tag + '\n')
        # the host's path unit restarts this container the moment the marker appears, so the marker
        # is armed after the answer has left; the caller polls for the new version anyway
        def _arm():
            time.sleep(1.5)
            open(os.path.join(d, 'ready'), 'w').close()
        threading.Thread(target=_arm, name='update-arm', daemon=True).start()
        log.info('image update accepted', extra={'tag': tag, 'bytes': n})
        return jsonify({'accepted': True, 'tag': tag, 'bytes': n}), 202

    @app.post('/jobs/<kind>')
    @require_token
    def submit(kind):
        if kind not in _REQUIRED:
            return jsonify({'error': f'unknown job kind {kind!r}'}), 404
        body = request.get_json(silent=True) or {}
        missing = [k for k in _REQUIRED[kind] if k not in body]
        if missing:
            return jsonify({'error': f'missing fields: {", ".join(missing)}'}), 400
        if 'target' in _REQUIRED[kind] and (not isinstance(body.get('target'), dict) or 'uri' not in body['target']):
            return jsonify({'error': 'target.uri is required'}), 400
        job = engine.submit(kind, body)
        log.info('job submitted', extra={'job': job.id, 'kind': kind})
        return jsonify({'id': job.id, 'state': job.state}), 202

    @app.get('/jobs')
    @require_token
    def list_jobs():
        return jsonify(engine.list()[:100])

    @app.get('/jobs/<job_id>')
    @require_token
    def get_job(job_id):
        job = engine.get(job_id)
        if not job:
            return jsonify({'error': 'not found'}), 404
        return jsonify(job.public())

    # ------------------------------------------------------------------ file-level recovery
    @app.post('/mounts')
    @require_token
    def mount_point():
        if mounts is None:
            return jsonify({'error': 'file-level recovery is not enabled on this proxy'}), 501
        body = request.get_json(silent=True) or {}
        for k in ('target', 'chain_dir', 'seq'):
            if k not in body:
                return jsonify({'error': f'missing field {k}'}), 400
        keys = body.get('keys') or []
        if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
            return jsonify({'error': 'keys must be a list of strings'}), 400
        try:
            m = mounts.mount(body['target'], body['chain_dir'], int(body['seq']), keys=keys)
        except mounts_mod.LockedVolumes as e:
            return jsonify({'error': str(e), 'locked': e.locked}), 422
        except Exception as e:                       # noqa: BLE001
            log.warning('mount failed', exc_info=True)
            return jsonify({'error': str(e)}), 502
        return jsonify(m.public()), 201

    @app.get('/support/bundle')
    @require_token
    def support_bundle():
        free = getattr(getattr(engine.backend, 'pool', None), 'free', None)
        data = support.bundle(cfg, identity_mod.identity(cfg, engine.backend.name, free), engine.list()[:200],
                              [m.public() for m in (mounts.mounts.values() if mounts else [])])
        name = f'hcm-proxy-{socket.gethostname()}-{time.strftime("%Y%m%d-%H%M%S")}.zip'
        return Response(data, mimetype='application/zip', headers={'Content-Disposition': f'attachment; filename="{name}"'})

    @app.get('/mounts')
    @require_token
    def list_mounts():
        return jsonify([m.public() for m in (mounts.mounts.values() if mounts else [])])

    def mount_gone(mid):
        # the backup service shows this text; 'not found' stays in it (it maps that to a 404 of its own)
        idle = getattr(mounts, 'idle_seconds', 1800) // 60
        return jsonify({'error': f'mount {mid} not found: it was unmounted (on request, after {idle} minutes '
                                 'without use, or by a restart of the proxy); open the restore point again'}), 404

    def path_gone(what, path):
        return jsonify({'error': f'{what} not found in this restore point: {path or "/"}'}), 404

    @app.get('/mounts/<mid>/ls')
    @require_token
    def mount_ls(mid):
        try:
            return jsonify(mounts.ls(mid, request.args.get('path') or '/'))
        except KeyError:
            return mount_gone(mid)
        except (FileNotFoundError, NotADirectoryError):
            return path_gone('directory', request.args.get('path'))

    @app.get('/mounts/<mid>/file')
    @require_token
    def mount_file(mid):
        from flask import Response, stream_with_context
        try:
            fh, size, name = mounts.open_file(mid, request.args.get('path') or '')
        except KeyError:
            return mount_gone(mid)
        except (FileNotFoundError, IsADirectoryError):
            return path_gone('file', request.args.get('path'))

        def gen():
            with fh, mounts.streaming(mid):
                while True:
                    b = fh.read(1 << 20)
                    if not b:
                        break
                    yield b
        return Response(stream_with_context(gen()), mimetype='application/octet-stream',
                        headers={'Content-Length': str(size), 'Content-Disposition': attachment(name)})

    @app.get('/mounts/<mid>/zip')
    @require_token
    def mount_zip(mid):
        from flask import Response, stream_with_context
        paths = [p for p in (request.args.get('paths') or '').split(',') if p]
        if not paths:
            return jsonify({'error': 'paths is required'}), 400
        try:
            mounts.get(mid)
        except KeyError:
            return mount_gone(mid)
        name = request.args.get('name') or 'recovered-files.zip'
        return Response(stream_with_context(mounts.iter_zip(mid, paths)), mimetype='application/zip',
                        headers={'Content-Disposition': attachment(name)})

    @app.get('/mounts/<mid>/iso')
    @require_token
    def mount_iso(mid):
        from flask import Response, stream_with_context
        paths = [p for p in (request.args.get('paths') or '').split(',') if p]
        if not paths:
            return jsonify({'error': 'paths is required'}), 400
        try:
            size = mounts.iso_size(mid, paths)
        except KeyError:
            return mount_gone(mid)
        except (FileNotFoundError, RuntimeError) as e:
            return jsonify({'error': str(e)}), 400
        if request.args.get('size'):
            return jsonify({'size': size})
        return Response(stream_with_context(mounts.iter_iso(mid, paths)), mimetype='application/octet-stream',
                        headers={'Content-Length': str(size), 'Content-Disposition': 'attachment; filename="recovery.iso"'})

    @app.delete('/mounts/<mid>')
    @require_token
    def unmount_point(mid):
        if mounts is None or mid not in mounts.mounts:
            return mount_gone(mid)
        try:
            mounts.unmount(mid)
        except mounts_mod.MountBusy as e:
            return jsonify({'error': str(e)}), 409
        return jsonify({'ok': True})

    @app.delete('/jobs/<job_id>')
    @require_token
    def cancel_job(job_id):
        job = engine.get(job_id)
        if not job:
            return jsonify({'error': 'not found'}), 404
        job.cancel()
        return jsonify({'id': job.id, 'state': job.state, 'cancel_requested': True})

    return app


class _Handler(WSGIRequestHandler):
    """Werkzeug's request handler with the TLS handshake moved into the request thread. Werkzeug wraps
    the listening socket with the default handshake-on-accept, so the handshake ran in the single accept
    loop: one connection that never spoke TLS froze the whole API (no token needed). Every socket
    operation is bounded by `timeout`, the handshake included; a slow reader between chunks is not."""
    timeout = 300

    def handle(self) -> None:
        if isinstance(self.connection, ssl.SSLSocket):
            try:
                self.connection.do_handshake()
            except (OSError, ValueError) as e:              # timeout, reset, not TLS at all
                log.debug('TLS handshake from %s failed: %s', self.client_address[0], e)
                return
        super().handle()


def make_server(app: Flask, port: int, ssl_context=None, host: str = '0.0.0.0'):
    """A threaded werkzeug server; with TLS (a (cert, key) tuple) the listening socket is wrapped
    without the handshake on accept, which _Handler does in the request's own thread."""
    srv = _make_server(host, port, app, threaded=True, request_handler=_Handler)
    if ssl_context:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(*ssl_context)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True, do_handshake_on_connect=False)
        srv.ssl_context = ctx                               # werkzeug reports https and treats TLS errors as dropped
    return srv


def start(app: Flask, port: int, ssl_context=None, host: str = '0.0.0.0'):
    srv = make_server(app, port, ssl_context, host)
    threading.Thread(target=srv.serve_forever, name='proxy-api', daemon=True).start()
    log.info('Proxy REST API started', extra={'port': srv.port, 'tls': bool(ssl_context)})
    return srv
