"""sync_echo.py — Echo server with /api/v1/sync route for H5 testing."""
from __future__ import annotations
import json, threading
from flask import Flask, request, jsonify

app = Flask(__name__)
received: list = []
lock = threading.Lock()

@app.route('/api/v1/sync', methods=['POST'])
def sync():
    payload = request.get_json(silent=True) or {}
    with lock:
        received.append(payload)
    print(f"[SYNC] received payload with {len(payload)} top-level keys")
    return jsonify({'ok': True}), 200

@app.route('/received')
def get_received():
    with lock:
        return jsonify(received)

if __name__ == '__main__':
    print("Sync echo server on http://0.0.0.0:9000/api/v1/sync")
    app.run(host='0.0.0.0', port=9000, debug=False, use_reloader=False)
