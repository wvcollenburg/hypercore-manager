"""
exports.py — entry point of the Exports service.

One process: APScheduler (daily exports, run-now, imports, post-restart recovery) plus
the REST API the dashboard proxies. Clusters and credentials come from the registry.
"""
from __future__ import annotations

import logging
import os
import signal
import threading

import api
import config
import hcm_crypto
import log
import scheduler

logger = logging.getLogger('exports.main')


def main() -> None:
    log.configure(level=os.getenv('EXPORTS_LOG_LEVEL', 'INFO'))
    cfg = config.load()
    if not hcm_crypto.key_configured():
        logger.error('HCM_SECRET_KEY is not set: stored share and cluster passwords cannot be decrypted')
    if not cfg.api_token:
        logger.warning('EXPORTS_API_TOKEN is not set; REST API authentication is disabled')
    logger.info('Exports starting', extra={'api_port': cfg.api_port, 'task_timeout_hours': cfg.task_timeout_hours,
                                           'poll_interval_s': cfg.poll_interval_seconds})
    api.start(cfg)
    scheduler.start(cfg.sync_interval_seconds)

    stop = threading.Event()

    def _handle(signum, _frame):
        logger.info('Shutdown signal received', extra={'signal': signum})
        stop.set()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    stop.wait()
    scheduler.shutdown()
    logger.info('Exports stopped')


if __name__ == '__main__':
    main()
