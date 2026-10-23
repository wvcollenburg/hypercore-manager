"""
deploy.py — create a backup proxy VM on a cluster from an Ubuntu cloud image with cloud-init.

What the panel does for an administrator:
  1. make sure the cluster holds the cloud image as an uploaded VirtualDisk (uploaded once from a
     URL the backup container can reach, or already present under a known name)
  2. create a UEFI VM with cloud-init user-data that installs Docker and the guest agent, sets the
     nbd and LVM host settings, and starts the proxy container with PROXY_TLS=auto and a token
  3. attach the image as the VM's disk (grown to the requested size), set the boot order, start
  4. wait for the VM's IP address (static from the form, or reported by the guest agent)
  5. register the proxy (https://<ip>:8083) and probe it until the container answers
  6. remove the cloud-init ISO from the VM (it carries the token; the daily sweep retries)

Everything here is plain API calls; the cloud-init document is rendered by cloud_init_userdata()
so it can be reviewed and tested without a cluster.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid as uuidlib
from datetime import datetime, timezone

import db
import incremental
import jobs
from hypercore import HyperCoreError, LengthStream
from incremental import IncrementalError, wait_task

log = logging.getLogger('backup.deploy')
DEFAULT_IMAGE_URL = 'https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img'
DEFAULT_IMAGE_NAME = 'hcm-ubuntu-noble-cloudimg.img'
DEFAULT_PROXY_IMAGE = 'ghcr.io/wvcollenburg/hcm-proxy:latest'


def _now() -> datetime:
    return datetime.now(timezone.utc)


def netplan(ip: dict) -> str | None:
    """netplan YAML for a static address, or None for DHCP."""
    if (ip or {}).get('mode') != 'static':
        return None
    dns = [d.strip() for d in str(ip.get('dns') or '').replace(';', ',').split(',') if d.strip()]
    lines = ['network:', '  version: 2', '  ethernets:', '    primary:', '      match:', '        name: "en*"',
             '      dhcp4: false', f"      addresses: [{ip['address']}/{ip.get('prefix') or 24}]"]
    if ip.get('gateway'):
        lines += ['      routes:', f"        - to: default", f"          via: {ip['gateway']}"]
    if dns:
        lines += ['      nameservers:', f"        addresses: [{', '.join(dns)}]"]
    return '\n'.join(lines) + '\n'


_MANIFEST_TYPES = ', '.join(('application/vnd.oci.image.index.v1+json', 'application/vnd.oci.image.manifest.v1+json',
                             'application/vnd.docker.distribution.manifest.list.v2+json',
                             'application/vnd.docker.distribution.manifest.v2+json'))


def parse_image_ref(ref: str) -> dict:
    """'ghcr.io/org/hcm-proxy:1.2' -> registry host, repository and tag/digest, with Docker Hub's rules
    for bare names ('nginx' is docker.io/library/nginx:latest)."""
    ref = (ref or '').strip()
    if not ref or any(c.isspace() for c in ref):
        raise ValueError(f'not an image reference: {ref!r}')
    digest = None
    if '@' in ref:
        ref, digest = ref.split('@', 1)
    first, _, rest = ref.partition('/')
    if rest and ('.' in first or ':' in first or first == 'localhost'):
        registry, path = first, rest
    else:
        registry, path = 'docker.io', ref
    tag = 'latest'
    if ':' in path.rsplit('/', 1)[-1]:
        path, tag = path.rsplit(':', 1)
    if registry == 'docker.io' and '/' not in path:
        path = 'library/' + path
    host = 'registry-1.docker.io' if registry == 'docker.io' else registry
    return {'registry': registry, 'host': host, 'repository': path, 'reference': digest or tag}


def _http_get(url: str, headers: dict, timeout: float) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, headers=headers, method='GET')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:      # noqa: S310 (https registry URL built above)
            return r.status, dict(r.headers), r.read(65536)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read(65536)


def image_pullable(ref: str, *, fetch=_http_get, timeout: float = 8.0) -> dict:
    """Ask the registry whether the image can be pulled without credentials, the way the VM will.

    ok True: the manifest is served. ok False: the registry refuses an anonymous pull (private or
    missing image), 'detail' says so. ok None: no answer from here (the backup container may sit
    behind a stricter firewall than the VM), so the deployment goes ahead."""
    try:
        r = parse_image_ref(ref)
    except ValueError as e:
        return {'ok': False, 'detail': str(e)}
    url = f"https://{r['host']}/v2/{r['repository']}/manifests/{r['reference']}"
    headers = {'Accept': _MANIFEST_TYPES}
    try:
        status, hdrs, _ = fetch(url, headers, timeout)
        if status == 401:
            challenge = next((v for k, v in hdrs.items() if k.lower() == 'www-authenticate'), '')
            m = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
            if challenge.lower().startswith('bearer') and m.get('realm'):
                q = {'scope': m.get('scope') or f"repository:{r['repository']}:pull"}
                if m.get('service'):
                    q['service'] = m['service']
                ts, _, body = fetch(f"{m['realm']}?{urllib.parse.urlencode(q)}", {}, timeout)
                token = ''
                if ts == 200:
                    try:
                        j = json.loads(body or b'{}')
                        token = j.get('token') or j.get('access_token') or ''
                    except ValueError:
                        token = ''
                if not token:
                    return {'ok': False, 'detail': f"{r['host']} hands out no anonymous pull token for {r['repository']} "
                                                   f"(HTTP {ts}): the image is private or does not exist"}
                status, hdrs, _ = fetch(url, {**headers, 'Authorization': f'Bearer {token}'}, timeout)
    except (OSError, ValueError) as e:                        # URLError, timeouts, bad JSON
        return {'ok': None, 'detail': f"registry {r['host']} not reachable from the backup service ({e}); the VM may still reach it"}
    if status == 200:
        return {'ok': True, 'detail': None}
    if status in (401, 403, 404):
        return {'ok': False, 'detail': f"{r['host']} refuses an anonymous pull of {r['repository']}:{r['reference']} "
                                       f"(HTTP {status}): the image is private or does not exist"}
    return {'ok': None, 'detail': f"unexpected HTTP {status} from {r['host']}"}


def cloud_init_userdata(*, token: str, proxy_image: str, tz: str = 'UTC', hostname: str = 'hcm-proxy',
                        ip: dict | None = None, authorized_key: str | None = None, image_tar_url: str | None = None) -> str:
    """#cloud-config for a proxy VM. Idempotent on reboot (docker --restart, module files)."""
    np = netplan(ip or {})
    files = [
        ('/etc/modprobe.d/nbd.conf', 'options nbd max_part=0\n'),
        ('/etc/modules-load.d/nbd.conf', 'nbd\n'),
        ('/etc/hcm-proxy.env', f'PROXY_API_TOKEN={token}\nPROXY_TLS=auto\nPROXY_HOST_NET=/host/sys/class/net\nTZ={tz}\n'),
        ('/usr/local/sbin/hcm-proxy-start.sh',
         '#!/bin/sh\nset -e\nexec >>/var/log/hcm-proxy-start.log 2>&1\nmodprobe nbd max_part=0 || true\n'
         # the pull is retried: right after boot DNS and the default route can lag the package install
         + (f'curl -fsSL "{image_tar_url}" | docker load\n' if image_tar_url else
            f'for i in 1 2 3 4 5 6; do docker pull {proxy_image} && break; echo "pull failed (attempt $i)"; sleep 20; done\n') +
         'docker rm -f hcm-proxy >/dev/null 2>&1 || true\n'
         'exec docker run -d --name hcm-proxy --restart unless-stopped --privileged --memory 1g -p 8083:8083 '
         '-v /dev:/dev -v /sys:/host/sys:ro -v hcm-proxy-data:/var/lib/hcm-proxy --env-file /etc/hcm-proxy.env '
         f'{proxy_image}\n'),
    ]
    if np:
        files.append(('/etc/netplan/60-hcm-proxy.yaml', np))
    doc = ['#cloud-config', f'hostname: {hostname}', 'manage_etc_hosts: true', 'package_update: true',
           'packages:', '  - docker.io', '  - qemu-guest-agent', '  - lvm2', 'write_files:']
    for path, content in files:
        mode = '0755' if path.endswith('.sh') else ('0600' if path.endswith('.env') else '0644')
        doc += [f'  - path: {path}', f'    permissions: "{mode}"', '    content: |']
        doc += ['      ' + line for line in content.rstrip('\n').split('\n')]
    if authorized_key:
        doc += ['ssh_authorized_keys:', f'  - {authorized_key}']
    doc += ['runcmd:',
            "  - sed -i 's#^devices {#devices {\\n    global_filter = [ \"a|^/dev/vda|\", \"r|.*|\" ]#' /etc/lvm/lvm.conf",
            '  - systemctl enable --now qemu-guest-agent',
            '  - systemctl enable --now docker']
    if np:
        doc += ['  - chmod 600 /etc/netplan/60-hcm-proxy.yaml', '  - netplan apply']
    doc += ['  - /usr/local/sbin/hcm-proxy-start.sh',
            'final_message: "HyperCore Manager backup proxy ready after $UPTIME seconds"']
    return '\n'.join(doc) + '\n'


