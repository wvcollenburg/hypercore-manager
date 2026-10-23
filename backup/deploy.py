"""
deploy.py — create a backup proxy VM on a cluster from an Ubuntu cloud image with cloud-init.

What the panel does for an administrator:
  1. make sure the cluster holds the cloud image as an uploaded VirtualDisk (uploaded once from a
     URL the backup container can reach, or already present under a known name)
  2. create a UEFI VM with cloud-init user-data that installs Docker and the guest agent, sets the
     nbd and LVM host settings, and starts a bootstrap receiver on port 8083 that waits for the
     proxy image
  3. attach the image as the VM's disk (grown to the requested size), set the boot order, start
  4. wait for the VM's IP address (static from the form, or reported by the guest agent)
  5. push this installation's proxy image to the receiver (the VM loads it and starts the proxy
     container with PROXY_TLS=auto and the token), register the proxy (https://<ip>:8083) and
     probe it until it answers
  6. remove the cloud-init ISO from the VM (it carries the token; the daily sweep retries)

Everything here is plain API calls; the cloud-init document is rendered by cloud_init_userdata()
so it can be reviewed and tested without a cluster.
"""
from __future__ import annotations

import base64
import logging
import secrets
import time
import uuid as uuidlib
from datetime import datetime, timezone

import db
import incremental
import jobs
import proxy_image
from hypercore import HyperCoreError, LengthStream
from incremental import IncrementalError, wait_task
from proxy_client import ProxyClient, ProxyError, certificate_fingerprint

log = logging.getLogger('backup.deploy')
DEFAULT_IMAGE_URL = 'https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img'
DEFAULT_IMAGE_NAME = 'hcm-ubuntu-noble-cloudimg.img'


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


