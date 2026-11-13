"""
checker.py — the open console session check (backlog #2).

For every cluster with the check switched on, each running VM's console is grabbed once per
interval (default 30 minutes) and classified (classify.py). The grabs are spread over the interval:
each tick (one minute) takes only the share of VMs that is due, most overdue first, two at a time
per cluster, over the service's one HyperCore session. Grabs open the console in shared mode and
send no input unless the cluster's wake option says so (a pointer nudge or a Shift tap).

Results go to console.check_vm. A VM whose console shows neither a learnt locked screen nor a login
or lock screen is 'open' since the first check that saw it; the dashboard shows those as warnings on
the cluster card. Excluded VMs (kiosks, appliances) are never grabbed. Every grab is in the audit
log as user 'system'.
"""
from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timezone
from typing import Optional

import aiohttp

import classify
import consoles
import hc
import store

log = logging.getLogger('console.check')

TICK_SECONDS = 60
PER_CLUSTER = 2

_busy: set[tuple[str, str]] = set()


async def check_vm(http: aiohttp.ClientSession, cluster_id: str, vm_uuid: str, settings: dict, *,
                   username: str = 'system', source: str = 'system') -> dict:
    """Grab and classify one VM now; returns the stored verdict."""
    key = (cluster_id, vm_uuid)
    if key in _busy:
        return {'state': 'busy'}
    _busy.add(key)
    try:
        try:
            img, name = await consoles.screenshot(http, cluster_id, vm_uuid, username=username, source=source,
                                                  wake=settings.get('wake') or 'none')
        except hc.ConsoleError as e:
            state = 'stopped' if 'not running' in str(e) else 'error'
            await asyncio.to_thread(store.record_verdict, cluster_id, vm_uuid, None, state, str(e))
            return {'state': state, 'detail': str(e)}
        refs = await asyncio.to_thread(store.ref_hashes, cluster_id, vm_uuid)
        verdict = await asyncio.to_thread(classify.classify, img, refs, classify.ocr_available())
        image = await asyncio.to_thread(classify.png, img) if settings.get('keep_images') else None
        await asyncio.to_thread(store.record_verdict, cluster_id, vm_uuid, name, verdict.state, verdict.detail, image)
        if verdict.state == 'open':
            log.info('console session open', extra={'cluster_id': cluster_id, 'vm': name, 'vm_uuid': vm_uuid})
        return {'state': verdict.state, 'detail': verdict.detail}
    finally:
        _busy.discard(key)


async def learn(http: aiohttp.ClientSession, cluster_id: str, vm_uuid: str, username: str) -> dict:
    """'Learn locked state': grab the console now and store it as a reference of this VM's locked
    screen. The VM's verdict becomes 'locked' at once."""
    settings = await asyncio.to_thread(store.get_settings, cluster_id)
    img, name = await consoles.screenshot(http, cluster_id, vm_uuid, username=username, source='user',
                                          wake=settings.get('wake') or 'none')
    if classify.is_blank(img):
        raise hc.ConsoleError('the screen is blank: wake it (or set the wake option) before learning it', 409)
    h = classify.dhash(img)
    ref_id = await asyncio.to_thread(store.add_ref, cluster_id, vm_uuid, h, classify.thumbnail(img), username)
    image = classify.png(img) if settings.get('keep_images') else None
    await asyncio.to_thread(store.record_verdict, cluster_id, vm_uuid, name, 'locked',
                            'learnt as locked just now', image)
    return {'ref_id': ref_id, 'state': 'locked'}


def _due(vm: Optional[dict], interval_s: float, now: datetime) -> float:
    """Seconds overdue (higher = sooner); never-checked VMs come first."""
    if not vm or not vm.get('last_check_at'):
        return float('inf')
    return (now - vm['last_check_at']).total_seconds() - interval_s


async def run_cluster(http: aiohttp.ClientSession, settings: dict) -> None:
    cid = settings['cluster_id']
    try:
        vms = await asyncio.to_thread(hc.list_vms, cid)
    except hc.ConsoleError as e:
        log.warning('open-session check skipped: %s', e, extra={'cluster_id': cid})
        return
    running = {v['uuid']: v.get('name') or v['uuid'] for v in vms if str(v.get('state', '')).upper() == 'RUNNING'}
    stopped = {v['uuid']: v.get('name') or v['uuid'] for v in vms if v['uuid'] not in running}
    await asyncio.to_thread(store.sync_vms, cid, running, stopped)
    known = {r['vm_uuid']: r for r in await asyncio.to_thread(store.check_vms, cid)}
    interval_s = settings['interval_minutes'] * 60
    now = datetime.now(timezone.utc)
    due = sorted((u for u in running if not (known.get(u) or {}).get('excluded') and _due(known.get(u), interval_s, now) >= 0),
                 key=lambda u: _due(known.get(u), interval_s, now), reverse=True)
    # spread: per tick only the share of the cluster's VMs that one interval needs, plus a margin
    budget = max(1, math.ceil(len(running) * TICK_SECONDS / interval_s) + 1)
    sem = asyncio.Semaphore(PER_CLUSTER)

    async def one(u):
        async with sem:
            try:
                await check_vm(http, cid, u, settings)
            except Exception as e:  # noqa: BLE001 — one VM never stops the round
                log.warning('check of %s failed: %s', u, e, extra={'cluster_id': cid})

    await asyncio.gather(*(one(u) for u in due[:budget]))


async def loop(http: aiohttp.ClientSession, stop: asyncio.Event) -> None:
    last_prune = 0.0
    while not stop.is_set():
        loop_t = asyncio.get_running_loop().time()
        try:
            clusters = await asyncio.to_thread(store.enabled_clusters)
            await asyncio.gather(*(run_cluster(http, s) for s in clusters))
            if loop_t - last_prune > 86400:
                await asyncio.to_thread(store.prune_system_sessions)
                last_prune = loop_t
        except Exception as e:  # noqa: BLE001 — database down: try again next tick
            log.warning('open-session check round failed: %s', e)
        try:
            await asyncio.wait_for(stop.wait(), TICK_SECONDS)
        except asyncio.TimeoutError:
            pass
