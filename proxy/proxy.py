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

import api
import config
import images
import jobs
import log
import targets


def main() -> None:
    log.configure(level=os.getenv('PROXY_LOG_LEVEL', 'INFO'))
    logger = logging.getLogger('proxy.main')
    cfg = config.load()
    os.makedirs(cfg.data_dir, exist_ok=True)
    if not cfg.api_token:
        logger.warning('PROXY_API_TOKEN is not set; REST API authentication is disabled')
    backend = images.make_backend(cfg.backend, cfg.nbd_slots, cfg.dev_dir)
    engine = jobs.Engine(cfg, backend, targets.Mounter(cfg.data_dir))
    app = api.create_app(cfg, engine)
    logger.info('Proxy starting', extra={'api_port': cfg.api_port, 'backend': backend.name, 'nbd_slots': cfg.nbd_slots,
                                         'data_dir': cfg.data_dir})
    api.start(app, cfg.api_port)

    stop = threading.Event()

    def _handle(signum, _frame):
        logger.info('Shutdown signal received', extra={'signal': signum})
        stop.set()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    stop.wait()
    logger.info('Proxy stopped')


if __name__ == '__main__':
    main()
