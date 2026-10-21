"""
jobs.py — the proxy's job engine: a small registry plus the four job kinds.

backup   write one point (full or delta) of a chain from hot-added devices onto the target
flatten  materialise one point as flat qcow2 files + the supplied definition (restore directory)
verify   re-hash a point against its manifest
sweep    remove stale restore directories under a folder on the target

Every job request carries the target (uri, username, password); the proxy mounts it for the
job and never stores the credentials. Progress and results are kept in memory and mirrored as
json under <data_dir>/jobs so a restarted proxy can still answer for finished jobs.
"""
from __future__ import annotations

import logging
import os
import shutil
import threading
import time
import uuid as uuidlib
from concurrent.futures import ThreadPoolExecutor

import copier
import devices
import manifest

log = logging.getLogger('proxy.jobs')
_DEVICE_TYPES_DISK = ('VIRTIO_DISK', 'IDE_DISK', 'SCSI_DISK')


def _now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


class Job:
    def __init__(self, kind: str, request: dict):
        self.id = uuidlib.uuid4().hex[:16]
        self.kind = kind
        self.request = request
        self.state = 'queued'            # queued | running | done | failed | cancelled
        self.progress: dict = {}
        self.result: dict | None = None
        self.error: str | None = None
        self.created_at = _now()
        self.updated_at = self.created_at
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def public(self) -> dict:
        return {'id': self.id, 'kind': self.kind, 'state': self.state, 'progress': self.progress,
                'result': self.result, 'error': self.error, 'created_at': self.created_at, 'updated_at': self.updated_at}