BOOTSTRAP_PY = r"""#!/usr/bin/env python3
# First contact of a freshly deployed proxy VM. Listens on the proxy port for HyperCore Manager
# to push the proxy image (POST /update, bearer token, X-Image-Tag), drops it where
# hcm-proxy-update.service picks it up, and exits; the proxy container takes the port from here.
import http.server, json, os, socket, ssl, subprocess, sys, threading

ENV = os.environ.get('HCM_PROXY_ENV', '/etc/hcm-proxy.env')
DROP = os.environ.get('HCM_PROXY_UPDATE_DIR', '/var/lib/hcm-proxy-update')
TLS_DIR = os.environ.get('HCM_PROXY_TLS_DIR', '/etc/hcm-proxy/tls')
PORT = int(os.environ.get('HCM_PROXY_BOOTSTRAP_PORT', '8083'))
TOKEN = ''
for line in open(ENV):
    if line.startswith('PROXY_API_TOKEN='):
        TOKEN = line.split('=', 1)[1].strip()


def ensure_certificate():
    # The same self-signed certificate serves this receiver and, mounted into the container, the
    # proxy afterwards: HyperCore Manager pins it on first contact and the pin holds through the
    # handover. Ten years, RSA 2048, hostname and addresses as SANs, like the proxy's own tls.py.
    os.makedirs(TLS_DIR, exist_ok=True)
    crt, key = os.path.join(TLS_DIR, 'proxy.crt'), os.path.join(TLS_DIR, 'proxy.key')
    if os.path.exists(crt) and os.path.exists(key):
        return crt, key
    host = socket.gethostname()
    sans = ['DNS:' + host, 'DNS:localhost', 'IP:127.0.0.1']
    try:
        for ip in {i[4][0] for i in socket.getaddrinfo(host, None, socket.AF_INET)}:
            sans.append('IP:' + ip)
    except socket.gaierror:
        pass
    base = ['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '3650', '-keyout', key, '-out', crt, '-subj', '/CN=' + host]
    r = subprocess.run(base + ['-addext', 'subjectAltName=' + ','.join(sans)], capture_output=True, text=True)
    if r.returncode:
        r = subprocess.run(base, capture_output=True, text=True)
    if r.returncode:
        sys.exit('certificate generation failed: ' + (r.stderr or r.stdout).strip()[:300])
    os.chmod(key, 0o600)
    return crt, key


class Handler(http.server.BaseHTTPRequestHandler):
    timeout = 120                     # a silent peer must not hold a thread forever

    def setup(self):
        # the listening socket is wrapped with do_handshake_on_connect=False so a peer that
        # connects and goes quiet (port scanners, probes) blocks only its own thread, not accept()
        self.request.settimeout(20)
        self.request.do_handshake()
        super().setup()

    def _json(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code); self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)

    def log_message(self, *a):
        sys.stderr.write('%s - %s\n' % (self.address_string(), a[0] % a[1:]))

    def do_GET(self):
        if self.path == '/health':
            return self._json(200, {'status': 'ok', 'service': 'hcm-proxy-bootstrap'})
        self._json(404, {'error': 'not found'})

    def do_POST(self):
        if TOKEN and self.headers.get('Authorization', '') != 'Bearer ' + TOKEN:
            return self._json(401, {'error': 'Unauthorized'})
        if self.path != '/update':
            return self._json(404, {'error': 'not found'})
        tag = (self.headers.get('X-Image-Tag') or '').strip()
        n = int(self.headers.get('Content-Length') or 0)
        if not tag or not n:
            return self._json(400, {'error': 'X-Image-Tag and a tarball body are required'})
        os.makedirs(DROP, exist_ok=True)
        part = os.path.join(DROP, 'image.tar.part')
        with open(part, 'wb') as fh:
            left = n
            while left:
                chunk = self.rfile.read(min(left, 1 << 20))
                if not chunk:
                    break
                fh.write(chunk); left -= len(chunk)
        if left:
            os.unlink(part)
            return self._json(400, {'error': 'short body'})
        os.replace(part, os.path.join(DROP, 'image.tar'))
        with open(os.path.join(DROP, 'image.tag'), 'w') as fh:
            fh.write(tag + '\n')
        self._json(202, {'accepted': True, 'tag': tag, 'bytes': n})
        self.wfile.flush()
        open(os.path.join(DROP, 'ready'), 'w').close()       # after the answer: the host acts on this
        threading.Thread(target=self.server.shutdown, daemon=True).start()


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        # a failed handshake or a dropped connection is logged, never fatal
        import traceback
        sys.stderr.write('hcm-proxy-bootstrap: connection from %s failed: %s\n' % (client_address[0], traceback.format_exc().strip().splitlines()[-1]))


crt, key = ensure_certificate()
srv = Server(('0.0.0.0', PORT), Handler)
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain(crt, key)
srv.socket = ctx.wrap_socket(srv.socket, server_side=True, do_handshake_on_connect=False)
sys.stderr.write('hcm-proxy-bootstrap: waiting for the proxy image on port %d (TLS)\n' % PORT)
while True:
    try:
        srv.serve_forever()
        break
    except Exception as e:            # the loop itself must survive anything a peer can do
        sys.stderr.write('hcm-proxy-bootstrap: serve loop error, continuing: %r\n' % (e,))
sys.stderr.write('hcm-proxy-bootstrap: image received, handing over\n')
"""

RUN_SH = """#!/bin/sh
# Runs the proxy container from the image the last push loaded (hcm-proxy.service).
set -e
image=$(cat /etc/hcm-proxy/image)
modprobe nbd max_part=0 || true
docker rm -f hcm-proxy >/dev/null 2>&1 || true
exec docker run -d --name hcm-proxy --restart unless-stopped --privileged --memory 1g -p 8083:8083 \\
  -v /dev:/dev -v /sys:/host/sys:ro -v hcm-proxy-data:/var/lib/hcm-proxy -v /var/lib/hcm-proxy-update:/update \\
  -v /etc/hcm-proxy/tls:/etc/hcm-proxy/tls:ro --env-file /etc/hcm-proxy.env "$image"
"""

UPDATE_SH = """#!/bin/sh
# Loads the image HyperCore Manager pushed into /var/lib/hcm-proxy-update and restarts the proxy on it
# (hcm-proxy-update.service, fired by the path unit when the 'ready' marker appears).
set -e
exec >>/var/log/hcm-proxy-update.log 2>&1
d=/var/lib/hcm-proxy-update
[ -f "$d/ready" ] || exit 0
mv "$d/ready" "$d/working"
tag=$(cat "$d/image.tag")
echo "$(date -Is) loading $tag"
docker load < "$d/image.tar"
mkdir -p /etc/hcm-proxy && echo "$tag" > /etc/hcm-proxy/image
rm -f "$d/image.tar" "$d/working"
systemctl stop hcm-proxy-bootstrap.service 2>/dev/null || true
systemctl restart hcm-proxy.service
docker image prune -f >/dev/null 2>&1 || true
echo "$(date -Is) proxy restarted on $tag"
"""

