"""
api.py — REST API of the backup proxy (port 8083), driven by the backup service.

  GET  /health                liveness
  GET  /identity              hostname, MAC addresses, capabilities (token required)
  POST /jobs/<kind>           kind in backup | flatten | verify | sweep | prune | delete_chain | stage -> {id}
  GET  /jobs                  recent jobs
  GET  /jobs/<id>             state, progress, result or error
  DELETE /jobs/<id>           request cancellation

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


def create_app(cfg, engine) -> Flask:
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
