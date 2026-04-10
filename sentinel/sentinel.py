"""
sentinel.py — SC//Foresight Sentinel entry point.

Architecture
------------
Two independent loops run on configurable intervals:

  Connectivity loop  (default 300 s)
      Pings InfluxDB with a cheap distinct-node count query to verify that
      the collector is writing and the database is reachable.  Records the
      result against the health endpoint.  No time-series data is retained.

  Analysis loop  (default 3600 s)
      Queries InfluxDB on-demand for the full analysis window, runs all
      analysis phases (forecasting, right-sizing, resize advisor, cluster
      health), and writes results to PostgreSQL.  All heavy data objects
      live only for the duration of _collect_and_analyse() — they are freed
      immediately on return and glibc arenas are trimmed back to the OS.
      This keeps sentinel's RSS proportional to one analysis pass rather
      than accumulating across the lifetime of the process.

  Outcome loop  (default 21600 s / 6 hours)  [Phase 3 — C7]
      Polls HyperCore for VM spec changes after a right-sizing recommendation
      has been emitted for long enough (default: 48 hours).  Writes outcome
      (resized_up / resized_down / no_action) back to the audit record.
      Disabled when SENTINEL_OUTCOME_TRACKER_ENABLED=false or SC_HOST is unset.

  Webhook loop  (default 60 s)  [Phase 3 — F5]
      Dispatches outbound HTTP POST alerts for new CRITICAL/EMERGENCY audit
      events.  Signs each request with HMAC-SHA256 using SENTINEL_WEBHOOK_SECRET.
      Retries failed deliveries on a back-off schedule; dead-letters after max
      attempts.  Disabled when SENTINEL_WEBHOOK_ENABLED=false or
      SENTINEL_WEBHOOK_URL is unset.

A health endpoint (G6) runs in a daemon thread throughout.
A REST API (F1-F4, F6) runs in a separate daemon thread on port 8081.
Structured JSON logging (G7) is configured before anything else starts.

Graceful shutdown on SIGTERM / SIGINT: all loops drain their current
iteration before exiting.
"""

import logging
import os
import signal
import threading

import api
import config
import health
import log
import sync_push
import tunables

logger = logging.getLogger('sentinel.main')


def _malloc_trim() -> None:
    """Return freed glibc arenas to the OS (no-op on non-Linux platforms)."""
    import ctypes
    import ctypes.util
    import sys
    if sys.platform != 'linux':
        return
    try:
        lib = ctypes.util.find_library('c')
        if lib:
            ctypes.CDLL(lib).malloc_trim(0)
    except Exception:
        pass


def _collect_and_analyse(reader, live_cfg, writer) -> dict:
    """
    Query InfluxDB on-demand then run all analysis phases.

    Intentionally isolated in its own function so that every heavy object
    (node/VM/storage time-series, forecast results, right-sizing results)
    goes out of scope and is freed by Python's reference counter the moment
    this function returns.  _malloc_trim() in the caller then returns the
    freed glibc arenas to the OS.

    Returns a dict of summary stats for the caller to log.
    """
    from collector.nodes   import collect_nodes
    from collector.vms     import collect_vms
    from collector.storage import collect_storage
    from analysis.cluster_health import run_cluster_health
    from analysis.forecaster     import run_forecasts
    from analysis.resize_advisor import run_resize_advisor
    from analysis.rightsizing    import run_rightsizing

    nodes   = collect_nodes(reader, live_cfg)
    vms     = collect_vms(reader, live_cfg)
    storage = collect_storage(reader, live_cfg)
    vm_mem  = reader.query_vm_mem_assigned()

    snapshot = {
        'nodes':           nodes,
        'vms':             vms,
        'storage':         storage,
        'vm_mem_assigned': vm_mem,
    }

    # Phase 1 + B3/B4/B9
    forecasts   = run_forecasts(snapshot, live_cfg)
    rightsizing = run_rightsizing(snapshot, live_cfg)
    writer.write_forecasts(forecasts)
    writer.write_rightsizing(rightsizing)

    # Phase 2: C3-C6 concrete resize advice
    resize_advice = run_resize_advisor(rightsizing, live_cfg)
    writer.write_resize_advice(resize_advice)

    # Phase 2: D1-D4, D6 cluster health
    cluster_health = run_cluster_health(
        forecasts, rightsizing, live_cfg,
        nodes=nodes,
        vm_mem_assigned=vm_mem,
    )
    writer.write_cluster_health(cluster_health)

    return {
        'forecasts':     len(forecasts),
        'rightsizing':   len(rightsizing),
        'resize_advice': len(resize_advice),
        'health_score':  cluster_health.health_score,
    }


def connectivity_loop(cfg: config.Config, stop: threading.Event, reader) -> None:
    """
    Ping InfluxDB on the collection interval to verify data flow.

    Uses a cheap distinct-node count query rather than fetching any time-series
    data — no memory is retained between pings.
    """
    while not stop.is_set():
        try:
            node_count = reader.ping()
            health.record_collection_ok()
            logger.info('Node collection complete', extra={'node_count': node_count})
        except Exception:
            health.record_collection_error()
            logger.exception('InfluxDB connectivity check failed')

        stop.wait(cfg.collection_interval_seconds)


