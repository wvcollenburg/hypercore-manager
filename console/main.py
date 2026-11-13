"""
main.py — entry point of the console service.

One asyncio process: the internal API and websocket relay the dashboard uses for VM consoles, and
the open console session check that grabs consoles on a schedule. It holds one HyperCore login
session per cluster (service 'console'); the session never leaves this process.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal

import aiohttp
from aiohttp import web

import api
import checker
import hc
import hcm_crypto
import log
import store

logger = logging.getLogger('console.main')


async def main() -> None:
    log.configure(level=os.getenv('CONSOLE_LOG_LEVEL', 'INFO'))
    if not hcm_crypto.key_configured():
        logger.error('HCM_SECRET_KEY is not set: cluster passwords cannot be decrypted and the API stays closed')
        token = ''
    else:
        token = hcm_crypto.derived_token('console')
    port = int(os.getenv('CONSOLE_API_PORT', '8084'))
    try:
        n = await asyncio.to_thread(store.close_dangling)
        if n:
            logger.info('closed %d console session(s) left open by the previous run', n)
    except Exception as e:  # noqa: BLE001 — migrations may not have run yet; the dashboard applies them
        logger.warning('could not close dangling sessions: %s', e)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    async with aiohttp.ClientSession() as http:
        runner = web.AppRunner(api.build(token, http), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, '0.0.0.0', port).start()
        logger.info('Console service listening', extra={'port': port})
        check = asyncio.create_task(checker.loop(http, stop))
        await stop.wait()
        logger.info('Shutting down')
        check.cancel()
        await runner.cleanup()
    await asyncio.to_thread(hc.close_all)


if __name__ == '__main__':
    asyncio.run(main())
