"""Background scheduler: one daily cron job per enabled export schedule, plus one-off jobs
for 'Run now', imports and post-restart recovery. Single process by design."""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

import db
import jobs

log = logging.getLogger('backup.scheduler')

_scheduler = BackgroundScheduler(job_defaults={
    'max_instances': 1,          # never run the same schedule twice at once
    'coalesce': True,            # collapse missed runs into one
    'misfire_grace_time': 3600,
})


def _job_id(schedule_id: int) -> str:
    return f'export-{schedule_id}'


def sync_jobs() -> int:
    """Reconcile APScheduler jobs with backup.plans (enabled schedules on enabled
    clusters). Called on start, after every change through the API, and periodically."""
    wanted = {}
    for s in db.get_schedules():
        if s['enabled'] and s.get('cluster_enabled', True):
            wanted[_job_id(s['id'])] = s
    for job in _scheduler.get_jobs():
        if job.id.startswith('export-') and job.id not in wanted:
            _scheduler.remove_job(job.id)
            log.info('Removed job %s', job.id)
    for job_id, s in wanted.items():
        hour, minute = s['run_time'].split(':')
        _scheduler.add_job(jobs.run_plan, trigger='cron', hour=int(hour), minute=int(minute),
                           id=job_id, args=[s['id']], replace_existing=True)
    return len(wanted)


def run_now(schedule_id: int) -> None:
    _scheduler.add_job(jobs.run_plan, args=[schedule_id], id=f'manual-{schedule_id}', replace_existing=True)


def run_restore(restore_id: int) -> None:
    import restore
    _scheduler.add_job(restore.run_restore, args=[restore_id], id=f'restore-{restore_id}', replace_existing=True)


def run_iso_delivery(restore_id: int) -> None:
    import flr
    _scheduler.add_job(flr.deliver_iso, args=[restore_id], id=f'iso-{restore_id}', replace_existing=True)


def run_import(import_id: int) -> None:
    _scheduler.add_job(jobs.run_import, args=[import_id], id=f'import-{import_id}', replace_existing=True)


def reconcile_interrupted() -> None:
    """Re-attach to runs/imports left RUNNING by a restart; rows with no task tag never got
    far enough to recover and are marked ERROR."""
    for r in db.get_active_runs():
        if r.get('mode') == 'incremental':
            # the proxy job may still be running, but its result is lost to us: fail the run, the
            # sweep removes clones and the next run re-anchors (a full if the anchor is gone)
            db.update_run(r['id'], status='ERROR', finished_at=db.now_utc(),
                          message='Interrupted by a restart; the next run continues the chain.')
            continue
        if r['task_tag']:
            _scheduler.add_job(jobs.resume_export, args=[r['id']], id=f"resume-run-{r['id']}", replace_existing=True)
            log.info('Resuming interrupted export run %s (task %s)', r['id'], r['task_tag'])
        else:
            db.update_run(r['id'], status='ERROR', finished_at=db.now_utc(),
                          message='Interrupted by a restart before the export task started.')
    for x in db.get_active_restores():
        db.update_restore(x['id'], status='ERROR', finished_at=db.now_utc(), message='Interrupted by a restart.')
    for i in db.get_active_imports():
        if i['task_tag']:
            _scheduler.add_job(jobs.resume_import, args=[i['id']], id=f"resume-import-{i['id']}", replace_existing=True)
            log.info('Resuming interrupted import %s (task %s)', i['id'], i['task_tag'])
        else:
            db.update_import(i['id'], status='ERROR', finished_at=db.now_utc(),
                             message='Interrupted by a restart before the import task started.')


def sweep_all() -> None:
    """Daily: stale anchor snapshots and clones left behind on every enabled cluster."""
    import cluster_registry
    import incremental
    for c in cluster_registry.list_enabled():
        try:
            out = incremental.sweep_cluster(c.cluster_id)
            if out['snapshots'] or out['clones']:
                log.info('sweep %s: %s', c.name, out)
        except Exception:        # noqa: BLE001
            log.exception('sweep failed for %s', c.name)


def verify_all() -> None:
    """Weekly: checksum verification of every active chain."""
    import cluster_registry
    import incremental
    for c in cluster_registry.list_enabled():
        try:
            out = incremental.verify_active_chains(c.cluster_id)
            if out['verified'] or out['failed']:
                log.info('verify %s: %s', c.name, out)
        except Exception:        # noqa: BLE001
            log.exception('verify failed for %s', c.name)


def start(sync_interval_seconds: int = 60) -> None:
    _scheduler.start()
    _scheduler.add_job(sync_jobs, trigger='interval', seconds=sync_interval_seconds, id='sync', replace_existing=True)
    _scheduler.add_job(sweep_all, trigger='cron', hour=4, minute=30, id='sweep', replace_existing=True)
    _scheduler.add_job(verify_all, trigger='cron', day_of_week='sun', hour=5, minute=0, id='verify', replace_existing=True)
    # first sweep a little after start: the dashboard applies new migrations at the same moment
    from datetime import datetime, timedelta
    _scheduler.add_job(sweep_all, trigger='date', run_date=datetime.now() + timedelta(seconds=120), id='sweep-start', replace_existing=True)
    n = sync_jobs()
    reconcile_interrupted()
    log.info('Scheduler started with %d active schedule(s)', n)


def next_run_time(schedule_id: int):
    job = _scheduler.get_job(_job_id(schedule_id))
    return job.next_run_time if job else None


def shutdown() -> None:
    _scheduler.shutdown(wait=False)
