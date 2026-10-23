"""installer/core.py — .env schema, parsing, validation and easy-mode autogen."""
from pathlib import Path

import pytest

from installer import core


@pytest.fixture(scope='module')
def schema():
    return core.build_schema()


def _all_fields(schema):
    return [f for s in schema for f in s.fields]


def test_schema_matches_env_template(schema, env_template):
    """Every variable in .env.template is described by the installer, and vice versa."""
    schema_keys = {f.key for f in _all_fields(schema)}
    template_keys = set(env_template)
    assert template_keys - schema_keys == set(), 'in .env.template but unknown to installer'
    assert schema_keys - template_keys == set(), 'in installer schema but missing from .env.template'


def test_schema_defaults_match_template_defaults(schema, env_template):
    mismatches = {
        f.key: (f.default, env_template[f.key])
        for f in _all_fields(schema)
        if f.key in env_template and f.default != env_template[f.key]
    }
    assert mismatches == {}


def test_schema_keys_unique(schema):
    keys = [f.key for f in _all_fields(schema)]
    assert len(keys) == len(set(keys))


def test_parse_env_handles_quotes_comments_and_blanks(tmp_path):
    p = tmp_path / '.env'
    p.write_text('# comment\n\nA=1\nB="two words"\nC=\'single\'\nD=has=equals\nnoequals\n')
    assert core.parse_env(p) == {'A': '1', 'B': 'two words', 'C': 'single', 'D': 'has=equals'}


def test_parse_env_missing_file(tmp_path):
    assert core.parse_env(tmp_path / 'nope') == {}


def test_render_then_parse_roundtrip(schema, tmp_path):
    values = core.seed_default_values(schema)
    values['TZ'] = 'Europe/Amsterdam'
    text = core.render_env(schema, values, {core.PROFILE_CORE, core.PROFILE_SENTINEL, core.PROFILE_BACKUP})
    p = tmp_path / '.env'
    p.write_text(text)
    back = core.parse_env(p)
    assert back['TZ'] == 'Europe/Amsterdam'
    for f in _all_fields(schema):
        assert f.key in back


@pytest.mark.parametrize('ftype, good, bad', [
    ('int',   '5',     'five'),
    ('float', '2.5',   'x'),
    ('bool',  'true',  'yes'),
    ('bool',  'FALSE', '0'),
])
def test_validate_value_types(ftype, good, bad):
    f = core.Field(key='K', label='k', type=ftype)
    assert core.validate_value(f, good) is None
    assert core.validate_value(f, bad) is not None


def test_validate_choice():
    f = core.Field(key='K', label='k', type='choice', choices=['mean', 'p95', 'max'])
    assert core.validate_value(f, 'p95') is None
    assert core.validate_value(f, 'median')


def test_validation_flags_placeholders(schema):
    values = core.seed_default_values(schema)
    errors = core.collect_validation_errors(schema, values, {core.PROFILE_CORE, core.PROFILE_SENTINEL})
    joined = '\n'.join(errors)
    for key in ('HCM_SECRET_KEY', 'SC_DASHBOARD_PASSWORD', 'INFLUX_TOKEN', 'SENTINEL_PG_PASSWORD'):
        assert key in joined, f'{key} placeholder not reported'
    assert 'SC_HOST' not in joined, 'cluster credentials are no longer part of .env'


def test_easy_autogen_fills_secrets_but_not_operator_keys(schema):
    values = core.seed_default_values(schema)
    before = dict(values)
    core.apply_easy_autogen(schema, values, core.EASY_PROFILES)
    for key in core.EASY_REQUIRED_KEYS:
        assert values[key] == before[key], f'{key} is operator-supplied and must not be generated'
    for key in ('INFLUX_TOKEN', 'DOCKER_INFLUXDB_INIT_PASSWORD', 'SENTINEL_PG_PASSWORD', 'SC_DASHBOARD_SECRET_KEY', 'HCM_SECRET_KEY'):
        assert values[key] not in core.PLACEHOLDERS
        assert len(values[key]) >= 24


def test_generated_passwords_survive_a_compose_env_file():
    """Docker Compose substitutes $NAME inside .env values; a generated password containing '$'
    reached the containers with a piece cut out (seen on the online install)."""
    for _ in range(300):
        pw = core.gen_password(24)
        assert '$' not in pw and pw[0] not in '\'"' and not any(c.isspace() for c in pw)
    f = core.Field(key='P', label='p', type='password')
    assert core.validate_value(f, 'abc$KmGjxim7TD7zLA') is not None
    assert core.validate_value(f, '"quoted') is not None and core.validate_value(f, 'has space') is not None
    assert core.validate_value(f, 'Fine-Pass-1!') is None


def test_gen_password_strength():
    for _ in range(20):
        pw = core.gen_password(24)
        assert len(pw) == 24
        assert not set(pw) & set('0O1lI')
        assert any(c.islower() for c in pw) and any(c.isupper() for c in pw)
        assert any(c.isdigit() for c in pw) and any(not c.isalnum() for c in pw)


