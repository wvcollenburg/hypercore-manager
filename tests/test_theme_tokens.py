"""Every colour of the dashboard comes from a theme token (dashboard/static/theme.css).

A colour written straight into a page, a script or a template would ignore the theme: it stays the same in light
and dark, and a future theme would show it as a stray. This test fails on any colour literal (#rgb, #rrggbb,
rgb(), rgba(), hsl()) outside theme.css.

Allowed, on purpose:
  - a line carrying the marker `token-ok`: a colour that is data rather than theme (a user-picked folder colour,
    a report's own accent, a value computed from a token);
  - data: URIs (the favicon);
  - the vendored libraries and the translation files, which are not ours or hold no styling.
The mails sent by the dashboard are not covered: mail clients ignore the page's stylesheet, so they keep their
own inline colours.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DASH = ROOT / 'dashboard'
COLOUR = re.compile(r'(?<![\w&])#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3,4})\b(?![\w-])|\b(?:rgba?|hsla?)\(')
SKIP_DIRS = {'vendor', 'lang', '__pycache__'}


def _files():
    for p in sorted((DASH / 'static').rglob('*')):
        if p.is_file() and p.suffix in ('.html', '.js', '.css') and not (SKIP_DIRS & set(p.parts)) and p.name != 'theme.css':
            yield p
    for p in sorted((DASH / 'templates').glob('*.html')):
        yield p
    yield DASH / 'manual.py'


def _offences(path: Path):
    out = []
    for n, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        if 'token-ok' in line:
            continue
        clean = re.sub(r'data:[^"\')\s]+', '', line)          # data: URIs (favicon)
        clean = re.sub(r'&#x?[0-9a-fA-F]+;', '', clean)        # HTML entities like &#9660;
        for m in COLOUR.finditer(clean):
            out.append(f'{path.relative_to(ROOT)}:{n}: {m.group(0)} in  {line.strip()[:140]}')
    return out


def test_no_colour_literals_outside_the_theme():
    offences = [o for p in _files() for o in _offences(p)]
    assert not offences, (f'{len(offences)} colour literal(s) outside dashboard/static/theme.css; use a token '
                          '(or mark a data colour with token-ok):\n' + '\n'.join(offences[:80]))


def test_every_token_has_a_dark_counterpart():
    """A colour defined only in the light block would stay light in dark mode."""
    css = (DASH / 'static' / 'theme.css').read_text(encoding='utf-8')
    light = css[css.index(':root {'):css.index(':root[data-theme="dark"]')]
    dark = css[css.index(':root[data-theme="dark"]'):]
    names = lambda block: set(re.findall(r'(--[\w-]+)\s*:\s*([^;]+);', block))
    colour_tokens = {n for n, v in names(light) if re.search(r'#[0-9a-fA-F]{3,8}\b|color-mix', v)}
    # tokens defined from other tokens follow them; only literal colours need their own dark value
    derived_ok = {'--shadow-sm', '--shadow-md', '--shadow-lg', '--focus-ring'}
    missing = sorted(colour_tokens - {n for n, _ in names(dark)} - derived_ok)
    assert not missing, f'light-only colour tokens (give them a value in the dark block): {missing}'
