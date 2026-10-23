"""
jobs.py — the proxy's job engine: a small registry plus the four job kinds.

backup   write one point (full or delta) of a chain from hot-added devices onto the target
flatten  materialise one point as flat qcow2 files + the supplied definition (restore directory)
verify   re-hash the points of a chain (one, all, or everything up to a seq) against their manifests
sweep    remove stale restore directories under a folder on the target
bench    measure write/read throughput onto a target through the paths a backup and a restore use

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

import bench
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
        runner = {'backup': self.run_backup, 'flatten': self.run_flatten, 'verify': self.run_verify, 'sweep': self.run_sweep,
                  'prune': self.run_prune, 'delete_chain': self.run_delete_chain, 'stage': self.run_stage, 'bench': self.run_bench}[kind]
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
            job.progress.pop('current', None)
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
            # the service names the chain directory (unique per chain); older services did not
            rel = req.get('chain_dir') or os.path.join(req['folder'], manifest.chain_dirname(req['vm']['name'], req['vm']['uuid']))
            if rel.startswith('/') or '..' in rel.split('/'):
                raise ValueError('chain_dir must be a relative path inside the target')
            chain_dir = os.path.join(root, rel)
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
            # a second of self-check before the first clone is read: a reader whose qemu-nbd or qcow2
            # semantics are off fails here, not after 40 GiB
            self.backend.selfcheck(os.path.join(self.cfg.data_dir, f'selfcheck-{job.id}'))
            out_devices = []
            t0 = time.time()
            for dev in req['devices']:
                job.progress['current_device'] = dev['uuid'][:8]
                src = devices.wait_for_device(dev['clone_uuid'], self.cfg.device_wait_seconds, self.cfg.dev_dir)
                size = devices.device_size(src)
                if dev.get('image_size'):
                    size = min(size, int(dev['image_size']))      # NVRAM: the firmware wants its exact size
                fname = self._device_file(dev)
                path = os.path.join(point_dir, fname)
                backing = os.path.join(prev_dir, fname) if prev_dir else None
                if backing and (not os.path.exists(backing) or dev.get('full')):
                    # device added or resized since the previous point: it gets a full within this
                    # delta point, written without a backing file (snapDiff says nothing about a
                    # grown region, and a backing file of another size would read through)
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
            only = set(req.get('only_uuids') or [])
            for d in pm.get('devices', []):
                if only and d['uuid'] not in only:
                    continue
                src = os.path.join(chain_dir, point['dir'], d['file'])
                name = req.get('files', {}).get(d['uuid'], d['file'])
                dst = os.path.join(out_dir, name)
                self.backend.flatten(src, dst)
                want = (req.get('sizes') or {}).get(d['uuid'])
                if want and int(want) < d.get('size', 0):
                    self.backend.shrink(dst, int(want))        # older chains stored NVRAM at the 1 MiB clone size
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
        """
        request: {target, chain_dir, seq?: one point only, upto_seq?: every point with seq <= upto_seq
                  (the backing chain of a restore point)}
        Re-hashes every file of the selected points against their manifests. Progress carries
        points and bytes so a restore can show how far the check is.
        """
        req = job.request
        root = self._with_target(req)
        try:
            chain_dir = os.path.join(root, req['chain_dir'])
            chain = manifest.load_chain(chain_dir)
            seq, upto = req.get('seq'), req.get('upto_seq')
            points = [p for p in chain['points'] if (seq is None or p['seq'] == int(seq)) and (upto is None or p['seq'] <= int(upto))]
            job.progress = {'points_total': len(points), 'points_done': 0, 'bytes_done': 0,
                            'bytes_total': sum(manifest.point_bytes(os.path.join(chain_dir, p['dir'])) for p in points)}
            t0 = time.time()
            results, done = {}, 0
            for p in points:
                job.progress['current'] = p['dir']

                def prog(n, _b=done):
                    job.progress['bytes_done'] = _b + n

                r = manifest.verify_point(os.path.join(chain_dir, p['dir']), progress=prog, cancelled=job.cancelled)
                results[p['dir']] = r
                done += r.get('bytes', 0)
                job.progress['bytes_done'] = done
                job.progress['points_done'] += 1
                self._persist(job)
            return {'ok': bool(points) and all(r['ok'] for r in results.values()), 'points': results,
                    'bytes': done, 'duration_s': round(time.time() - t0, 1)}
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

    # ---------------------------------------------------------------- prune / delete
    def run_prune(self, job: Job) -> dict:
        """
        request: {target, chain_dir, keep_min_seq}
        Drops every point with seq < keep_min_seq by committing each delta forward into the base:
        base <- delta-0001 (commit), delta-0001 removed, delta-0002 rebased onto base, ... until the
        surviving oldest point lives in 'base'. Returns {dirs: {seq: dir}} for the remaining points.
        """
        req = job.request
        root = self._with_target(req)
        try:
            chain_dir = os.path.join(root, req['chain_dir'])
            chain = manifest.load_chain(chain_dir)
            points = sorted(chain['points'], key=lambda p: p['seq'])
            keep_min = int(req['keep_min_seq'])
            job.progress = {'dropped': 0, 'to_drop': len([p for p in points if p['seq'] < keep_min])}
            while points and points[0]['seq'] < keep_min:
                oldest, nxt = points[0], points[1] if len(points) > 1 else None
                base_dir = os.path.join(chain_dir, oldest['dir'])
                if nxt is None:
                    break
                nxt_dir = os.path.join(chain_dir, nxt['dir'])
                nxt_m = manifest.read_json(os.path.join(nxt_dir, manifest.POINT_FILE)) or {}
                for d in nxt_m.get('devices', []):
                    delta_file = os.path.join(nxt_dir, d['file']); base_file = os.path.join(base_dir, d['file'])
                    if self.backend.name == 'file':
                        shutil.copyfile(delta_file, base_file)          # raw files: the delta is complete
                    elif os.path.exists(base_file) and not d.get('full'):
                        self.backend.commit(delta_file)                 # fold the delta into its base
                    else:
                        shutil.move(delta_file, base_file)              # device that got a full inside this delta
                    after = points[2] if len(points) > 2 else None
                    if after and self.backend.name != 'file':
                        after_file = os.path.join(chain_dir, after['dir'], d['file'])
                        if os.path.exists(after_file):
                            self.backend.rebase(after_file, base_file)
                # the base directory now holds nxt's state: move nxt's manifest in, drop nxt's dir
                nxt_m['dir'] = oldest['dir']
                nxt_m['note'] = f"committed from {nxt['dir']} on {_now()}"
                manifest.write_json(os.path.join(base_dir, manifest.POINT_FILE), nxt_m)
                shutil.rmtree(nxt_dir, ignore_errors=True)
                nxt['dir'] = oldest['dir']
                points.pop(0)
                job.progress['dropped'] += 1
                self._persist(job)
            chain['points'] = points
            manifest.save_chain(chain_dir, chain)
            return {'dirs': {str(p['seq']): p['dir'] for p in points}, 'dropped': job.progress['dropped']}
        finally:
            self._release_target(req)

    def run_delete_chain(self, job: Job) -> dict:
        req = job.request
        root = self._with_target(req)
        try:
            chain_dir = os.path.join(root, req['chain_dir'])
            if os.path.isdir(chain_dir):
                shutil.rmtree(chain_dir, ignore_errors=True)
            return {'removed': not os.path.exists(chain_dir)}
        finally:
            self._release_target(req)

    # ---------------------------------------------------------------- stage (fast restore)
    def run_stage(self, job: Job) -> dict:
        """
        request: {target, chain_dir, seq, devices:[{uuid, staging_clone_uuid}]}
        Writes a point's disk images into writable staging disks hot-added to this proxy's VM
        (created by the backup service). Only allocated, non-zero blocks are written; a fresh
        HyperCore disk reads as zeros. The nbd backend does it with one qemu-img convert per
        disk (see images.NbdBackend.stage_into). The service then snapshots the proxy VM and
        clones the staging disks onto the restored VM.
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
            by_uuid = {d['uuid']: d for d in pm.get('devices', [])}
            job.progress = {'devices_total': len(req['devices']), 'devices_done': 0, 'blocks_total': 0, 'blocks_done': 0}
            out = []
            for want in req['devices']:
                d = by_uuid.get(want['uuid'])
                if not d:
                    raise RuntimeError(f'device {want["uuid"]} is not part of point {req["seq"]}')
                image = os.path.join(chain_dir, point['dir'], d['file'])
                dst = devices.wait_for_device(want['staging_clone_uuid'], self.cfg.device_wait_seconds, self.cfg.dev_dir)
                intervals = self.backend.allocation_map(image)
                job.progress['blocks_total'] += copier.blocks_in(intervals)
                base_done = job.progress['blocks_done']

                def prog(done, total, _b=base_done):
                    job.progress['blocks_done'] = _b + done // copier.MiB

                t = time.time()
                written = self.backend.stage_into(image, dst, intervals, self.cfg.writers, progress=prog, cancelled=job.cancelled)
                if job.cancelled():
                    raise RuntimeError('cancelled')
                out.append({'uuid': d['uuid'], 'file': d['file'], 'bytes_written': written, 'blocks': copier.blocks_in(intervals),
                            'duration_s': round(time.time() - t, 1)})
                job.progress['devices_done'] += 1
                self._persist(job)
            return {'devices': out, 'bytes_written': sum(x['bytes_written'] for x in out)}
        finally:
            self._release_target(req)

    # ---------------------------------------------------------------- bench
    def run_bench(self, job: Job) -> dict:
        """
        request: {target, size_mib (default 512, 16..8192), profile: write|restore, or modes: [...]}
        Writes size_mib per mode into <target>/_bench/<job id>/ and removes it again.
        """
        req = job.request
        root = self._with_target(req)
        try:
            size_mib = max(16, min(int(req.get('size_mib') or 512), 8192))
            wanted = req.get('modes') or bench.PROFILES.get(req.get('profile') or 'write') or bench.DEFAULT_MODES
            modes = [m for m in wanted if m in bench.ALL_MODES]
            if not modes:
                raise ValueError(f'no known modes in {req.get("modes")!r}; known: {bench.ALL_MODES}')
            last_mode = [None]

            def prog(p):
                job.progress = p
                if p.get('current') != last_mode[0]:
                    last_mode[0] = p.get('current')
                    self._persist(job)

            return bench.run(self.backend, root, self.cfg.data_dir, size_mib, modes, self.cfg.writers,
                             progress=prog, cancelled=job.cancelled, job_id=job.id)
        finally:
            self._release_target(req)
