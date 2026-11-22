"""
netcheck.py — network checks of a test-restored VM on an isolated test VLAN (plans/PLAN-test-restores.md, T2).

The proxy VM has a second NIC on the test VLAN (added at deploy, or later with a power cycle). For one check
run the NIC is brought up with a temporary probe address, every target is pinged and its TCP ports tried, and
the address is removed and the NIC taken down again. HyperCore Manager never touches the test VLAN itself.

A restored VM keeps its production IP address, and the proxy's management NIC may well sit in the same subnet.
A plain connect would then leave through the management NIC and could reach the *production* VM: a false pass.
So every probe is bound to the test NIC (SO_BINDTODEVICE for TCP, `ping -I` for ICMP), the probe address is
added with `noprefixroute` (no subnet route that could compete with the management NIC's), and each target gets
a host route through the test NIC for the duration of the check.

The test VLAN usually has no DHCP server, and a VM configured for DHCP would come up without an address there.
So the proxy can serve DHCP on the test NIC during the check: dnsmasq with one static lease per test-VM MAC (the
address its source NIC has), no pool, no router, no DNS: nothing else on the VLAN gets an address. The caller refuses targets that are the proxy's own
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


def answered(results: dict) -> bool:
    """One target answers on every port asked for (with no ports asked for: answers ping)."""
    for r in results.values():
        ports = r.get('ports') or {}
        if (ports and all(ports.values())) or (not ports and r.get('ping')):
            return True
    return False


def dnsmasq_cmd(dev: str, probe: ipaddress.IPv4Interface, leases: list[dict]) -> list[str]:
    """DHCP for the test VM only: a static lease per MAC (its source NIC's address), no pool, no DNS, no router.
    Nothing else on the VLAN gets an address."""
    cmd = ['dnsmasq', '--no-daemon', '--port=0', f'--interface={dev}', '--bind-interfaces', '--except-interface=lo',
           '--dhcp-authoritative', '--leasefile-ro', '--dhcp-option=3', '--dhcp-option=6', '--log-dhcp']
    nets = set()
    for l in leases:
        a = ipaddress.IPv4Address(str(l['ip']))
        net = ipaddress.IPv4Network(f'{a}/{probe.network.prefixlen}', strict=False)
        if net not in nets:
            cmd.append(f'--dhcp-range={net.network_address},static,{net.netmask},1h')
            nets.add(net)
        cmd.append(f"--dhcp-host={str(l['mac']).lower()},{a},infinite")
    return cmd


def run(mac: str, probe: str, targets: list[dict], settle_s: float = 3.0, ping_fn=ping, tcp_fn=tcp, ip_fn=_ip,
        sys_net: str = SYS_NET, dhcp: list[dict] | None = None, wait_s: float = 0, interval_s: float = 10,
        cancelled=None, popen=subprocess.Popen, sleep=time.sleep, clock=time.monotonic) -> dict:
    """targets: [{ip, ports: [int]}]. With wait_s, probe every interval_s until a target answers (the VM is still
    booting) or the time is up. dhcp: [{mac, ip}] serves those static leases on the test VLAN meanwhile (a VM
    configured for DHCP gets its own address; the test VLAN has no DHCP server of its own).
    Returns {'interface', 'probe', 'results': {ip: {'ping': bool, 'ports': {port: bool}}}, 'answered', 'after_s'}."""
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
    routes, server = [], None
    t0 = clock()
    try:                                                     # from the link up on: the finally always cleans up
        ip_fn('link', 'set', dev, 'up')
        # with its prefix but without the subnet route: dnsmasq needs to see the subnet on the interface, the
        # management NIC (often in the same subnet) must keep the route. 'replace', not 'add': an interrupted
        # check (container stopped) left the address behind, and 'add' then failed until the VM rebooted
        ip_fn('addr', 'replace', f'{pr.ip}/{pr.network.prefixlen}', 'dev', dev, 'noprefixroute')
        for a in cleaned:
            # a host route per target through the test NIC, for the duration of the check only
            ip_fn('route', 'replace', f'{a}/32', 'dev', dev, 'src', str(pr.ip))
            routes.append(a)
        if dhcp:
            server = popen(dnsmasq_cmd(dev, pr, dhcp), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        sleep(settle_s)                                      # link up, switch learns the port
        while True:
            results = {}
            for t in targets:
                ip = str(t['ip'])
                results[ip] = {'ping': ping_fn(ip, dev), 'ports': {int(p): tcp_fn(ip, int(p), dev) for p in t.get('ports') or []}}
            ok = answered(results)
            if ok or clock() - t0 >= wait_s or (cancelled and cancelled()):
                return {'interface': dev, 'probe': str(pr.ip), 'results': results, 'answered': ok,
                        'after_s': round(clock() - t0, 1), 'dhcp': bool(dhcp)}
            sleep(interval_s)
    finally:
        if server is not None:
            try:
                server.terminate()
                server.wait(timeout=10)
            except Exception:                                # noqa: BLE001
                try:
                    server.kill()
                except Exception:                            # noqa: BLE001
                    pass
        for a in routes:
            try:
                ip_fn('route', 'del', f'{a}/32', 'dev', dev)
            except NetcheckError:
                pass
        for args in (('addr', 'del', f'{pr.ip}/{pr.network.prefixlen}', 'dev', dev), ('link', 'set', dev, 'down')):
            try:
                ip_fn(*args)
            except NetcheckError:
                pass
