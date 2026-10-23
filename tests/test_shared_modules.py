"""shared/ modules: crypto round-trip, host normalisation, and copy drift guard."""
import importlib
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'shared'))


def test_shared_copies_are_identical(repo):
    r = subprocess.run([sys.executable, str(repo / 'tools' / 'sync_shared.py'), '--check'],
                       capture_output=True, text=True)
    assert r.returncode == 0, 'run tools/sync_shared.py:\n' + r.stdout


@pytest.fixture
def crypto(monkeypatch):
    monkeypatch.setenv('HCM_SECRET_KEY', 'unit-test-secret-key')
    import hcm_crypto
    return importlib.reload(hcm_crypto)


def test_crypto_roundtrip(crypto):
    token = crypto.encrypt('S3cret / ünïcode')
    assert token != 'S3cret / ünïcode'
    assert crypto.decrypt(token) == 'S3cret / ünïcode'
    assert crypto.key_configured()


def test_crypto_tokens_are_salted(crypto):
    assert crypto.encrypt('x') != crypto.encrypt('x')


def test_crypto_wrong_key_rejected(crypto, monkeypatch):
    token = crypto.encrypt('secret')
    monkeypatch.setenv('HCM_SECRET_KEY', 'another-key')
    with pytest.raises(crypto.InvalidToken):
        crypto.decrypt(token)


def test_crypto_missing_key_is_a_clear_error(monkeypatch):
    monkeypatch.delenv('HCM_SECRET_KEY', raising=False)
    import hcm_crypto
    importlib.reload(hcm_crypto)
    with pytest.raises(hcm_crypto.SecretKeyMissing):
        hcm_crypto.encrypt('x')
    assert not hcm_crypto.key_configured()


@pytest.mark.parametrize('raw, host', [
    ('10.0.0.241', '10.0.0.241'),
    ('  HTTPS://10.0.0.241/rest/v1  ', '10.0.0.241'),
    ('cluster.example.com:8443/', 'cluster.example.com:8443'),
    ('http://Cluster.Example.COM', 'cluster.example.com'),
])
def test_normalize_host_and_base_url(raw, host):
    import cluster_registry as cr
    assert cr.normalize_host(raw) == host
    assert cr.base_url(raw) == f'https://{host}/rest/v1'


def test_is_uuid():
    import cluster_registry as cr
    assert cr.is_uuid(cr.TEMPLATE_CLUSTER_ID)
    assert not cr.is_uuid('default') and not cr.is_uuid('') and not cr.is_uuid(None)


# --- duplicate detection (same HyperCore cluster registered twice) ----------------

def _cluster(cid, name, hc_uuid, registered):
    import cluster_registry as cr
    from datetime import datetime, timezone
    return cr.Cluster(cluster_id=cid, name=name, host='10.0.0.1', username='u', verify_tls=False, enabled=True,
                      is_template=False, hc_cluster_uuid=hc_uuid, colour=None, last_seen_at=None, last_error=None,
                      registered_at=datetime(2026, 10, registered, tzinfo=timezone.utc), updated_at=None)


def test_duplicate_groups_by_hypercore_uuid():
    import cluster_registry as cr
    a = _cluster('a' * 8 + '-0000-0000-0000-000000000001', 'Lab 241', 'hc-1', 1)
    b = _cluster('b' * 8 + '-0000-0000-0000-000000000002', 'Lab 242', 'hc-1', 2)
    c = _cluster('c' * 8 + '-0000-0000-0000-000000000003', 'Prod', 'hc-2', 3)
    d = _cluster('d' * 8 + '-0000-0000-0000-000000000004', 'Unknown yet', None, 4)
    groups = cr.duplicate_groups([c, b, d, a])
    assert len(groups) == 1
    assert [x.name for x in groups[0]] == ['Lab 241', 'Lab 242'], 'oldest registration first'
    assert cr.duplicate_of(b, [a, b, c, d]).name == 'Lab 241'
    assert cr.duplicate_of(a, [a, b, c, d]) is None, 'the keeper is not a duplicate'
    assert cr.duplicate_of(c, [a, b, c, d]) is None
    assert cr.duplicate_groups([a, c, d]) == []


# --- multi-node awareness: node records, addresses that follow the seed, usability ---------

@pytest.mark.parametrize('host,expected', [
    ('10.0.0.241', True), ('10.0.0.241:8443', True), ('[fe80::1]', True), ('[fe80::1]:443', True),
    ('hc-node1.example.net', False), ('https://hc.example.net/rest/v1', False), ('', False),
])
def test_is_ip(host, expected):
    import cluster_registry as cr
    assert cr.is_ip(host) is expected


def _api_node(**over):
    base = {'uuid': 'ba566cf9-0000-0000-0000-000000000001', 'peerID': 1, 'lanIP': '10.0.0.241',
            'backplaneIP': '10.20.30.241', 'networkStatus': 'ONLINE', 'currentDisposition': 'IN',
            'allowRunningVMs': True, 'virtualizationOnline': True, 'supportsVirtualization': True,
            'drives': [], 'cpuUsage': 3.2}
    base.update(over)
    return base


