"""
manual.py — the user manual served by the dashboard at /manual.

The chapters are the Markdown files under docs/manual (copied into the image as /app/manual);
the guides they link (administration, data sharing) sit under manual/extra. They are
rendered server-side with the markdown package so a dark-site installation needs nothing from
the internet, and wrapped in a page that follows the dashboard's dark theme with a chapter list.
Links between chapters keep working: `01-getting-around.md` becomes `/manual/01-getting-around`,
`../administration.md` becomes `/manual/administration`, a guide's `manual/08-installing-and-updating.md`
becomes that chapter, and images stay under /manual/img/.
"""
from __future__ import annotations

import html
import os
import re
from pathlib import Path

EXTRA = {'administration': 'administration.md', 'data-sharing': 'phase4-data-sharing.md'}
_GUIDES = {'README.md': '', 'administration.md': 'administration', 'phase4-data-sharing.md': 'data-sharing'}


def chapters(root: str | os.PathLike) -> list[dict]:
    """[{slug, title, file}] in file order; README.md is the index and comes first."""
    out = []
    for p in sorted(Path(root).glob('*.md')):
        slug = '' if p.name == 'README.md' else p.stem
        out.append({'slug': slug, 'title': _title(p), 'file': str(p)})
    out.sort(key=lambda c: (c['slug'] != '', c['slug']))
    return out


def _title(path: Path) -> str:
    for line in path.read_text(encoding='utf-8').splitlines():
        if line.startswith('# '):
            return line[2:].strip()
    return path.stem


def rewrite_links(body: str) -> str:
    """Chapter and guide links in rendered HTML point at the dashboard's routes."""
    def fix(m):
        href = m.group(1)
        # a chapter or guide, wherever the linking file sits (docs/manual, docs or the repository root)
        if re.fullmatch(r'(\.\./|manual/|docs/manual/|docs/)*[0-9A-Za-z_-]+\.md(#.*)?', href):
            path, _, frag = href.partition('#')
            name = path.rsplit('/', 1)[-1]
            slug = _GUIDES.get(name, name[:-3])
            return f'href="/manual/{slug}{"#" + frag if frag else ""}"'
        return m.group(0)
    body = re.sub(r'href="([^"]+)"', fix, body)
    return body.replace('src="img/', 'src="/manual/img/')


def render(md_text: str) -> str:
    import markdown
    body = markdown.markdown(md_text, extensions=['tables', 'fenced_code', 'toc'], output_format='html5')
    return rewrite_links(body)


def resolve(root: str | os.PathLike, slug: str) -> Path | None:
    """The Markdown file for a slug: a chapter, the index, or one of the linked guides."""
    root = Path(root)
    if slug in ('', 'index', 'README'):
        return root / 'README.md'
    if slug in EXTRA:
        p = root / 'extra' / EXTRA[slug]
        return p if p.exists() else None
    if re.fullmatch(r'[0-9A-Za-z_-]+', slug):
        p = root / f'{slug}.md'
        return p if p.exists() else None
    return None


def page(title: str, body_html: str, nav: list[dict], current: str, version: str = '') -> str:
    items = ''.join(f'<a class="{"cur" if c["slug"] == current else ""}" href="/manual/{c["slug"]}">{html.escape(c["title"])}</a>' for c in nav)
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} · HyperCore Manager manual</title>
<style>
:root {{ --bg: #0b1220; --card: #111a2b; --line: #1f2a3f; --text: #e5e9f0; --muted: #94a3b8; --accent: #38bdf8; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--bg); color: var(--text); font: 15px/1.6 -apple-system, "Segoe UI", Roboto, sans-serif; }}
header {{ display: flex; align-items: center; gap: 16px; padding: 12px 24px; border-bottom: 1px solid var(--line); background: var(--card); position: sticky; top: 0; }}
header .brand {{ font-family: "JetBrains Mono", ui-monospace, monospace; font-weight: 700; letter-spacing: .02em; }}
header .brand span {{ color: var(--accent); }}
header a {{ color: var(--muted); text-decoration: none; font-size: 13px; }}
.wrap {{ display: grid; grid-template-columns: 260px 1fr; gap: 32px; max-width: 1200px; margin: 0 auto; padding: 24px; }}
nav {{ position: sticky; top: 72px; align-self: start; display: flex; flex-direction: column; gap: 4px; }}
nav a {{ color: var(--muted); text-decoration: none; padding: 6px 10px; border-radius: 6px; font-size: 14px; }}
nav a.cur, nav a:hover {{ background: var(--card); color: var(--text); }}
main {{ min-width: 0; }}
main img {{ max-width: 100%; border: 1px solid var(--line); border-radius: 8px; margin: 8px 0 16px; }}
main h1, main h2, main h3 {{ line-height: 1.25; }}
main h1 {{ font-size: 28px; margin-top: 0; }}
main h2 {{ font-size: 20px; margin-top: 32px; border-top: 1px solid var(--line); padding-top: 16px; }}
main a {{ color: var(--accent); }}
main code {{ background: var(--card); padding: 1px 5px; border-radius: 4px; font-size: 13px; }}
main pre {{ background: var(--card); padding: 12px 14px; border-radius: 8px; overflow: auto; }}
main pre code {{ padding: 0; background: none; }}
main table {{ border-collapse: collapse; margin: 12px 0; width: 100%; font-size: 14px; }}
main th, main td {{ border: 1px solid var(--line); padding: 6px 10px; text-align: left; vertical-align: top; }}
main th {{ background: var(--card); color: var(--muted); font-weight: 600; }}
main blockquote {{ border-left: 3px solid var(--accent); margin: 0; padding: 4px 14px; color: var(--muted); }}
@media (max-width: 860px) {{ .wrap {{ grid-template-columns: 1fr; }} nav {{ position: static; flex-direction: row; flex-wrap: wrap; }} }}
</style></head>
<body><header><div class="brand">HyperCore <span>Manager</span> · manual</div><a href="/">← back to the dashboard</a>{f'<a style="margin-left:auto">{html.escape(version)}</a>' if version else ''}</header>
<div class="wrap"><nav>{items}<a href="/manual/administration">Administration guide</a></nav><main>{body_html}</main></div>
</body></html>'''
