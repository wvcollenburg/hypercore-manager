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


@pytest.mark.parametrize('code', LANGS)
def test_every_translation_is_a_well_formed_js_string(repo, code):
    """A quote inside a same-quoted value breaks the whole file in the browser and every screen in
    that language falls back to raw keys (fr.js had 'J'ai compris' before 1.0)."""
    import re
    bad = []
    for i, line in enumerate((repo / 'dashboard' / 'static' / 'lang' / f'{code}.js').read_text(encoding='utf-8').splitlines(), 1):
        m = re.match(r"^\s*'([^']+)':\s*(.*),\s*$", line)
        if not m:
            continue
        v = m.group(2)
        ok = (v.startswith('"') and v.endswith('"') and not re.search(r'(?<!\\)"', v[1:-1])) or \
             (v.startswith("'") and v.endswith("'") and not re.search(r"(?<!\\)'", v[1:-1]))
        if not ok:
            bad.append(f'{code}.js:{i}: {line.strip()[:100]}')
    assert not bad, '\n'.join(bad)


def test_community_notice_present_in_all_languages(repo):
    for c in LANGS:
        keys = lang_keys(repo, c)
        for k in ('community.title', 'community.body', 'community.issues_hint', 'community.issues_link', 'community.ack'):
            assert k in keys, f'{c}.js missing {k}'


# docs/CHANGELOG.md is history and may legitimately name the old prefix
USER_FACING = [
    'README.md', '.github/CONTRIBUTING.md', '.github/SECURITY.md', 'docs/manual/08-installing-and-updating.md',
    '.env.template', 'docker-compose.yaml', 'install.py',
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
    for rel in ('README.md', 'LICENSE', 'NOTICE', '.github/CONTRIBUTING.md', '.github/SECURITY.md'):
        assert '@scalecomputing.com' not in (repo / rel).read_text(), f'{rel} contains a contact email'


def test_license_is_apache_2(repo):
    text = (repo / 'LICENSE').read_text()
    assert 'Apache License' in text and 'Version 2.0, January 2004' in text
    assert 'Copyright 2026 William David van Collenburg' in text
    for rel in ('LICENSE', 'NOTICE', 'README.md'):
        assert 'Copyright 2026 William David van Collenburg' in (repo / rel).read_text(), rel
        assert 'Copyright 2026 Scale Computing' not in (repo / rel).read_text(), rel
        assert 'Copyright (c) 2026 Scale Computing' not in (repo / rel).read_text(), rel
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


def test_root_holds_only_readme_license_and_notice(repo):
    """Every other document lives under docs/ or .github/."""
    import subprocess
    try:
        r = subprocess.run(['git', '-C', str(repo), 'ls-files'], capture_output=True, text=True)
    except FileNotFoundError:
        pytest.skip('git is not installed')
    if r.returncode != 0:
        pytest.skip('not a git checkout')
    tracked = r.stdout.splitlines()
    root_docs = sorted(f for f in tracked if '/' not in f and (f.endswith('.md') or f in ('LICENSE', 'NOTICE')))
    assert root_docs == ['LICENSE', 'NOTICE', 'README.md'], root_docs
    assert (repo / '.github' / 'CONTRIBUTING.md').exists() and (repo / '.github' / 'SECURITY.md').exists()
    assert (repo / 'docs' / 'CHANGELOG.md').exists()


def _lang_values(repo, code):
    text = (repo / 'dashboard' / 'static' / 'lang' / f'{code}.js').read_text(encoding='utf-8')
    return dict(re.findall(r"^\s*'([A-Za-z0-9_.]+)'\s*:\s*(.*),\s*$", text, re.M))


def test_every_tunable_has_a_translated_label(repo):
    """The Balancer and Foresight setting labels came from CTRL_GROUPS / the Sentinel API in English, and the
    toggles said ON/OFF, in every language."""
    html = (repo / 'dashboard' / 'static' / 'index.html').read_text(encoding='utf-8')
    groups = html[html.index('const CTRL_GROUPS'):html.index('// reverse map: key')]
    sent = html[html.index('const SENT_INFO'):html.index('const SENT_GROUPS')]
    keys = set(re.findall(r"\{ key: '([A-Z0-9_]+)'", groups)) | set(re.findall(r"\n  ([A-Z0-9_]+): \{", sent))
    assert len(keys) >= 40
    for code in LANGS:
        missing = {k for k in keys if f'tun.{k}' not in lang_keys(repo, code)}
        assert not missing, f'{code}.js lacks tun.<KEY> for {sorted(missing)}'
    assert "? 'ON' : 'OFF'" not in html


@pytest.mark.parametrize('code', ('nl', 'fr', 'de'))
def test_setting_help_is_translated(repo, code):
    """All 123 info.<KEY>.what/benefits/careful values were the English text in nl, fr and de."""
    en, other = _lang_values(repo, 'en'), _lang_values(repo, code)
    info = [k for k in en if re.match(r'info\.[A-Z0-9_]+\.(what|benefits|careful)$', k)]
    assert len(info) >= 120
    same = [k for k in info if other.get(k) == en[k]]
    assert not same, f'{code}.js still has English help for {same[:5]}'


@pytest.mark.parametrize('rel', ('dashboard/static/index.html', 'dashboard/static/access.js', 'dashboard/static/mail.js'))
def test_every_key_the_frontend_asks_for_exists(repo, rel):
    """t('key') and data-i18n-* name keys of en.js (the parity test carries them to nl, fr and de); a key that is
    missing shows as the raw key."""
    src = (repo / rel).read_text(encoding='utf-8')
    used = set(re.findall(r"\bt\(\s*'([a-zA-Z][\w.]*)'", src))
    used |= set(re.findall(r'data-i18n(?:-title|-placeholder|-aria-label)?="([^"$]+)"', src))
    used = {k for k in used if not k.endswith(('.', '_'))}       # t('ve.st_' + status): a prefix
    missing = used - lang_keys(repo, 'en')
    assert not missing, f'{rel} asks for keys en.js lacks: {sorted(missing)}'


def test_no_fixed_english_texts_or_date_locale(repo):
    """Time ranges, the update dialog, chart legends, the forecast log, 'Connecting...', '3m ago' and every date
    were English (dates in a fixed en-GB) whatever the interface language."""
    html = (repo / 'dashboard' / 'static' / 'index.html').read_text(encoding='utf-8')
    for label in ('1 day', '7 days', '1 month', '1 year'):
        for m in re.finditer(rf'>{label}</button>', html):
            tag = html[html.rfind('<button', 0, m.start()):m.start()]
            assert 'data-i18n=' in tag, f'time range "{label}" without data-i18n'
    for text in ("label: 'Total IOPS'", "'just now'", 'No Oracle runs in the selected window', "'⟳ Checking...'",
                 "'✓ Up to date'", "<th>Queued at</th>", "desc:'Physical node CPU & RAM'", "`Error: ${e.message}`"):
        assert text not in html, f'English left in index.html: {text}'
    assert 'id="statusText" data-i18n="header.connecting"' in html
    assert html.count("'en-GB'") == 3, 'dates follow _hcmLocale(); en-GB only as its English default (twice) and in zone arithmetic'
    for rel in ('access.js', 'mail.js'):
        src = (repo / 'dashboard' / 'static' / rel).read_text(encoding='utf-8')
        assert '.toLocaleString()' not in src, f'{rel}: give toLocaleString the interface locale'
