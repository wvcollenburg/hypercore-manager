"""
netcheck.py — network checks of a test-restored VM on an isolated test VLAN (plans/PLAN-test-restores.md, T2).

The proxy VM has a second NIC on the test VLAN (added at deploy, or later with a power cycle). For one check
run the NIC is brought up with a temporary probe address, every target is pinged and its TCP ports tried, and
the address is removed and the NIC taken down again. HyperCore Manager never touches the test VLAN itself.

A restored VM keeps its production IP address, and the proxy's management NIC may well sit in the same subnet.
A plain connect would then leave through the management NIC and could reach the *production* VM: a false pass.
So every probe is bound to the test NIC (SO_BINDTODEVICE for TCP, `ping -I` for ICMP), the probe address is
added as a /32 (no subnet route that could compete with the management NIC's), and each target gets a host
route through the test NIC for the duration of the check. The caller refuses targets that are the proxy's own
infrastructure (its gateway, the share, HyperCore Manager), so those routes never cut the proxy off.

Needs the proxy container to run in the host's network namespace (`--network host`, proxies deployed from
1.7.0 on) and to be privileged (it is): only then does it see and configure the VM's NICs.
"""
from __future__ import annotations

import ipaddress
import os
import socket
import subprocess
import time

SYS_NET = '/sys/class/net'


class NetcheckError(Exception):
    pass


def interface_for_mac(mac: str, sys_net: str = SYS_NET) -> str | None:
    """The interface in this network namespace carrying `mac`, or None."""
    want = (mac or '').strip().lower()
    try:
        names = os.listdir(sys_net)
    except OSError:
        return None
    for n in sorted(names):
        try:
            with open(os.path.join(sys_net, n, 'address')) as fh:
                if fh.read().strip().lower() == want:
                    return n
        except OSError:
            continue
    return None


def host_network(sys_net: str = SYS_NET, host_sys_net: str | None = None) -> bool:
    """True when this container sees the VM's own NICs, i.e. it runs with --network host: one of the host's
    physical interfaces (from the bind-mounted host /sys) is present in our own namespace."""
    host_sys_net = host_sys_net or os.environ.get('PROXY_HOST_NET') or ''
    if not host_sys_net:
        return False
    try:
        host = {n for n in os.listdir(host_sys_net) if not n.startswith(('lo', 'docker', 'veth', 'br-'))}
        mine = set(os.listdir(sys_net))
    except OSError:
        return False
    return bool(host & mine)


def parse_probe(probe: str) -> ipaddress.IPv4Interface:
    try:
        iface = ipaddress.IPv4Interface(str(probe).strip())
    except ValueError:
        raise NetcheckError(f'the probe address {probe!r} is not an IPv4 address with a prefix (e.g. 172.16.0.250/25)') from None
    if iface.network.prefixlen == 32:
        raise NetcheckError('the probe address needs the test subnet\'s prefix (e.g. /24), not /32')
    return iface


def _ip(*args: str) -> None:
    r = subprocess.run(['ip', *args], capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        raise NetcheckError(f"ip {' '.join(args)}: {r.stderr.strip() or r.returncode}")


def ping(ip: str, dev: str, count: int = 2, wait_s: int = 2) -> bool:
    r = subprocess.run(['ping', '-n', '-q', '-I', dev, '-c', str(count), '-W', str(wait_s), ip],
                       capture_output=True, text=True, timeout=count * wait_s + 10)
    return r.returncode == 0


def tcp(ip: str, port: int, dev: str, timeout: float = 3.0) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, dev.encode() + b'\0')
        s.settimeout(timeout)
        s.connect((ip, int(port)))
        return True
    except OSError:
        return False
    finally:
        s.close()


def run(mac: str, probe: str, targets: list[dict], settle_s: float = 3.0, ping_fn=ping, tcp_fn=tcp, ip_fn=_ip,
        sys_net: str = SYS_NET) -> dict:
    """targets: [{ip, ports: [int]}]. Returns {'interface', 'results': {ip: {'ping': bool, 'ports': {port: bool}}}}."""
    dev = interface_for_mac(mac, sys_net)
    if not dev:
        raise NetcheckError(f'no interface with MAC {mac} in the proxy: it needs a NIC on the test VLAN and must run '
                            'with host networking (proxies deployed from 1.7.0 on; redeploy an older one)')
    pr = parse_probe(probe)
    cleaned: list[ipaddress.IPv4Address] = []
    for t in targets:
        try:
            cleaned.append(ipaddress.IPv4Address(str(t['ip'])))
        except (KeyError, ValueError):
            raise NetcheckError(f"not an IPv4 address: {t.get('ip')!r}") from None
    ip_fn('link', 'set', dev, 'up')
    ip_fn('addr', 'add', f'{pr.ip}/32', 'dev', dev)
    routes = []
    try:
        for a in cleaned:
            # a host route per target through the test NIC, for the few seconds of the check: the management
            # NIC's subnet route stays, but this address is reached on the test VLAN only
            ip_fn('route', 'replace', f'{a}/32', 'dev', dev, 'src', str(pr.ip))
            routes.append(a)
        time.sleep(settle_s)                                 # link up, switch learns the port
        results = {}
        for t in targets:
            ip = str(t['ip'])
            results[ip] = {'ping': ping_fn(ip, dev), 'ports': {int(p): tcp_fn(ip, int(p), dev) for p in t.get('ports') or []}}
        return {'interface': dev, 'probe': str(pr.ip), 'results': results}
    finally:
        for a in routes:
            try:
                ip_fn('route', 'del', f'{a}/32', 'dev', dev)
            except NetcheckError:
                pass
        for args in (('addr', 'del', f'{pr.ip}/32', 'dev', dev), ('link', 'set', dev, 'down')):
            try:
                ip_fn(*args)
            except NetcheckError:
                pass
