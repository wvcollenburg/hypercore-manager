"""
proxy.py — entry point of the backup proxy container.

Runs on the HyperCore Manager VM (self-proxy) or in a small VM per remote cluster. It reads
hot-added snapshot clones, writes qcow2 chains onto backup targets, flattens points for restores
and verifies chains. It never talks to HyperCore and never keeps cluster credentials.
"""
from __future__ import annotations

import logging
import os
import signal
import threading
import time

import api
import config
import images
import jobs
import log
import mounts as mounts_mod
import targets
import tls


def main() -> None:
    log.configure(level=os.getenv('PROXY_LOG_LEVEL', 'INFO'))
    logger = logging.getLogger('proxy.main')
    cfg = config.load()
    os.makedirs(cfg.data_dir, exist_ok=True)
    if not cfg.api_token:
        logger.warning('PROXY_API_TOKEN is not set; REST API authentication is disabled')
    backend = images.make_backend(cfg.backend, cfg.nbd_slots, cfg.dev_dir)
    mounter = targets.Mounter(cfg.data_dir)
    engine = jobs.Engine(cfg, backend, mounter)
    mounts = mounts_mod.MountManager(cfg, backend, mounter)
    ssl_ctx, fp = tls.context_for(cfg)
    app = api.create_app(cfg, engine, mounts, tls_fingerprint=fp)

    def _idle_sweeper():
        while True:
            time.sleep(60)
            try:
                mounts.sweep_idle()
            except Exception:                       # noqa: BLE001
                logging.getLogger('proxy.mounts').exception('idle sweep failed')
    threading.Thread(target=_idle_sweeper, name='flr-idle', daemon=True).start()
    logger.info('Proxy starting', extra={'api_port': cfg.api_port, 'backend': backend.name, 'nbd_slots': cfg.nbd_slots,
                                         'data_dir': cfg.data_dir, 'tls': cfg.tls, 'tls_fingerprint': fp})
    api.start(app, cfg.api_port, ssl_ctx)

    stop = threading.Event()

    def _handle(signum, _frame):
        logger.info('Shutdown signal received', extra={'signal': signum})
        stop.set()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    stop.wait()
    mounts.unmount_all()
    logger.info('Proxy stopped')


if __name__ == '__main__':
    main()
