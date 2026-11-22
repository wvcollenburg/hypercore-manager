"""
jobs.py — the proxy's job engine: a small registry plus the four job kinds.

backup   write one point (full or delta) of a chain from hot-added devices onto the target
flatten  materialise one point as flat qcow2 files + the supplied definition (restore directory)
synthesize  a point, read through its chain, as the base of a new chain (a synthetic full)
verify   re-hash the points of a chain (one, all, or everything up to a seq) against their manifests
netcheck ping / TCP checks of a test-restored VM on the test VLAN, through the proxy's test NIC
sweep    remove stale restore directories under a folder on the target
bench    measure write/read throughput onto a target through the paths a backup and a restore use

Every job request carries the target (uri, username, password); the proxy mounts it for the
job and never stores the credentials: a job drops its request when it ends. Progress and results
are kept in memory and mirrored as json under <data_dir>/jobs (for a look on the VM itself), both
for the newest KEEP_FINISHED finished jobs. A restarted proxy does not know earlier jobs.
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


def grown_tail(prev_size: int | None, size: int) -> dict | None:
    """The block interval a delta must copy because its image is larger than the previous point's
    (None when it is not): from the block holding the old end up to the new end, whole MiB blocks."""
    if not prev_size or size <= prev_size:
        return None
    return {'lower': prev_size // copier.MiB, 'upper': (size - 1) // copier.MiB}


_FINISHED = ('done', 'failed', 'cancelled')


class Engine:
    KEEP_FINISHED = 300                  # finished jobs kept in memory and as json; older ones are forgotten

    def __init__(self, cfg, backend, mounter):
        self.cfg, self.backend, self.mounter = cfg, backend, mounter
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=max(1, cfg.concurrency), thread_name_prefix='job')
        os.makedirs(os.path.join(cfg.data_dir, 'jobs'), exist_ok=True)
        self._trim_job_files()

    def _job_file(self, job_id: str) -> str:
        return os.path.join(self.cfg.data_dir, 'jobs', f'{job_id}.json')

    def _trim_job_files(self) -> None:
        """Earlier container lives left one json per job, never removed: keep the newest KEEP_FINISHED."""
        d = os.path.join(self.cfg.data_dir, 'jobs')
        try:
            files = sorted((e for e in os.scandir(d) if e.name.endswith('.json')), key=lambda e: e.stat().st_mtime)
        except OSError:
            return
        for e in files[:max(0, len(files) - self.KEEP_FINISHED)]:
            try:
                os.unlink(e.path)
            except OSError:
                pass

    def _forget_old(self, job: Job) -> None:
        """A job has ended: its request (with the target's password) goes, and finished jobs beyond the
        newest KEEP_FINISHED leave memory and disk."""
        job.request = {}
        with self._lock:
            finished = [j for j in self.jobs.values() if j.state in _FINISHED]
            drop = finished[:max(0, len(finished) - self.KEEP_FINISHED)]
            for j in drop:
                self.jobs.pop(j.id, None)
        for j in drop:
            try:
                os.unlink(self._job_file(j.id))
            except OSError:
                pass

    # ---------------------------------------------------------------- registry
    def submit(self, kind: str, request: dict) -> Job:
        runner = {'backup': self.run_backup, 'flatten': self.run_flatten, 'synthesize': self.run_synthesize, 'verify': self.run_verify, 'sweep': self.run_sweep,
                  'prune': self.run_prune, 'delete_chain': self.run_delete_chain, 'stage': self.run_stage, 'bench': self.run_bench, 'netcheck': self.run_netcheck}[kind]
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
            manifest.write_json(self._job_file(job.id), job.public())
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
            self._forget_old(job)

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
            prev_sizes = {}
            if prev_dir:
                pm = manifest.read_json(os.path.join(prev_dir, manifest.POINT_FILE)) or {}
                prev_sizes = {d.get('uuid'): int(d.get('size') or 0) for d in pm.get('devices', [])}
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
                if dev.get('image_size') and dev.get('type') in ('NVRAM', 'VTPM'):
                    size = min(size, int(dev['image_size']))      # NVRAM: the firmware wants its exact size
                # disks keep the clone's size: the guest's whole disk (an older backup service capped
                # them at the API capacity, which cut off the last partial MiB)
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
                    tail = grown_tail(prev_sizes.get(dev['uuid']), size)
                    if tail:
                        # the previous point's image was shorter than the disk (written capped): copy
                        # the end it never had, or it would read as zeros through every later point
                        dev_map = list(dev_map) + [tail]
                        job.progress['blocks_total'] += copier.blocks_in([tail])
                base_done = job.progress['blocks_done']

                def prog(done, total, _b=base_done):
                    job.progress['blocks_done'] = _b + done // copier.MiB

                job.progress['phase'] = 'copy'
                with self.backend.prepare(path, size, backing) as writable:
                    written = copier.copy_intervals(src, writable, dev_map, writers=self.cfg.writers, progress=prog, cancelled=job.cancelled)
                if job.cancelled():
                    raise RuntimeError('cancelled')
                # after the copy: the image is checked and read back once for its checksum. On a big full that
                # takes minutes; the phases are reported so the run does not look stuck at its last MiB
                job.progress['phase'] = 'check'
                problem = self.backend.check_detail(path)
                if problem:
                    raise RuntimeError(f'image check failed: {fname}: {problem}')
                job.progress.update(phase='checksum', checksum_done=0, checksum_total=os.path.getsize(path))

                def hashed(n):
                    job.progress['checksum_done'] = n
                digest = manifest.sha256_file(path, progress=hashed, cancelled=job.cancelled)
                out_devices.append({'uuid': dev['uuid'], 'type': dev.get('type', 'VIRTIO_DISK'), 'file': fname, 'size': size,
                                    'serial': dev.get('serial'), 'blocks': copier.blocks_in(dev_map), 'bytes_written': written,
                                    'full': backing is None and kind == 'delta', 'sha256': digest})
                job.progress.pop('phase', None)
                job.progress.pop('checksum_done', None)
                job.progress.pop('checksum_total', None)
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
            job.progress = {'phase': 'checksum'}
            healed = self._heal_legacy_prune(chain_dir, chain)
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
                    'bytes': done, 'duration_s': round(time.time() - t0, 1), 'healed': healed}
        finally:
            self._release_target(req)

    # ---------------------------------------------------------------- netcheck
    def run_netcheck(self, job: Job) -> dict:
        """request: {mac: the test NIC's MAC, probe: '172.16.0.250/25', targets: [{ip, ports: [int]}],
        dhcp?: [{mac, ip}], wait_s?}. Ping and TCP checks of a test-restored VM on the test VLAN, through the test
        NIC only, repeated until it answers or wait_s is up, serving its static DHCP leases meanwhile (netcheck.py)."""
        import netcheck
        req = job.request
        job.progress = {'targets': len(req.get('targets') or [])}
        try:
            return netcheck.run(req['mac'], req['probe'], req.get('targets') or [], dhcp=req.get('dhcp') or None,
                                wait_s=float(req.get('wait_s') or 0), cancelled=job.cancelled)
        except netcheck.NetcheckError as e:
            raise RuntimeError(str(e)) from None

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

    # ---------------------------------------------------------------- synthesize
    def run_synthesize(self, job: Job) -> dict:
        """
        request: {target, chain_dir, seq, new_chain_dir, plan_id?, run_id?}
        A synthetic full: point `seq` of a chain, read through its backing chain, becomes the base of
        a new chain (same files, devices, serials and definition, fresh checksums), without reading
        the VM again. The service verifies the source chain first; nothing is touched in it.
        """
        req = job.request
        root = self._with_target(req)
        try:
            rel = req['new_chain_dir']
            if rel.startswith('/') or '..' in rel.split('/'):
                raise ValueError('new_chain_dir must be a relative path inside the target')
            src_chain_dir = os.path.join(root, req['chain_dir'])
            chain = manifest.load_chain(src_chain_dir)
            point = next((p for p in chain['points'] if p['seq'] == int(req['seq'])), None)
            if not point:
                raise RuntimeError(f'point {req["seq"]} not found')
            pm = manifest.read_json(os.path.join(src_chain_dir, point['dir'], manifest.POINT_FILE)) or {}
            new_chain_dir = os.path.join(root, rel)
            base_dir = os.path.join(new_chain_dir, manifest.point_dirname('full', 0))
            if os.path.exists(os.path.join(base_dir, manifest.POINT_FILE)):
                raise RuntimeError(f'{rel} already has a base')
            os.makedirs(base_dir, exist_ok=True)
            job.progress = {'devices_total': len(pm.get('devices', [])), 'devices_done': 0}
            t0 = time.time()
            out = []
            for d in pm.get('devices', []):
                job.progress['current_device'] = d['uuid'][:8]
                dst = os.path.join(base_dir, d['file'])
                self.backend.synthesize(os.path.join(src_chain_dir, point['dir'], d['file']), dst)
                if job.cancelled():
                    raise RuntimeError('cancelled')
                problem = self.backend.check_detail(dst)
                if problem:
                    raise RuntimeError(f'image check failed: {d["file"]}: {problem}')
                out.append({'uuid': d['uuid'], 'type': d.get('type', 'VIRTIO_DISK'), 'file': d['file'], 'size': d.get('size'),
                            'serial': d.get('serial'), 'blocks': d.get('blocks'), 'bytes_written': os.path.getsize(dst),
                            'full': False, 'sha256': manifest.sha256_file(dst)})
                job.progress['devices_done'] += 1
                self._persist(job)
            base = {'kind': 'full', 'seq': 0, 'dir': os.path.basename(base_dir), 'previous_seq': None,
                    'created_at': pm.get('created_at') or point.get('created_at') or _now(),
                    'duration_s': round(time.time() - t0, 1), 'devices': out, 'definition': pm.get('definition'),
                    'plan_id': req.get('plan_id', pm.get('plan_id')), 'run_id': req.get('run_id', pm.get('run_id')),
                    'proxy_backend': self.backend.name, 'synthetic_from': {'chain_dir': req['chain_dir'], 'seq': point['seq']}}
            manifest.write_json(os.path.join(base_dir, manifest.POINT_FILE), base)
            manifest.save_chain(new_chain_dir, {'vm': chain.get('vm'), 'created_at': _now(),
                                                'points': [{k: base[k] for k in ('kind', 'seq', 'dir', 'previous_seq', 'created_at')}]})
            return {'chain_dir': rel, 'point_dir': base['dir'], 'devices': [{k: v for k, v in x.items() if k != 'serial'} for x in out],
                    'bytes_written': sum(x['bytes_written'] for x in out), 'duration_s': base['duration_s']}
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
            # a commit rewrites the base file and a rebase rewrites the next delta's header: the checksums in
            # their manifests are of files that no longer exist in that form. Remember what changed and hash
            # it again once the chain has its final shape (once per file, not once per dropped point).
            base_changed: set[str] = set()
            rebased: tuple[str, set[str]] | None = None
            # Crash safety: every step leaves chain.json and the manifests in a state a retry can continue
            # from. The base manifest is replaced first, chain.json gets the new shape next, nxt's directory
            # goes last; a retry that finds the base already holding nxt's manifest only finishes the
            # bookkeeping (before, it read the vanished nxt's manifest as {} and wrote that over the base).
            while points and points[0]['seq'] < keep_min:
                oldest, nxt = points[0], points[1] if len(points) > 1 else None
                base_dir = os.path.join(chain_dir, oldest['dir'])
                if nxt is None:
                    break
                nxt_dir = os.path.join(chain_dir, nxt['dir'])
                after = points[2] if len(points) > 2 else None
                rebased = None                                          # the point rebased last time is the one folded in now
                base_m = manifest.read_json(os.path.join(base_dir, manifest.POINT_FILE)) or {}
                if base_m.get('seq') is not None and int(base_m['seq']) == int(nxt['seq']):
                    # an earlier prune folded nxt in and stopped before chain.json said so
                    log.warning('prune of %s: %s was already folded into %s; finishing that step', chain_dir, nxt['dir'], oldest['dir'])
                    files = {d['file'] for d in base_m.get('devices', [])}
                    if self.backend.name != 'file':
                        base_changed |= files
                        if after:
                            rebased = (after['dir'], {f for f in files if os.path.exists(os.path.join(chain_dir, after['dir'], f))})
                else:
                    nxt_m = manifest.read_json(os.path.join(nxt_dir, manifest.POINT_FILE))
                    if not nxt_m:
                        raise RuntimeError(f'{nxt["dir"]} has no readable manifest; prune stopped, {oldest["dir"]} left as it is')
                    for d in nxt_m.get('devices', []):
                        delta_file = os.path.join(nxt_dir, d['file']); base_file = os.path.join(base_dir, d['file'])
                        if self.backend.name == 'file':
                            shutil.copyfile(delta_file, base_file)      # raw files: the delta is complete
                            base_changed.discard(d['file'])             # ... and so is its checksum
                        elif not os.path.exists(delta_file) and os.path.exists(base_file):
                            base_changed.add(d['file'])                 # moved there by an attempt that stopped halfway
                        elif os.path.exists(base_file) and not d.get('full'):
                            self.backend.commit(delta_file)             # fold the delta into its base
                            base_changed.add(d['file'])
                        else:
                            shutil.move(delta_file, base_file)          # device that got a full inside this delta
                            base_changed.discard(d['file'])             # the same file under another name
                        if after and self.backend.name != 'file':
                            after_file = os.path.join(chain_dir, after['dir'], d['file'])
                            if os.path.exists(after_file):
                                self.backend.rebase(after_file, base_file)
                                if rebased is None:
                                    rebased = (after['dir'], set())
                                rebased[1].add(d['file'])
                    # the base directory now holds nxt's state: move nxt's manifest in
                    nxt_m['dir'] = oldest['dir']
                    nxt_m['note'] = f"committed from {nxt['dir']} on {_now()}"
                    manifest.write_json(os.path.join(base_dir, manifest.POINT_FILE), nxt_m)
                nxt['dir'] = oldest['dir']
                points.pop(0)
                chain['points'] = list(points)
                manifest.save_chain(chain_dir, chain)
                shutil.rmtree(nxt_dir, ignore_errors=True)
                job.progress['dropped'] += 1
                self._persist(job)
            # the base is marked as hashed after its commit: a base that carries the 'committed from' note
            # without the mark was pruned by an older proxy and is healed once by the next verify
            stale = [(points[0]['dir'], base_changed, True)] if points and job.progress['dropped'] else []
            if rebased and any(p['dir'] == rebased[0] for p in points):
                stale.append((rebased[0], rebased[1], False))
            if any(files for _, files, _ in stale):
                job.progress['phase'] = 'checksum'
                self._persist(job)
            for point_dir, files, mark in stale:
                self._rehash(os.path.join(chain_dir, point_dir), files, mark=mark)
            chain['points'] = points
            manifest.save_chain(chain_dir, chain)
            return {'dirs': {str(p['seq']): p['dir'] for p in points}, 'dropped': job.progress['dropped']}
        finally:
            self._release_target(req)

    @staticmethod
    def _rehash(point_dir: str, files: set[str], mark: bool = False, healed: bool = False) -> None:
        """Write the current sha256 of `files` into the point's manifest (after a commit or rebase changed them).
        mark: the point is a base whose checksums were taken after its commit (LEGACY_COMMIT_NOTE without it
        means an older proxy pruned it); healed: stamp healed_at (see _heal_legacy_prune)."""
        path = os.path.join(point_dir, manifest.POINT_FILE)
        m = manifest.read_json(path)
        if not m:
            return
        for d in m.get('devices', []):
            if d.get('file') in files and os.path.exists(os.path.join(point_dir, d['file'])):
                d['sha256'] = manifest.sha256_file(os.path.join(point_dir, d['file']))
        if mark:
            m['checksums_after_commit'] = True
        if healed:
            m['healed_at'] = _now()
        manifest.write_json(path, m)

    LEGACY_COMMIT_NOTE = 'committed from '

    def _heal_legacy_prune(self, chain_dir: str, chain: dict) -> list[str]:
        """Proxies before 0.6.3 left stale checksums after a prune in two manifests: the base (its file was
        committed into) and the point after it (rebased). They would heal only on the next prune, which may
        never come, and until then every verification fails and a verified restore refuses the chain. Such a
        base carries the 'committed from' note without the checksums_after_commit mark that run_prune writes
        now: re-hash both points once, trusting their current file contents, and mark the base. Returns the
        point directories healed."""
        pts = sorted(chain.get('points') or [], key=lambda p: p['seq'])
        if not pts:
            return []
        base_dir = os.path.join(chain_dir, pts[0]['dir'])
        bm = manifest.read_json(os.path.join(base_dir, manifest.POINT_FILE)) or {}
        if not str(bm.get('note') or '').startswith(self.LEGACY_COMMIT_NOTE) or bm.get('checksums_after_commit'):
            return []
        healed = []
        if len(pts) > 1:                                    # the next point first: the base's mark is the last write
            nxt_dir = os.path.join(chain_dir, pts[1]['dir'])
            nm = manifest.read_json(os.path.join(nxt_dir, manifest.POINT_FILE)) or {}
            if nm:
                self._rehash(nxt_dir, {d['file'] for d in nm.get('devices', [])}, healed=True)
                healed.append(pts[1]['dir'])
        self._rehash(base_dir, {d['file'] for d in bm.get('devices', [])}, mark=True, healed=True)
        healed.insert(0, pts[0]['dir'])
        log.warning('checksums of %s re-taken once (pruned by a proxy before 0.6.3): %s', chain_dir, ', '.join(healed))
        return healed

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
