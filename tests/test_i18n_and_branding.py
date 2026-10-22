"""Repository-level guards: translations stay in lockstep, branding rules hold, docs are consistent."""
import re
from pathlib import Path

import pytest

LANGS = ('en', 'nl', 'fr', 'de')
KEY_RE = re.compile(r"^\s*'([A-Za-z0-9_.]+)'\s*:", re.M)


def lang_keys(repo, code):
    text = (repo / 'dashboard' / 'static' / 'lang' / f'{code}.js').read_text(encoding='utf-8')
    return set(KEY_RE.findall(text))


def test_all_languages_have_identical_key_sets(repo):
    sets = {c: lang_keys(repo, c) for c in LANGS}
    base = sets['en']
    for c in LANGS[1:]:
        assert sets[c] - base == set(), f'{c}.js has keys missing from en.js'
        assert base - sets[c] == set(), f'{c}.js is missing keys present in en.js'


def test_every_data_i18n_key_in_dashboard_is_translated(repo):
    html = (repo / 'dashboard' / 'static' / 'index.html').read_text(encoding='utf-8')
    used = set(re.findall(r'data-i18n="([^"]+)"', html))
    assert used, 'no data-i18n attributes found'
    assert used - lang_keys(repo, 'en') == set()


def test_lang_files_are_valid_js_objects(repo):
    """Cheap syntax guard: balanced braces and every value line ends with a comma."""
    for c in LANGS:
        text = (repo / 'dashboard' / 'static' / 'lang' / f'{c}.js').read_text(encoding='utf-8')
        assert text.count('{') == text.count('}'), f'{c}.js unbalanced braces'
        for line in text.splitlines():
            s = line.strip()
            if KEY_RE.match(line) and not s.endswith(',') and not s.endswith('{'):
                pytest.fail(f'{c}.js: entry without trailing comma: {s[:60]}')


def test_community_notice_present_in_all_languages(repo):
    for c in LANGS:
        keys = lang_keys(repo, c)
        for k in ('community.title', 'community.body', 'community.issues_hint', 'community.issues_link', 'community.ack'):
            assert k in keys, f'{c}.js missing {k}'


# CHANGELOG.md is history and may legitimately name the old prefix
USER_FACING = [
    'README.md', 'CONTRIBUTING.md', 'SECURITY.md', 'docs/manual/08-installing-and-updating.md',
    'docs/phase4-data-sharing.md', '.env.template', 'docker-compose.yaml', 'install.py',
    'dashboard/static/index.html', 'dashboard/templates/login.html',
    'dashboard/static/lang/en.js', 'dashboard/static/lang/nl.js',
    'dashboard/static/lang/fr.js', 'dashboard/static/lang/de.js',
    'dashboard/reports/pdf_report.py', 'sentinel/reports/pdf_report.py',
]


@pytest.mark.parametrize('rel', USER_FACING)
def test_no_sc_prefix_in_user_facing_text(repo, rel):
    assert 'SC//' not in (repo / rel).read_text(encoding='utf-8'), f'{rel} still uses the SC// prefix'


def test_no_logo_image_assets(repo):
    img_dir = repo / 'dashboard' / 'static' / 'img'
    assert not img_dir.exists() or not any(img_dir.iterdir())
    for rel in ('dashboard/static/index.html', 'dashboard/templates/login.html'):
        text = (repo / rel).read_text(encoding='utf-8')
        assert '/static/img/' not in text, f'{rel} references an image asset'
        assert 'alt="Scale Computing"' not in text


def test_no_personal_contact_email(repo):
    for rel in ('README.md', 'LICENSE', 'NOTICE', 'CONTRIBUTING.md', 'SECURITY.md'):
        assert '@scalecomputing.com' not in (repo / rel).read_text(), f'{rel} contains a contact email'


def test_license_is_apache_2(repo):
    text = (repo / 'LICENSE').read_text()
    assert 'Apache License' in text and 'Version 2.0, January 2004' in text
    assert 'Copyright 2026 Scale Computing' in text
    assert 'All rights reserved' not in text
    assert (repo / 'NOTICE').exists()


def test_windows_installer_is_gone(repo):
    assert not (repo / 'installer' / 'windows').exists()
    assert not (repo / 'HyperCoreDeploy.spec').exists()
    readme = (repo / 'README.md').read_text()
    for word in ('HyperCoreDeploy', 'Deploy Wizard', 'installer/windows'):
        assert word not in readme


def test_lcars_experiment_is_gone(repo):
    static = repo / 'dashboard' / 'static'
    assert not (static / 'index2.html').exists() and not (static / 'index3.html').exists()
    assert '/lcars' not in (repo / 'dashboard' / 'app.py').read_text()


def test_readme_loop_count_matches_sentinel(repo):
    src = (repo / 'sentinel' / 'sentinel.py').read_text()
    loops = len(re.findall(r'threading\.Thread\(target=\w+_loop', src))
    readme = (repo / 'README.md').read_text()
    words = {1: 'one', 2: 'two', 3: 'three', 4: 'four', 5: 'five', 6: 'six'}
    assert f'runs {words[loops]} independent background loops' in readme


def test_compose_has_no_obsolete_version_key(repo):
    first = (repo / 'docker-compose.yaml').read_text().lstrip().splitlines()[0]
    assert not first.startswith('version:')


def test_requirements_are_pinned(repo):
    for sub in ('dashboard', 'Balancer', 'Collector', 'sentinel'):
        for line in (repo / sub / 'requirements.txt').read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#'):
                assert '==' in line, f'{sub}/requirements.txt: {line!r} is not pinned'


def test_dockerfile_fetches_compose_for_image_arch(repo):
    df = (repo / 'dashboard' / 'Dockerfile').read_text()
    assert 'docker-compose-linux-${ARCH}' in df
    assert 'docker-compose-linux-x86_64' not in df
    assert 'docker-compose version' in df, 'build must fail fast if the binary cannot run'
    assert 'COPY dashboard/versioning.py' in df
    assert 'COPY docs/manual/ manual/' in df, 'the image serves the manual at /manual'


def test_tests_dir_not_gitignored(repo):
    lines = [l.strip() for l in (repo / '.gitignore').read_text().splitlines()]
    assert 'tests/' not in lines