def cloud_init_metadata(hostname: str) -> str:
    return f'instance-id: {hostname}-{uuidlib.uuid4().hex[:8]}\nlocal-hostname: {hostname}\n'


def vm_body(name: str, vcpu: int, mem_gb: int, vlan: int, userdata: str, metadata: str) -> dict:
    """VirDomain create body. HyperCore wants the cloud-init documents base64-encoded (a plain
    string is answered with a 500) and builds a NoCloud ISO from them."""
    b64 = lambda t: base64.b64encode(t.encode()).decode()  # noqa: E731
    return {'dom': {'name': name, 'mem': int(mem_gb) << 30, 'numVCPU': int(vcpu), 'operatingSystem': 'os_other',
                    'tags': 'hcm-proxy', 'blockDevs': [], 'netDevs': [{'type': 'VIRTIO', 'vlan': int(vlan or 0), 'connected': True}],
                    'cloudInitData': {'userData': b64(userdata), 'metaData': b64(metadata)}},
            'options': {'attachGuestToolsISO': False, 'machineTypeKeyword': 'uefi', 'useUEFISecureBoot': False}}


# ------------------------------------------------------------------ cluster steps
def find_image(hc, name: str) -> dict | None:
    return next((d for d in (hc._request('GET', '/VirtualDisk') or []) if d.get('name') == name), None)