def analysis_loop(cfg: config.Config, stop: threading.Event, reader) -> None:
    """
    On each trend interval:
      1. Query InfluxDB on-demand for the full analysis window.
      2. Run all analysis phases inside _collect_and_analyse().
      3. Write results to PostgreSQL.
      4. Free all heavy objects and trim glibc arenas back to the OS.

    Memory usage peaks during the analysis pass and returns to baseline
    immediately after — RSS does not grow with the number of collection cycles.
    """
    from writer import Writer
    writer = Writer(cfg)

    while not stop.is_set():
        # Wait for the trend interval before the first run so the connectivity
        # loop has had a chance to confirm InfluxDB is alive.
        stop.wait(cfg.trend_interval_seconds)
        if stop.is_set():
            break

        # Refresh live-tunable config before each cycle — no restart needed.
        live_cfg = tunables.apply_live(cfg)

        try:
            stats = _collect_and_analyse(reader, live_cfg, writer)
            health.record_trend_ok()
            logger.info('Analysis cycle complete', extra=stats)
        except Exception:
            health.record_trend_error()
            logger.exception('Analysis cycle failed')
        finally:
            # _collect_and_analyse() has returned; all its locals are freed.
            # Trim glibc arenas so the OS sees the RSS drop.
            _malloc_trim()


def outcome_loop(cfg: config.Config, stop: threading.Event) -> None:
    """C7: Periodically detect whether right-sized VMs were actually resized."""
    if not cfg.outcome_tracker_enabled:
        logger.info('Outcome tracker disabled via config')
        return

    from outcome_tracker import run_once as tracker_run_once

    while not stop.is_set():
        stop.wait(cfg.outcome_check_interval_seconds)
        if stop.is_set():
            break

        try:
            tracker_run_once(cfg)
        except Exception:
            logger.exception('Outcome tracker cycle failed')


def webhook_loop(cfg: config.Config, stop: threading.Event) -> None:
    """F5: Periodically dispatch pending webhook alerts for CRITICAL/EMERGENCY events."""
    if not cfg.webhook_enabled or not cfg.webhook_url:
        logger.info('Webhook dispatcher disabled \u2014 SENTINEL_WEBHOOK_URL not set')
        return

    from webhook import dispatch_pending

    while not stop.is_set():
        try:
            dispatch_pending(cfg)
        except Exception:
            logger.exception('Webhook dispatch cycle failed')

        stop.wait(cfg.webhook_dispatch_interval_seconds)


def main() -> None:
    cfg = config.load()

    log.configure(level=os.getenv('SENTINEL_LOG_LEVEL', 'INFO'))

    # T1: seed live tunables from env vars on first start (no-op if already seeded).
    tunables.seed_from_env()

    logger.info(
        'Sentinel starting',
        extra={
            'collection_interval_s':   cfg.collection_interval_seconds,
            'trend_interval_s':        cfg.trend_interval_seconds,
            'outcome_interval_s':      cfg.outcome_check_interval_seconds,
            'outcome_tracker_enabled': cfg.outcome_tracker_enabled,
            'webhook_enabled':         cfg.webhook_enabled and bool(cfg.webhook_url),
            'cluster_id':              cfg.cluster_id,
        },
    )

    if not cfg.hc_verify_ssl:
        logger.warning(
            'SC_VERIFY_SSL is false \u2014 TLS certificate validation disabled for HyperCore API. '
            'Set SC_VERIFY_SSL=true in production.'
        )
    if not cfg.api_token:
        logger.warning(
            'SENTINEL_API_TOKEN is not set \u2014 REST API authentication is disabled. '
            'Set a strong token in production.'
        )

    health.start(port=cfg.health_port)
    health.set_trend_interval(cfg.trend_interval_seconds)
    api.start(cfg)

    stop = threading.Event()

    def _handle_signal(signum, _frame):
        logger.info('Shutdown signal received', extra={'signal': signum})
        stop.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT,  _handle_signal)

    # Create one shared InfluxReader — the underlying HTTP connection is
    # kept alive and reused across both the connectivity and analysis loops.
    from influx_client import InfluxReader
    reader = InfluxReader(cfg)

    sync_loop = sync_push.SyncPushLoop(cfg)
    sync_loop.start()

    threads = [
        threading.Thread(target=connectivity_loop, args=(cfg, stop, reader), name='connectivity', daemon=True),
        threading.Thread(target=analysis_loop,     args=(cfg, stop, reader), name='analysis',     daemon=True),
        threading.Thread(target=outcome_loop,      args=(cfg, stop),         name='outcome',      daemon=True),
        threading.Thread(target=webhook_loop,      args=(cfg, stop),         name='webhook',      daemon=True),
    ]
    for t in threads:
        t.start()

    # Block main thread until shutdown signal.
    stop.wait()

    logger.info('Waiting for loops to finish current iteration...')
    sync_loop.stop()
    reader.close()
    for t in threads:
        t.join(timeout=60)

    logger.info('Sentinel stopped')


if __name__ == '__main__':
    main()