UNITS = {
    '/etc/systemd/system/hcm-proxy.service': """[Unit]
Description=HyperCore Manager backup proxy container
After=docker.service
Requires=docker.service
ConditionPathExists=/etc/hcm-proxy/image

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/hcm-proxy-run.sh

[Install]
WantedBy=multi-user.target
""",
    '/etc/systemd/system/hcm-proxy-bootstrap.service': """[Unit]
Description=HyperCore Manager proxy bootstrap: waits for the proxy image to be pushed
After=network-online.target docker.service
Wants=network-online.target
ConditionPathExists=!/etc/hcm-proxy/image

[Service]
ExecStart=/usr/bin/python3 /usr/local/sbin/hcm-proxy-bootstrap.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
""",
    '/etc/systemd/system/hcm-proxy-update.path': """[Unit]
Description=HyperCore Manager proxy image drop

[Path]
PathExists=/var/lib/hcm-proxy-update/ready
Unit=hcm-proxy-update.service

[Install]
WantedBy=multi-user.target
""",
    '/etc/systemd/system/hcm-proxy-update.service': """[Unit]
Description=HyperCore Manager proxy image update
After=docker.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/hcm-proxy-update.sh
""",
}


def cloud_init_userdata(*, token: str, tz: str = 'UTC', hostname: str = 'hcm-proxy',
                        ip: dict | None = None, authorized_key: str | None = None) -> str:
    """#cloud-config for a proxy VM. The VM pulls nothing: it comes up with a bootstrap receiver
    on port 8083 (TLS, a self-signed certificate it generates and the proxy container keeps
    using), the backup service pushes the installation's proxy image to it, and the same drop
    directory takes every later update. Idempotent on reboot (systemd units, module files)."""
    np = netplan(ip or {})
    files = [
        ('/etc/modprobe.d/nbd.conf', 'options nbd max_part=0\n'),
        ('/etc/modules-load.d/nbd.conf', 'nbd\n'),
        ('/etc/hcm-proxy.env', f'PROXY_API_TOKEN={token}\nPROXY_TLS=cert\nPROXY_TLS_CERT=/etc/hcm-proxy/tls/proxy.crt\n'
                               f'PROXY_TLS_KEY=/etc/hcm-proxy/tls/proxy.key\nPROXY_HOST_NET=/host/sys/class/net\n'
                               f'PROXY_UPDATE_DIR=/update\nTZ={tz}\n'),
        ('/usr/local/sbin/hcm-proxy-run.sh', RUN_SH),
        ('/usr/local/sbin/hcm-proxy-update.sh', UPDATE_SH),
        ('/usr/local/sbin/hcm-proxy-bootstrap.py', BOOTSTRAP_PY),
    ] + sorted(UNITS.items())
    if np:
        files.append(('/etc/netplan/60-hcm-proxy.yaml', np))
    doc = ['#cloud-config', f'hostname: {hostname}', 'manage_etc_hosts: true', 'package_update: true',
           'packages:', '  - docker.io', '  - qemu-guest-agent', '  - lvm2', 'write_files:']
    for path, content in files:
        mode = '0755' if path.endswith(('.sh', '.py')) else ('0600' if path.endswith('.env') else '0644')
        doc += [f'  - path: {path}', f'    permissions: "{mode}"', '    content: |']
        doc += [('      ' + line).rstrip() for line in content.rstrip('\n').split('\n')]
    if authorized_key:
        doc += ['ssh_authorized_keys:', f'  - {authorized_key}']
    doc += ['runcmd:',
            "  - sed -i 's#^devices {#devices {\\n    global_filter = [ \"a|^/dev/vda|\", \"r|.*|\" ]#' /etc/lvm/lvm.conf",
            '  - mkdir -p /var/lib/hcm-proxy-update /etc/hcm-proxy/tls',
            '  - systemctl enable --now qemu-guest-agent',
            '  - systemctl enable --now docker']
    if np:
        doc += ['  - chmod 600 /etc/netplan/60-hcm-proxy.yaml', '  - netplan apply']
    doc += ['  - systemctl daemon-reload',
            '  - systemctl enable --now hcm-proxy-update.path hcm-proxy.service hcm-proxy-bootstrap.service',
            'final_message: "HyperCore Manager backup proxy VM ready for its image after $UPTIME seconds"']
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