def test_node_from_api_keeps_the_fields_we_store():
    import cluster_registry as cr
    n = cr.node_from_api(_api_node())
    assert n == {'node_uuid': 'ba566cf9-0000-0000-0000-000000000001', 'peer_id': 1, 'lan_ip': '10.0.0.241',
                 'backplane_ip': '10.20.30.241', 'network_status': 'ONLINE', 'disposition': 'IN',
                 'allow_running_vms': True, 'virtualization_online': True, 'supports_virtualization': True}
    sparse = cr.node_from_api({'uuid': 'x'})
    assert sparse['network_status'] == 'UNKNOWN' and sparse['disposition'] == 'UNKNOWN'
    assert sparse['allow_running_vms'] is None and sparse['supports_virtualization'] is None


def test_node_address_follows_the_seed():
    import cluster_registry as cr
    calls = []
    def lookup(ip):
        calls.append(ip)
        return ('node2.lab.example.net', [], [ip])
    # registered by IP: the LAN IP as it is, no lookup
    assert cr.resolve_node_address('10.0.0.241', '10.0.0.242', lookup=lookup) == '10.0.0.242'
    assert calls == []
    # registered by name: the reverse-resolved name
    assert cr.resolve_node_address('hc.lab.example.net', '10.0.0.242', lookup=lookup) == 'node2.lab.example.net'
    assert calls == ['10.0.0.242']
    # lookup fails: keep the IP
    def broken(ip):
        raise OSError('no PTR')
    assert cr.resolve_node_address('hc.lab.example.net', '10.0.0.243', lookup=broken) == '10.0.0.243'
    assert cr.resolve_node_address('hc.lab.example.net', '', lookup=lookup) == ''


def test_node_usable_ignores_virtualization_flags():
    import cluster_registry as cr
    witness = {'present': True, 'network_status': 'ONLINE', 'disposition': 'IN', 'address': '10.0.0.4',
               'allow_running_vms': False, 'virtualization_online': False, 'supports_virtualization': False}
    assert cr.node_usable(witness), 'a witness serves the API'
    assert not cr.node_usable(dict(witness, network_status='OFFLINE'))
    assert not cr.node_usable(dict(witness, disposition='OUT'))
    assert not cr.node_usable(dict(witness, present=False))
    assert not cr.node_usable(dict(witness, address='', lan_ip=''))


def test_cluster_dataclass_carries_active_host():
    import cluster_registry as cr
    c = _cluster('a' * 8 + '-0000-0000-0000-000000000001', 'Lab 241', 'hc-1', 1)
    assert c.active_host is None
    assert 'active_host' in c.to_dict()


def _known(*addrs, **flags):
    return [{'address': a, 'lan_ip': a, 'present': True, 'network_status': 'ONLINE', 'disposition': 'IN', **flags} for a in addrs]


def test_order_candidates_no_preferred_node():
    import cluster_registry as cr
    nodes = _known('10.0.0.1', '10.0.0.2', '10.0.0.3')
    # first contact, nothing recorded yet: the usable nodes, the seed is among them
    assert cr.order_candidates('10.0.0.1', None, nodes) == ['10.0.0.1', '10.0.0.2', '10.0.0.3']
    # the node in use comes first, the rest rotate after it, the seed gets no preference
    assert cr.order_candidates('10.0.0.1', '10.0.0.2', nodes) == ['10.0.0.2', '10.0.0.3', '10.0.0.1']
    assert cr.order_candidates('10.0.0.1', '10.0.0.2', nodes, current='10.0.0.3') == ['10.0.0.3', '10.0.0.1', '10.0.0.2']
    # a seed that is not a node (DNS name, VIP) is the last resort only
    assert cr.order_candidates('hc.example.net', None, nodes) == ['10.0.0.1', '10.0.0.2', '10.0.0.3', 'hc.example.net']
    # nothing discovered yet: the seed alone
    assert cr.order_candidates('10.0.0.1', None, []) == ['10.0.0.1']
    # offline / out / gone nodes are skipped; the current one is kept even when flagged (one failure moves on)
    nodes[1]['network_status'] = 'OFFLINE'; nodes[2]['present'] = False
    assert cr.order_candidates('10.0.0.1', '10.0.0.2', nodes) == ['10.0.0.2', '10.0.0.1']


def test_timezone_helpers():
    import cluster_registry as cr
    assert cr.zone_is_valid('US/Eastern') and cr.zone_is_valid('Europe/Amsterdam') and not cr.zone_is_valid('Mars/Olympus')
    assert cr.timezone_from_api([{'uuid': 'timezone_guid', 'timeZone': 'US/Eastern'}]) == 'US/Eastern'
    assert cr.timezone_from_api({'timeZone': 'Nowhere/Land'}) is None and cr.timezone_from_api([]) is None
    c = cr.Cluster(cluster_id='x', name='n', host='h', username='u', verify_tls=False, enabled=True, is_template=False,
                   hc_cluster_uuid=None, colour=None, last_seen_at=None, last_error=None, registered_at=None, updated_at=None)
    import os
    os.environ['TZ'] = 'Europe/Amsterdam'
    assert c.effective_timezone == 'Europe/Amsterdam'
    c.timezone = 'US/Eastern'
    assert c.effective_timezone == 'US/Eastern'
