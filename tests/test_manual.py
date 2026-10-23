"""dashboard/manual.py — the manual served by the dashboard renders every chapter and keeps its links."""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'dashboard'))
import manual  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / 'docs' / 'manual'


def test_chapters_are_discovered_in_order_with_the_index_first():
    ch = manual.chapters(DOCS)
    assert ch[0]['slug'] == '' and 'manual' in ch[0]['title'].lower()
    slugs = [c['slug'] for c in ch[1:]]
    assert slugs == sorted(slugs) and slugs[0].startswith('01-') and len(slugs) >= 7
    assert all(c['title'] for c in ch)


def test_links_point_at_dashboard_routes_and_images_at_the_image_route():
    html = ('<a href="01-getting-around.md">x</a> <a href="05-backup.md#plans">y</a> <a href="../administration.md">z</a> '
            '<a href="manual/08-installing-and-updating.md#updating">d</a> <a href="https://example.com/a.md">ext</a> <img src="img/login.png">')
    out = manual.rewrite_links(html)
    assert 'href="/manual/01-getting-around"' in out and 'href="/manual/05-backup#plans"' in out
    assert 'href="/manual/administration"' in out
    assert 'href="/manual/08-installing-and-updating#updating"' in out, 'a guide links a chapter by its docs/ relative path'
    assert 'href="https://example.com/a.md"' in out, 'external links are left alone'
    assert 'src="/manual/img/login.png"' in out


def test_resolve_accepts_chapters_index_and_guides_only(tmp_path):
    (tmp_path / 'README.md').write_text('# idx'); (tmp_path / '01-a.md').write_text('# a')
    (tmp_path / 'extra').mkdir(); (tmp_path / 'extra' / 'administration.md').write_text('# d')
    assert manual.resolve(tmp_path, '').name == 'README.md' and manual.resolve(tmp_path, '01-a').name == '01-a.md'
    assert manual.resolve(tmp_path, 'administration').name == 'administration.md' and manual.resolve(tmp_path, 'nope-guide') is None
    assert manual.resolve(tmp_path, 'deploy') is None, 'the deployment guide was folded into the installing chapter'
    assert manual.resolve(tmp_path, '../etc/passwd') is None and manual.resolve(tmp_path, 'nope') is None


@pytest.mark.parametrize('chapter', sorted(p.name for p in DOCS.glob('*.md')))
def test_every_chapter_renders_and_its_images_exist(chapter):
    pytest.importorskip('markdown')
    text = (DOCS / chapter).read_text(encoding='utf-8')
    out = manual.render(text)
    assert '<h1' in out and '.md"' not in out, 'no raw .md link survives'
    import re
    for img in re.findall(r'src="/manual/img/([^"]+)"', out):
        assert (DOCS / 'img' / img).exists(), f'{chapter} references a missing screenshot {img}'
    page = manual.page('t', out, manual.chapters(DOCS), chapter[:-3] if chapter != 'README.md' else '', '1.0')
    assert '<nav>' in page and 'back to the dashboard' in page