class Engine:
    def __init__(self, cfg, backend, mounter):
        self.cfg, self.backend, self.mounter = cfg, backend, mounter
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=max(1, cfg.concurrency), thread_name_prefix='job')
        os.makedirs(os.path.join(cfg.data_dir, 'jobs'), exist_ok=True)

    # ---------------------------------------------------------------- registry
    def submit(self, kind: str, request: dict) -> Job:
        runner = {'backup': self.run_backup, 'flatten': self.run_flatten, 'verify': self.run_verify, 'sweep': self.run_sweep}[kind]
        job = Job(kind, request)
        with self._lock:
            self.jobs[job.id] = job
        self._persist(job)
        self._pool.submit(self._guard, job, runner)
        return job

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def list(self) -> list[dict]:
        return [j.public() for j in reversed(list(self.jobs.values()))]   # newest first (insertion order)

    def _persist(self, job: Job) -> None:
        job.updated_at = _now()
        try:
            manifest.write_json(os.path.join(self.cfg.data_dir, 'jobs', f'{job.id}.json'), job.public())
        except OSError:
            pass

    def _guard(self, job: Job, runner) -> None:
        job.state = 'running'
        self._persist(job)
        try:
            job.result = runner(job)
            job.state = 'cancelled' if job.cancelled() else 'done'
        except Exception as e:                      # noqa: BLE001 — the job is the error boundary
            log.exception('job %s (%s) failed', job.id, job.kind)
            job.state = 'failed'
            job.error = str(e)
        finally:
            job.progress.pop('current_device', None)
            self._persist(job)

    # ---------------------------------------------------------------- helpers
    def _with_target(self, req: dict):
        t = req['target']
        root = self.mounter.acquire(t['uri'], t.get('username'), t.get('password'))
        return root

    def _release_target(self, req: dict) -> None:
        try:
            self.mounter.release(req['target']['uri'])
        except Exception:                           # noqa: BLE001
            log.warning('target release failed', exc_info=True)

    @staticmethod
    def _device_file(dev: dict) -> str:
        t = dev.get('type', 'VIRTIO_DISK')
        prefix = {'NVRAM': 'nvram-', 'VTPM': 'vtpm-'}.get(t, '')
        return f'{prefix}{dev["uuid"]}.qcow2'

    # ---------------------------------------------------------------- backup
    def run_backup(self, job: Job) -> dict:
        """
        request: {target:{uri,username,password}, folder, vm:{uuid,name}, point:{kind: full|delta, seq, previous_seq},
                  devices:[{uuid, type, clone_uuid, size, map:[{lower,upper,differentHow}]}], definition:{...}, plan_id, run_id}
        """
        req = job.request
        root = self._with_target(req)
        try:
            chain_dir = os.path.join(root, req['folder'], manifest.chain_dirname(req['vm']['name'], req['vm']['uuid']))
            kind, seq = req['point']['kind'], int(req['point']['seq'])
            prev_seq = req['point'].get('previous_seq')
            point_dir = os.path.join(chain_dir, manifest.point_dirname(kind, seq))
            if os.path.exists(os.path.join(point_dir, manifest.POINT_FILE)):
                raise RuntimeError(f'point {os.path.basename(point_dir)} already exists in {chain_dir}')
            os.makedirs(point_dir, exist_ok=True)
            chain = manifest.load_chain(chain_dir)
            prev_dir = None
            if kind == 'delta':
                if prev_seq is None:
                    raise ValueError('a delta needs point.previous_seq')
                prev = next((p for p in chain['points'] if p['seq'] == int(prev_seq)), None)
                if not prev:
                    raise RuntimeError(f'previous point {prev_seq} not in chain')
                prev_dir = os.path.join(chain_dir, prev['dir'])
            total_blocks = sum(copier.blocks_in(d['map']) for d in req['devices'])
            job.progress = {'devices_total': len(req['devices']), 'devices_done': 0, 'blocks_total': total_blocks, 'blocks_done': 0}
            out_devices = []
            t0 = time.time()
            for dev in req['devices']:
                job.progress['current_device'] = dev['uuid'][:8]
                src = devices.wait_for_device(dev['clone_uuid'], self.cfg.device_wait_seconds, self.cfg.dev_dir)
                size = devices.device_size(src)
                fname = self._device_file(dev)
                path = os.path.join(point_dir, fname)
                backing = os.path.join(prev_dir, fname) if prev_dir else None
                if backing and not os.path.exists(backing):
                    # device added since the previous point: it gets a full within this delta point
                    backing = None
                    dev_map = dev.get('full_map') or dev['map']
                else:
                    dev_map = dev['map']
                base_done = job.progress['blocks_done']

                def prog(done, total, _b=base_done):
                    job.progress['blocks_done'] = _b + done // copier.MiB

                with self.backend.prepare(path, size, backing) as writable:
                    written = copier.copy_intervals(src, writable, dev_map, writers=self.cfg.writers, progress=prog, cancelled=job.cancelled)
                if job.cancelled():
                    raise RuntimeError('cancelled')
                if not self.backend.check(path):
                    raise RuntimeError(f'image check failed: {fname}')
                out_devices.append({'uuid': dev['uuid'], 'type': dev.get('type', 'VIRTIO_DISK'), 'file': fname, 'size': size,
                                    'serial': dev.get('serial'), 'blocks': copier.blocks_in(dev_map), 'bytes_written': written,
                                    'full': backing is None and kind == 'delta', 'sha256': manifest.sha256_file(path)})
                job.progress['devices_done'] += 1
                self._persist(job)
            point = {'kind': kind, 'seq': seq, 'dir': os.path.basename(point_dir), 'previous_seq': prev_seq, 'created_at': _now(),
                     'duration_s': round(time.time() - t0, 1), 'devices': out_devices, 'definition': req.get('definition'),
                     'plan_id': req.get('plan_id'), 'run_id': req.get('run_id'), 'proxy_backend': self.backend.name}
            manifest.write_json(os.path.join(point_dir, manifest.POINT_FILE), point)
            chain.setdefault('vm', req['vm']); chain.setdefault('created_at', _now())
            chain['points'] = [p for p in chain['points'] if p['seq'] != seq] + [{k: point[k] for k in ('kind', 'seq', 'dir', 'previous_seq', 'created_at')}]
            chain['points'].sort(key=lambda p: p['seq'])
            manifest.save_chain(chain_dir, chain)
            return {'chain_dir': os.path.relpath(chain_dir, root), 'point_dir': os.path.basename(point_dir),
                    'devices': [{k: v for k, v in d.items() if k != 'serial'} for d in out_devices],
                    'bytes_written': sum(d['bytes_written'] for d in out_devices), 'duration_s': point['duration_s']}
        finally:
            self._release_target(req)

    # ---------------------------------------------------------------- flatten
    def run_flatten(self, job: Job) -> dict:
        """
        request: {target, chain_dir (relative to target root), seq, out_dir (relative), files:{device uuid -> file name},
                  definition_xml, definition_name}
        Produces <out_dir>/<file name> per device (flat qcow2) and the definition file.
        """
        req = job.request
        root = self._with_target(req)
        try:
            chain_dir = os.path.join(root, req['chain_dir'])
            chain = manifest.load_chain(chain_dir)
            point = next((p for p in chain['points'] if p['seq'] == int(req['seq'])), None)
            if not point:
                raise RuntimeError(f'point {req["seq"]} not found')
            pm = manifest.read_json(os.path.join(chain_dir, point['dir'], manifest.POINT_FILE)) or {}
            out_dir = os.path.join(root, req['out_dir'])
            os.makedirs(out_dir, exist_ok=True)
            job.progress = {'devices_total': len(pm.get('devices', [])), 'devices_done': 0}
            produced = []
            for d in pm.get('devices', []):
                src = os.path.join(chain_dir, point['dir'], d['file'])
                name = req.get('files', {}).get(d['uuid'], d['file'])
                dst = os.path.join(out_dir, name)
                self.backend.flatten(src, dst)
                produced.append({'uuid': d['uuid'], 'type': d['type'], 'file': name, 'size': os.path.getsize(dst)})
                job.progress['devices_done'] += 1
                self._persist(job)
            if req.get('definition_xml'):
                with open(os.path.join(out_dir, req.get('definition_name') or 'definition.xml'), 'w') as fh:
                    fh.write(req['definition_xml'])
            return {'out_dir': req['out_dir'], 'files': produced, 'definition': pm.get('definition')}
        finally:
            self._release_target(req)

    # ---------------------------------------------------------------- verify
    def run_verify(self, job: Job) -> dict:
        req = job.request
        root = self._with_target(req)
        try:
            chain_dir = os.path.join(root, req['chain_dir'])
            chain = manifest.load_chain(chain_dir)
            points = [p for p in chain['points'] if req.get('seq') is None or p['seq'] == int(req['seq'])]
            results = {p['dir']: manifest.verify_point(os.path.join(chain_dir, p['dir'])) for p in points}
            return {'ok': all(r['ok'] for r in results.values()), 'points': results}
        finally:
            self._release_target(req)

    # ---------------------------------------------------------------- sweep
    def run_sweep(self, job: Job) -> dict:
        """request: {target, folder, older_than_hours (default 24)} — removes <folder>/_restore/* older than the limit."""
        req = job.request
        root = self._with_target(req)
        try:
            base = os.path.join(root, req['folder'], '_restore')
            limit = time.time() - 3600 * float(req.get('older_than_hours', 24))
            removed = []
            if os.path.isdir(base):
                for name in os.listdir(base):
                    p = os.path.join(base, name)
                    if os.path.isdir(p) and os.path.getmtime(p) < limit:
                        shutil.rmtree(p, ignore_errors=True)
                        removed.append(name)
            return {'removed': removed}
        finally:
            self._release_target(req)