def test_gen_token_hex():
    t = core.gen_token(32)
    assert len(t) == 64
    int(t, 16)


@pytest.mark.parametrize('inp, expected', [
    ('172.16.0.241',                   'https://172.16.0.241/rest/v1'),
    ('https://172.16.0.241/rest/v1',   'https://172.16.0.241/rest/v1'),
    ('https://172.16.0.241',           'https://172.16.0.241/rest/v1'),
    ('cluster.example.com',            'https://cluster.example.com/rest/v1'),
])
def test_normalize_sc_host(inp, expected):
    assert core.normalize_sc_host(inp) == expected


def test_extract_host_ip_roundtrip():
    assert core.extract_host_ip('https://172.16.0.241/rest/v1') == '172.16.0.241'
    assert core.extract_host_ip(core.normalize_sc_host('10.1.2.3')) == '10.1.2.3'


@pytest.mark.parametrize('s, ok', [
    ('10.0.0.1', True), ('256.0.0.1', False), ('host.example', True), ('', False),
])
def test_looks_like_host(s, ok):
    assert core.looks_like_host(s) is ok


def test_cluster_credentials_are_not_in_the_installer(schema):
    keys = {f.key for s in schema for f in s.fields}
    assert not keys & {'SC_HOST', 'SC_USERNAME', 'SC_PASSWORD', 'SC_VERIFY_SSL'}
    assert 'HCM_SECRET_KEY' in keys and 'TZ' in keys


def test_env_template_installer_and_compose_agree():
    """Every variable in .env.template has an installer field and is read by the compose file, and
    the compose file reads nothing the template lacks (apart from documented fallbacks)."""
    import re
    root = Path(__file__).resolve().parent.parent
    template = set(re.findall(r'^([A-Z][A-Z0-9_]+)=', (root / '.env.template').read_text(), re.M))
    compose = set(re.findall(r'\$\{([A-Z][A-Z0-9_]+)', (root / 'docker-compose.yaml').read_text()))
    schema = {f.key for s in core.build_schema() for f in s.fields}
    fallbacks = {'EXPORTS_API_TOKEN', 'EXPORTS_DEFAULT_RETENTION', 'EXPORTS_DEFAULT_RUN_TIME', 'EXPORTS_POLL_INTERVAL_SECONDS',
                 'EXPORTS_TASK_TIMEOUT_HOURS',        # former names, honoured for one release
                 'PROXY_API_URL'}                      # has a default in the compose file; only set for an external proxy
    assert template == schema, (sorted(template - schema), sorted(schema - template))
    assert template <= compose, sorted(template - compose)
    assert compose - template <= fallbacks, sorted(compose - template - fallbacks)



@pytest.mark.parametrize('value, ok', [
    ('UTC', True), ('Europe/Amsterdam', True), ('US/Eastern', True), ('America/Indiana/Indianapolis', True),
    ('', False), ('CET+1', False), ('Amsterdam', False), ('europe/amsterdam', False), ('Europe/Amsterdam ', True),
])
def test_timezone_validation(value, ok):
    f = core.Field(key='TZ', label='tz', type='timezone')
    assert (core.validate_value(f, value) is None) == ok, value


def test_timezone_validation_hints_at_the_right_case():
    assert 'Europe/Amsterdam' in core.validate_timezone('europe/amsterdam')


def test_easy_mode_covers_every_module_and_asks_for_the_time_zone(schema):
    """Every module starts with the stack, so easy mode must generate the backup and proxy tokens
    too (an empty token disables that API's authentication); the time zone is operator input."""
    assert core.EASY_PROFILES == {core.PROFILE_CORE, core.PROFILE_SENTINEL, core.PROFILE_BACKUP}
    assert 'TZ' in core.EASY_REQUIRED_KEYS
    values = core.seed_default_values(schema)
    core.apply_easy_autogen(schema, values, core.EASY_PROFILES)
    for key in ('BACKUP_API_TOKEN', 'PROXY_API_TOKEN'):
        assert values[key] not in core.PLACEHOLDERS and len(values[key]) >= 32, key
    src = (Path(__file__).resolve().parents[1] / 'install.py').read_text()
    assert '"backup",   "Backup",' in src and '"Backup (optional)"' not in src, 'every module is on by default in advanced mode'
    assert '"key": "TZ",' in src, 'easy mode asks for the time zone'


def test_timezone_picker_lists_utc_then_regions_then_cities():
    regions = core.timezone_regions()
    assert regions[0] == 'UTC' and 'Europe' in regions and 'US' in regions and 'Etc' not in regions
    cities = core.timezone_cities('Europe')
    assert 'Amsterdam' in cities and cities == sorted(cities) and all('/' not in c or c.count('/') >= 1 for c in cities)
    assert 'Indiana/Indianapolis' in core.timezone_cities('America'), 'sub-regions keep their path after the region'
    assert 'Eastern' in core.timezone_cities('US')
    assert core.validate_timezone('Europe/' + cities[0]) is None, 'everything the picker offers validates'