def upload_image(hc, name: str, url: str, on_progress=None) -> dict:
    """Stream a cloud image from `url` into the cluster as a VirtualDisk named `name`."""
    import requests
    src = requests.get(url, stream=True, timeout=(30, 600))
    src.raise_for_status()
    size = int(src.headers.get('Content-Length') or 0)
    if not size:
        raise IncrementalError('the image URL does not report a Content-Length; download it and upload the file through the HyperCore UI instead')
    sent = {'n': 0}

    def gen():
        for chunk in src.iter_content(1 << 20):
            sent['n'] += len(chunk)
            if on_progress and sent['n'] % (64 << 20) < (1 << 20):
                on_progress(sent['n'], size)
            yield chunk
    # the body of a PUT is sent under the *connect* timeout (urllib3 applies the read timeout only
    # once the response is awaited); a cluster that pauses while it swallows the image over a slow
    # link must not abort the upload after 30 s
    r = hc.http().put(f'{hc.base}/VirtualDisk/upload', params={'filename': name, 'filesize': size}, data=LengthStream(gen(), size),
                      timeout=(900, 7200), headers={'Content-Type': 'application/octet-stream'})
    if not r.ok:
        raise HyperCoreError(f'image upload failed ({r.status_code}): {r.text[:200]}')
    tag = (r.json() or {}).get('taskTag')
    if tag:
        wait_task(hc, tag, 'image upload', 3600)
    img = find_image(hc, name)
    if not img:
        raise IncrementalError('image upload finished but the VirtualDisk is not listed')
    return img


def attach_image(hc, image_uuid: str, vm_uuid: str, disk_gb: int) -> str:
    r = hc._request('POST', f'/VirtualDisk/{image_uuid}/attach',
                    json={'template': {'virDomainUUID': vm_uuid, 'type': 'VIRTIO_DISK', 'capacity': int(disk_gb) << 30},
                          'options': {'readOnly': False, 'regenerateDiskID': True}}) or {}
    if not r.get('taskTag'):
        raise HyperCoreError(f'attach not accepted: {r}')
    wait_task(hc, r['taskTag'], 'attach image', 900)
    return r.get('createdUUID', '')


def wait_for_ip(hc, vm_uuid: str, timeout: float = 900) -> str | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        vm = hc.get_vm(vm_uuid)
        for n in vm.get('netDevs', []):
            for ip in n.get('ipv4Addresses') or []:
                if ip and not ip.startswith('127.') and not ip.startswith('169.254.'):
                    return ip
        time.sleep(10)
    return None


