"""
webhook_echo.py — Minimal webhook receiver for F5 testing.
Run:  python tests/webhook_echo.py
Listens on 0.0.0.0:9000, logs every POST to /hook.
Sentinel must be configured with:
    SENTINEL_WEBHOOK_URL=http://host.docker.internal:9000/hook
"""
from __future__ import annotations

import json
import threading
from flask import Flask, request, jsonify

app = Flask(__name__)
received: list[dict] = []
lock = threading.Lock()


@app.route('/hook', methods=['POST'])
def hook():
    raw     = request.get_data()          # raw bytes — needed for HMAC verification
    payload = request.get_json(silent=True) or {}
    sig     = request.headers.get('X-Sentinel-Signature', '')
    with lock:
        received.append({
            'headers': dict(request.headers),
            'body':    payload,
            'raw':     raw.decode('utf-8', errors='replace'),
        })
    print(f"\n[WEBHOOK] sig={sig}")
    print(json.dumps(payload, indent=2))
    return jsonify({'ok': True}), 200


@app.route('/received', methods=['GET'])
def get_received():
    with lock:
        return jsonify(received)


@app.route('/clear', methods=['POST'])
def clear():
    with lock:
        received.clear()
    return jsonify({'cleared': True}), 200


if __name__ == '__main__':
    print("Webhook echo server listening on http://0.0.0.0:9000/hook")
    app.run(host='0.0.0.0', port=9000, debug=False)
