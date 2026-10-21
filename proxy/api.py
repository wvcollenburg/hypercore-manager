"""
api.py — REST API of the backup proxy (port 8083), driven by the backup service.

  GET  /health                liveness
  GET  /identity              hostname, MAC addresses, capabilities (token required)
  POST /jobs/<kind>           kind in backup | flatten | verify | sweep | prune | delete_chain | stage -> {id}
  GET  /jobs                  recent jobs
  GET  /jobs/<id>             state, progress, result or error
  DELETE /jobs/<id>           request cancellation
  POST /mounts                mount a restore point read-only {target, chain_dir, seq} -> {id, filesystems}
  GET  /mounts/<id>/ls?path=  directory listing ('/' lists the filesystems)
  GET  /mounts/<id>/file?path= stream one file
  GET  /mounts/<id>/zip?paths=a,b  stream a zip of files/directories
  DELETE /mounts/<id>         unmount

Authentication: Bearer token (PROXY_API_TOKEN). An empty token disables auth, for development only.
"""
from __future__ import annotations

import logging
import threading
from functools import wraps

from flask import Flask, jsonify, request

import identity as identity_mod

log = logging.getLogger('proxy.api')

_REQUIRED = {
    'backup': ('target', 'folder', 'vm', 'point', 'devices'),
    'flatten': ('target', 'chain_dir', 'seq', 'out_dir'),
    'verify': ('target', 'chain_dir'),
    'sweep': ('target', 'folder'),
    'prune': ('target', 'chain_dir', 'keep_min_seq'),
    'delete_chain': ('target', 'chain_dir'),
    'stage': ('target', 'chain_dir', 'seq', 'devices'),
}


def create_app(cfg, engine, mounts=None) -> Flask:
    app = Flask('proxy.api')
    app.config['API_TOKEN'] = cfg.api_token

    def require_token(f):
        @wraps(f)
        def wrapper(*a, **kw):
            token = app.config.get('API_TOKEN', '')
            if token:
                auth = request.headers.get('Authorization', '')
                if not auth.startswith('Bearer ') or auth[7:] != token:
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
        return jsonify(identity_mod.identity(cfg, engine.backend.name, free))

    @app.post('/jobs/<kind>')
    @require_token
    def submit(kind):
        if kind not in _REQUIRED:
            return jsonify({'error': f'unknown job kind {kind!r}'}), 404
        body = request.get_json(silent=True) or {}
        missing = [k for k in _REQUIRED[kind] if k not in body]
        if missing:
            return jsonify({'error': f'missing fields: {", ".join(missing)}'}), 400
        if not isinstance(body.get('target'), dict) or 'uri' not in body['target']:
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
        try:
            m = mounts.mount(body['target'], body['chain_dir'], int(body['seq']))
        except Exception as e:                       # noqa: BLE001
            log.warning('mount failed', exc_info=True)
            return jsonify({'error': str(e)}), 502
        return jsonify(m.public()), 201

    @app.get('/mounts')
    @require_token
    def list_mounts():
        return jsonify([m.public() for m in (mounts.mounts.values() if mounts else [])])

    @app.get('/mounts/<mid>/ls')
    @require_token
    def mount_ls(mid):
        try:
            return jsonify(mounts.ls(mid, request.args.get('path') or '/'))
        except KeyError:
            return jsonify({'error': 'mount not found'}), 404
        except (FileNotFoundError, NotADirectoryError):
            return jsonify({'error': 'path not found'}), 404

    @app.get('/mounts/<mid>/file')
    @require_token
    def mount_file(mid):
        from flask import Response, stream_with_context
        try:
            fh, size, name = mounts.open_file(mid, request.args.get('path') or '')
        except KeyError:
            return jsonify({'error': 'mount not found'}), 404
        except (FileNotFoundError, IsADirectoryError):
            return jsonify({'error': 'file not found'}), 404

        def gen():
            with fh:
                while True:
                    b = fh.read(1 << 20)
                    if not b:
                        break
                    yield b
        return Response(stream_with_context(gen()), mimetype='application/octet-stream',
                        headers={'Content-Length': str(size), 'Content-Disposition': f'attachment; filename="{name}"'})

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
            return jsonify({'error': 'mount not found'}), 404
        name = request.args.get('name') or 'recovered-files.zip'
        return Response(stream_with_context(mounts.iter_zip(mid, paths)), mimetype='application/zip',
                        headers={'Content-Disposition': f'attachment; filename="{name}"'})

    @app.delete('/mounts/<mid>')
    @require_token
    def unmount_point(mid):
        if mounts is None or mid not in mounts.mounts:
            return jsonify({'error': 'mount not found'}), 404
        mounts.unmount(mid)
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


def start(app: Flask, port: int) -> None:
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=port, threaded=True, use_reloader=False),
                     name='proxy-api', daemon=True).start()
    log.info('Proxy REST API started', extra={'port': port})