# ------------------------------------------------------------------ the deployment
def run_deployment(dep_id: int) -> None:
    d = db.get_deployment(dep_id)
    if not d:
        return
    p = d['params']
    try:
        hc = jobs.client_for(d['cluster_id'])
        name = p['name']
        if any((v.get('name') or '').lower() == name.lower() for v in hc.list_vms_full()):
            raise IncrementalError(f"a VM named '{name}' already exists on the cluster")
        # 1. image
        img_name = p.get('image_name') or DEFAULT_IMAGE_NAME
        img = find_image(hc, img_name)
        if not img:
            db.update_deployment(dep_id, message=f'uploading cloud image {img_name} to the cluster')
            img = upload_image(hc, img_name, p.get('image_url') or DEFAULT_IMAGE_URL,
                               on_progress=lambda n, s: db.update_deployment(dep_id, message=f'uploading cloud image: {n >> 20}/{s >> 20} MiB'))
        # 2. VM with cloud-init
        token = p.get('token') or secrets.token_hex(24)
        userdata = cloud_init_userdata(token=token, proxy_image=p.get('proxy_image') or DEFAULT_PROXY_IMAGE, tz=p.get('tz') or 'UTC',
                                       hostname=name, ip=p.get('ip'), authorized_key=p.get('authorized_key'),
                                       image_tar_url=p.get('proxy_image_tar_url') or None)
        db.update_deployment(dep_id, message='creating the proxy VM')
        r = hc._request('POST', '/VirDomain', json=vm_body(name, p.get('vcpu') or 1, p.get('mem_gb') or 2, p.get('vlan') or 0, userdata, cloud_init_metadata(name))) or {}
        if not r.get('createdUUID'):
            raise HyperCoreError(f'VM not created: {r}')
        wait_task(hc, r['taskTag'], 'create VM', 600)
        vm_uuid = r['createdUUID']
        db.update_deployment(dep_id, vm_uuid=vm_uuid)
        # 3. disk from the image, boot order, start
        disk = attach_image(hc, img['uuid'], vm_uuid, p.get('disk_gb') or 20)
        if disk:
            t = hc.patch_vm(vm_uuid, bootDevices=[disk])
            if t:
                wait_task(hc, t, 'boot order', 300)
        t = hc.vm_action(vm_uuid, 'START')
        if t:
            wait_task(hc, t, 'start', 600)
        # 4. address
        ip = (p.get('ip') or {}).get('address') if (p.get('ip') or {}).get('mode') == 'static' else None
        if not ip:
            db.update_deployment(dep_id, message='VM started; waiting for its IP address (guest agent)')
            ip = wait_for_ip(hc, vm_uuid)
        if not ip:
            raise IncrementalError('the VM did not report an IP address within 15 minutes; register the proxy by hand once it is up')
        # 5. register + probe until the container answers
        url = f'https://{ip}:8083'
        pid = db.add_proxy(d['cluster_id'], name, url, token, kind='remote', vm_uuid=vm_uuid)
        db.update_deployment(dep_id, proxy_id=pid, message=f'VM at {ip}; waiting for the proxy container to come up')
        deadline = time.monotonic() + 1200
        result = {'ok': False}
        while time.monotonic() < deadline:
            result = incremental.probe_proxy(pid, hc)
            if result.get('ok'):
                break
            db.update_proxy(pid, tls_fingerprint=None)      # a failed TOFU attempt must not pin a half-started service
            time.sleep(20)
        if not result.get('ok'):
            raise IncrementalError(f"proxy registered but not reachable at {url} after 20 minutes ({result.get('error') or result.get('detail')}). "
                                   'The VM is up but nothing answers on port 8083, so the proxy container did not start: '
                                   '/var/log/hcm-proxy-start.log on the VM says why (usually the image could not be pulled). '
                                   'Fix the cause and run /usr/local/sbin/hcm-proxy-start.sh on the VM, or delete the VM and deploy again.')
        # 6. the cloud-init ISO holds the token in clear: get it off the VM now that the proxy runs
        db.update_deployment(dep_id, message='removing the cloud-init data from the VM')
        try:
            removed = incremental.remove_cloud_init_disk(hc, vm_uuid)
        except (HyperCoreError, IncrementalError) as e:
            removed = None
            log.warning('[deploy %s] cloud-init disk not removed (%s); the daily sweep retries', dep_id, e)
        db.update_deployment(dep_id, status='COMPLETE', finished_at=_now(),
                             message=f'proxy {name} running at {url} and registered; cloud-init data '
                                     + ({'deleted': 'removed from the VM', 'ejected': 'ejected from the VM'}.get(removed) or 'still attached, the daily sweep retries'))
        log.info('[deploy %s] done: %s at %s', dep_id, name, url)
    except (IncrementalError, HyperCoreError, KeyError, ValueError) as e:
        db.update_deployment(dep_id, status='ERROR', message=str(e), finished_at=_now())
        log.error('[deploy %s] %s', dep_id, e)
    except Exception as e:                                  # noqa: BLE001
        db.update_deployment(dep_id, status='ERROR', message=f'Unexpected error: {e}', finished_at=_now())
        log.exception('[deploy %s] unexpected error', dep_id)
