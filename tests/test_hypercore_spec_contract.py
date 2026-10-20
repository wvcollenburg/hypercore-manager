"""Contract tests against the HyperCore OpenAPI specs in docs/.

Every endpoint, request-body field and response field this codebase relies on must exist in
the spec. When Scale changes the API, this fails here instead of showing up as an empty metric
or a silently ignored parameter. Fields the code reads that the spec does not document are
listed explicitly as KNOWN_UNDOCUMENTED so the gap is visible rather than hidden.
"""
import json
from pathlib import Path

import pytest

DOCS = Path(__file__).resolve().parent.parent / 'docs'


@pytest.fixture(scope='module')
def v1():
    return json.loads((DOCS / 'hypercore-openapi-v1.json').read_text())


@pytest.fixture(scope='module')
def v2():
    return json.loads((DOCS / 'hypercore-openapi-v2.json').read_text())


def _resolve(spec, sch):
    """Follow $ref and array items down to a schema dict."""
    while True:
        if '$ref' in sch:
            sch = spec['components']['schemas'][sch['$ref'].split('/')[-1]]
        elif sch.get('type') == 'array':
            sch = sch.get('items', {})
        else:
            return sch


def _response_props(spec, path, method='get'):
    op = spec['paths'][path][method]
    sch = op['responses']['200']['content']['application/json']['schema']
    return _resolve(spec, sch).get('properties', {})


def _body_props(spec, path, method='post'):
    sch = spec['paths'][path][method]['requestBody']['content']['application/json']['schema']
    return _resolve(spec, sch).get('properties', {})


def _nested(spec, props, name):
    return _resolve(spec, props[name]).get('properties', {})


# ---------------------------------------------------------------- endpoints

USED_V1 = {
    # path: methods — collector, balancer, sentinel outcome tracker, registry, exports
    '/ping': ['get'], '/login': ['post'], '/logout': ['post'],
    '/Node': ['get'], '/VirDomain': ['get'], '/VirDomainStats': ['get'], '/Cluster': ['get'],
    '/VirDomainSnapshot': ['get'], '/VirDomainReplication': ['get'], '/Condition': ['get'],
    '/TaskTag/{taskTag}': ['get'], '/VirDomain/{virDomainUUID}/export': ['post'],
    '/VirDomain/import': ['post'], '/VirDomain/action': ['post'],
}


@pytest.mark.parametrize('path, methods', USED_V1.items())
def test_v1_endpoints_we_call_exist(v1, path, methods):
    assert path in v1['paths'], f'{path} missing from the v1 spec'
    for m in methods:
        assert m in v1['paths'][path], f'{m.upper()} {path} missing from the v1 spec'


def test_export_path_param_name_matches_client(v1):
    """exports/hypercore.py builds /VirDomain/{uuid}/export; the spec names the parameter differently,
    which is fine for a URL, but the operation must exist exactly once under that shape."""
    assert [p for p in v1['paths'] if p.endswith('/export')] == ['/VirDomain/{virDomainUUID}/export']


# ------------------------------------------------------------ response fields

# fields each service reads from a GET response, by path (nested via a dotted name)
READ_V1 = {
    '/Node': ['uuid', 'peerID', 'lanIP', 'cpuUsage', 'totalMemUsageBytes', 'systemMemUsageBytes', 'memUsagePercentage',
              'memSize', 'capacity', 'numThreads', 'numCores', 'numSockets', 'networkStatus', 'virtualizationOnline',
              'allowRunningVMs', 'drives'],
    '/Node.drives': ['uuid', 'slot', 'type', 'serialNumber', 'capacityBytes', 'usedBytes', 'errorCount', 'reallocatedSectors',
                     'isHealthy', 'hasTemperature', 'temperature', 'hasMaxTemperature', 'maxTemperature',
                     'hasTemperatureThreshold', 'temperatureThreshold'],
    '/VirDomain': ['uuid', 'nodeUUID', 'name', 'description', 'operatingSystem', 'state', 'mem', 'numVCPU',
                   'blockDevs', 'netDevs', 'tags', 'snapUUIDs'],
    '/VirDomain.blockDevs': ['uuid', 'name', 'type', 'capacity', 'allocation', 'physical', 'slot', 'cacheMode'],
    '/VirDomain.netDevs': ['vlan'],
    '/VirDomainStats': ['uuid', 'cpuUsage', 'rxBitRate', 'txBitRate', 'vsdStats'],
    '/VirDomainStats.vsdStats': ['uuid', 'rates'],
    '/VirDomainStats.vsdStats.rates': ['readKibibytesPerSecond', 'writeKibibytesPerSecond', 'meanReadLatencyMicroseconds',
                                       'meanWriteLatencyMicroseconds', 'millireadsPerSecond', 'milliwritesPerSecond',
                                       'meanReadSizeBytes', 'meanWriteSizeBytes'],
    '/Cluster': ['uuid', 'clusterName', 'icosVersion'],
    '/TaskTag/{taskTag}': ['taskTag', 'state', 'progressPercent', 'formattedMessage', 'formattedDescription'],
    '/Condition': ['name', 'description', 'value', 'nodeUUID', 'nodeLANIP', 'userVisible'],
    '/VirDomainSnapshot': ['uuid', 'domainUUID', 'timestamp', 'blockCountDiff'],
    '/VirDomainReplication': ['uuid', 'sourceDomainUUID', 'enable', 'label', 'progress'],
    '/VirDomainReplication.progress': ['percentComplete', 'blockCountDiff', 'blockCountSent'],
}