def push_image(ip: str, token: str, on_message=None, timeout: float = 1200, poll: float = 15, sleep=time.sleep,
               fingerprint_of=certificate_fingerprint) -> dict:
    """Wait for the VM's bootstrap receiver (cloud-init installs Docker first, minutes), pin the
    certificate it presents and push the installation's proxy image over that connection. The
    proxy container serves the same certificate afterwards, so the pin taken here is the one the
    registration keeps: no second trust-on-first-use after the handover. A VM that already runs
    the proxy is left alone. Returns the receiver's answer plus 'tls_fingerprint'."""
    say = on_message or (lambda m: None)
    url = f'https://{ip}:8083'
    deadline = time.monotonic() + timeout
    service, fp, client = None, None, None
    while time.monotonic() < deadline:
        try:
            fp = fingerprint_of(url)
            client = ProxyClient(url, token, tls_fingerprint=fp, timeout=20)
            service = (client.health() or {}).get('service')
        except (ProxyError, OSError):
            service = None
        if service == 'hcm-proxy-bootstrap':
            break
        if service == 'hcm-proxy':
            return {'skipped': 'the proxy already runs', 'tls_fingerprint': fp}
        sleep(poll)
    if service != 'hcm-proxy-bootstrap':
        raise IncrementalError(f'the VM at {ip} did not come up with its bootstrap receiver within {timeout / 60:.0f} minutes; '
                               'cloud-init may have failed (no DHCP, no route for apt): check the VM console')
    info = proxy_image.stack_image()
    if not info.get('available'):
        raise IncrementalError(info.get('detail') or 'the installation has no proxy image to push')
    say(f"pushing proxy image {info['version']} to the VM at {ip} (certificate {fp[:16]}… pinned)")
    answer = proxy_image.push(client)
    answer['tls_fingerprint'] = fp
    say(f"proxy image {answer.get('tag')} loaded on the VM ({(answer.get('bytes') or 0) >> 20} MiB); the VM starts the container")
    return answer


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
        userdata = cloud_init_userdata(token=token, tz=p.get('tz') or 'UTC', hostname=name, ip=p.get('ip'),
                                       authorized_key=p.get('authorized_key'))
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
        # 5. push the installation's proxy image to the VM's bootstrap receiver
        db.update_deployment(dep_id, message=f'VM at {ip}; waiting for it to finish installing (cloud-init)')
        pushed = push_image(ip, token, on_message=lambda m: db.update_deployment(dep_id, message=m))
        # 6. register with the pin taken at first contact, probe until the container answers
        url = f'https://{ip}:8083'
        pid = db.add_proxy(d['cluster_id'], name, url, token, kind='remote', vm_uuid=vm_uuid, tls_fingerprint=pushed.get('tls_fingerprint'))
        db.update_deployment(dep_id, proxy_id=pid, message=f'image loaded on the VM; waiting for the proxy container to come up')
        deadline = time.monotonic() + 600
        result = {'ok': False}
        while time.monotonic() < deadline:
            result = incremental.probe_proxy(pid, hc)
            if result.get('ok'):
                break
            time.sleep(20)                                  # the pin stays: it was taken from the receiver, not from a half-started service
        if not result.get('ok'):
            raise IncrementalError(f"proxy registered but not reachable at {url} after 10 minutes ({result.get('error') or result.get('detail')}). "
                                   'The image was pushed but the proxy container did not come up: /var/log/hcm-proxy-update.log '
                                   'on the VM says why. Fix the cause and run `systemctl restart hcm-proxy` there, or delete the VM and deploy again.')
        # 7. the cloud-init ISO holds the token in clear: get it off the VM now that the proxy runs
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
    except (IncrementalError, HyperCoreError, ProxyError, KeyError, ValueError) as e:
        db.update_deployment(dep_id, status='ERROR', message=str(e), finished_at=_now())
        log.error('[deploy %s] %s', dep_id, e)
    except Exception as e:                                  # noqa: BLE001
        db.update_deployment(dep_id, status='ERROR', message=f'Unexpected error: {e}', finished_at=_now())
        log.exception('[deploy %s] unexpected error', dep_id)
