"""
sync_push.py — Scheduled HTTPS sync push to SC//Foresight-Central.

Runs as a background thread in sentinel. Every SENTINEL_SYNC_INTERVAL_HOURS
(default 4), builds a delta snapshot (since last successful sync), signs it,
and POSTs it to the viewer's sync endpoint.

The push is skipped if:
- sharing_enabled is False in config.sharing_prefs
- viewer_url or bearer_token are not set
- A push is already in progress (no concurrent pushes)

On success: updates last_sync_at, last_sync_status='ok', clears last_sync_error
On failure: updates last_sync_status='error', last_sync_error=<message>, does NOT
            update last_sync_at (so next attempt retries the full delta)
"""
import json
import logging
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

log = logging.getLogger(__name__)


class SyncPushLoop:
    def __init__(self, cfg, interval_seconds: int = None):
        self._cfg = cfg
        self._interval = interval_seconds or (4 * 3600)
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(
            target=self._loop, name="sync-push", daemon=True
        )
        self._thread.start()
        log.info("sync push loop started", extra={"interval_seconds": self._interval})

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)

    def _loop(self):
        # stagger first push by 60s to let sentinel fully start
        self._stop_event.wait(60)
        while not self._stop_event.is_set():
            self._run_push()
            self._stop_event.wait(self._interval)

    def _run_push(self):
        if not self._lock.acquire(blocking=False):
            log.warning("sync push already in progress, skipping")
            return
        try:
            self._do_push()
        finally:
            self._lock.release()

    def _do_push(self):
        """
        Full push cycle:
        1. Read sharing_prefs from DB
        2. Skip if disabled / missing config
        3. Build delta snapshot (since last_sync_at)
        4. Anonymise if configured
        5. Sign
        6. POST to viewer_url/api/v1/sync
        7. Update last_sync_at / last_sync_status in DB
        """
        import db
        import exporter
        import anonymiser

        cfg = self._cfg

        try:
            with db.connect(cfg) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT sharing_enabled, viewer_url, bearer_token, "
                        "       last_sync_at, anon_vm_names, anon_node_uuids, "
                        "       anon_cluster_name, anon_hmac_key "
                        "FROM config.sharing_prefs WHERE cluster_id='default'"
                    )
                    row = cur.fetchone()
        except Exception as exc:
            log.error("sync push: failed to read sharing_prefs", extra={"error": str(exc)})
            return

        if not row:
            return
        sharing_enabled, viewer_url, bearer_token, last_sync_at, \
            anon_vm_names, anon_node_uuids, anon_cluster_name, anon_hmac_key = row

        if not sharing_enabled:
            return
        if not viewer_url or not bearer_token:
            log.warning("sync push: sharing enabled but viewer_url or bearer_token not set")
            return

        try:
            snapshot = exporter.build_snapshot(cfg, last_sync_at=last_sync_at)

            anon_cfg = anonymiser.AnonConfig(
                enabled=sharing_enabled,
                anon_vm_names=anon_vm_names or False,
                anon_node_uuids=anon_node_uuids or False,
                anon_cluster_name=anon_cluster_name or False,
                hmac_key=anon_hmac_key or "",
            )
            if anon_cfg.hmac_key:
                snapshot = anonymiser.anonymise_snapshot(snapshot, anon_cfg)

            signed = exporter.sign_snapshot(snapshot, bearer_token)
            payload = json.dumps(signed).encode("utf-8")
        except Exception as exc:
            log.error("sync push: snapshot build/sign failed", extra={"error": str(exc)})
            self._update_status(cfg, status="error", error=str(exc))
            return

        sync_url = viewer_url.rstrip("/") + "/api/v1/sync"
        req = urllib.request.Request(
            sync_url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {bearer_token}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                status_code = resp.status
                if status_code in (200, 201, 204):
                    log.info("sync push succeeded",
                             extra={"viewer_url": viewer_url, "status_code": status_code})
                    self._update_status(cfg, status="ok", sync_at=datetime.now(timezone.utc))
                else:
                    msg = f"Unexpected status {status_code}"
                    log.warning("sync push: unexpected status", extra={"status_code": status_code})
                    self._update_status(cfg, status="error", error=msg)
        except urllib.error.HTTPError as exc:
            msg = f"HTTP {exc.code}: {exc.reason}"
            log.error("sync push: HTTP error", extra={"error": msg, "url": sync_url})
            self._update_status(cfg, status="error", error=msg)
        except Exception as exc:
            log.error("sync push: transmission failed", extra={"error": str(exc)})
            self._update_status(cfg, status="error", error=str(exc))

    def _update_status(self, cfg, status: str, error: str = None,
                       sync_at: datetime = None):
        import db
        try:
            with db.connect(cfg) as conn:
                with conn.cursor() as cur:
                    if status == "ok":
                        cur.execute(
                            "UPDATE config.sharing_prefs "
                            "SET last_sync_at=%s, last_sync_status='ok', "
                            "    last_sync_error=NULL, updated_at=NOW() "
                            "WHERE cluster_id='default'",
                            (sync_at,)
                        )
                    else:
                        cur.execute(
                            "UPDATE config.sharing_prefs "
                            "SET last_sync_status='error', last_sync_error=%s, "
                            "    updated_at=NOW() "
                            "WHERE cluster_id='default'",
                            (error,)
                        )
                conn.commit()
        except Exception as exc:
            log.error("sync push: failed to update status", extra={"error": str(exc)})