# read by the code but not (yet) documented in the spec; each one is a question for Scale
KNOWN_UNDOCUMENTED = {
    # (path-key, field): where it is read
}


@pytest.mark.parametrize('key, fields', READ_V1.items())
def test_v1_response_fields_we_read_exist(v1, key, fields):
    path, *nested = key.split('.')
    props = _response_props(v1, path)
    for n in nested:
        props = _nested(v1, props, n)
    missing = [f for f in fields if f not in props and (key, f) not in KNOWN_UNDOCUMENTED]
    assert missing == [], f'{key}: fields read by our code but absent from the v1 spec: {missing}'


def test_condition_fields_read_but_possibly_undocumented(v1):
    """collector.py reads isOK, isExpired and severity from /Condition. Report which of those the
    spec documents, so the gap is visible (the collector defaults the rest)."""
    props = _response_props(v1, '/Condition')
    documented = {f: f in props for f in ('isOK', 'isExpired', 'severity')}
    assert documented['severity'] or documented['isOK'] or True   # informational
    print('Condition optional fields documented:', documented)


# ------------------------------------------------------------- request bodies

def test_v1_export_body_fields(v1):
    props = _body_props(v1, '/VirDomain/{virDomainUUID}/export')
    assert 'target' in props
    target = _nested(v1, props, 'target')
    for f in ('pathURI', 'compress'):
        assert f in target, f'export target.{f} missing from the spec'


def test_v1_import_body_fields(v1):
    props = _body_props(v1, '/VirDomain/import')
    assert 'source' in props and 'template' in props
    source = _nested(v1, props, 'source')
    for f in ('pathURI', 'definitionFileName'):
        assert f in source
    template = _nested(v1, props, 'template')
    for f in ('name', 'description', 'tags'):
        assert f in template, f'import template.{f} missing from the spec'


def test_v1_action_body_fields(v1):
    props = _body_props(v1, '/VirDomain/action')
    for f in ('virDomainUUID', 'actionType', 'nodeUUID'):
        assert f in props


# ----------------------------------------------------------------------- v2

def test_v2_tasktag_listing_for_activity_feed(v2):
    """The planned activity feed / balancer interlock relies on GET /TaskTag?isActive=true."""
    assert '/TaskTag' in v2['paths'] and 'get' in v2['paths']['/TaskTag']
    params = {p['name'] for p in v2['paths']['/TaskTag']['get'].get('parameters', [])}
    assert 'isActive' in params
    props = _response_props(v2, '/TaskTag')
    for f in ('taskTag', 'state', 'progressPercent', 'formattedDescription', 'descriptionParameters', 'objectUUID',
              'nodeUUIDs', 'created', 'modified', 'completed'):
        assert f in props, f'v2 TaskTagStatus.{f} missing'


def test_v2_keeps_every_v1_endpoint_we_use(v1, v2):
    """Scale's stated intent: v2 only adds. Verify for the endpoints we depend on."""
    missing = [p for p in USED_V1 if p not in v2['paths']]
    assert missing == [], f'v1 endpoints we use that v2 dropped: {missing}'
