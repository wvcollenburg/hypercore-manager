"""
health.py — HTTP health check endpoint (G6).

Runs a minimal Flask app in a daemon thread.  Docker uses it as the
container liveness probe:

    GET /health  →  200 {"status": "ok", "service": "sentinel"}

A second endpoint exposes current operational state so operators can
see at a glance whether the main loops are running and when they last
completed successfully.
"""

import logging
import threading
from datetime import datetime, timezone

from flask import Flask, jsonify

log = logging.getLogger(__name__)

app = Flask(__name__)

# state written by the main loops; read by /health/detail
_state: dict = {
    'collection_last_ok':    None,   # datetime | None
    'trend_last_ok':         None,   # datetime | None
    'collection_errors':     0,
    'trend_errors':          0,
    'start_time':            datetime.now(timezone.utc).isoformat(),
    'trend_interval_seconds': 3600,  # updated at startup via set_trend_interval()
}


def set_trend_interval(seconds: int) -> None:
    _state['trend_interval_seconds'] = seconds


def record_collection_ok() -> None:
    _state['collection_last_ok'] = datetime.now(timezone.utc).isoformat()


def record_collection_error() -> None:
    _state['collection_errors'] += 1


def record_trend_ok() -> None:
    _state['trend_last_ok'] = datetime.now(timezone.utc).isoformat()


def record_trend_error() -> None:
    _state['trend_errors'] += 1


@app.get('/health')
def health():
    return jsonify({'status': 'ok', 'service': 'sentinel'}), 200


@app.get('/health/detail')
def health_detail():
    return jsonify({
        'status':                 'ok',
        'service':                'sentinel',
        'start_time':             _state['start_time'],
        'collection_last_ok':     _state['collection_last_ok'],
        'trend_last_ok':          _state['trend_last_ok'],
        'collection_errors':      _state['collection_errors'],
        'trend_errors':           _state['trend_errors'],
        'trend_interval_seconds': _state['trend_interval_seconds'],
    }), 200


def start(port: int = 8080) -> None:
    """Start the health server in a background daemon thread."""
    thread = threading.Thread(
        target=lambda: app.run(host='0.0.0.0', port=port, use_reloader=False),
        daemon=True,
        name='health-server',
    )
    thread.start()
    log.info('Health endpoint started', extra={'port': port})
