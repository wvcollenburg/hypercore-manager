"""
manual.py — the user manual served by the dashboard at /manual.

The chapters are the Markdown files under docs/manual (copied into the image as /app/manual);
the guide it links (administration) sits under manual/extra. They are
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

EXTRA = {'administration': 'administration.md'}
_GUIDES = {'README.md': '', 'administration.md': 'administration'}


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
<link rel="stylesheet" href="/static/vendor/fonts.css">
<link rel="stylesheet" href="/static/theme.css">
<script src="/static/theme.js"></script>
<link rel="icon" href="/static/favicon.svg" type="image/svg+xml">
<style>
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--bg); color: var(--ink); font: 15px/1.6 var(--font-ui); }}
header {{ display: flex; align-items: center; gap: 16px; padding: 0 24px; height: 52px; background: var(--bar); color: var(--bar-ink); position: sticky; top: 0; z-index: 5; }}
header .brand {{ display: flex; align-items: center; gap: 9px; font-weight: 600; }}
header .brand::before {{ content: 'H'; width: 22px; height: 22px; border-radius: 5px; background: var(--accent); color: var(--on-accent); display: grid; place-items: center; font-size: 12px; }}
header .brand span {{ color: var(--bar-ink-2); font-weight: 400; }}
header a {{ color: var(--bar-ink-2); text-decoration: none; font-size: 13px; }}
header a:hover {{ color: var(--bar-ink); }}
.wrap {{ display: grid; grid-template-columns: 260px 1fr; gap: 32px; max-width: 1200px; margin: 0 auto; padding: 24px; }}
nav {{ position: sticky; top: 76px; align-self: start; display: flex; flex-direction: column; gap: 2px; }}
nav a {{ color: var(--ink-2); text-decoration: none; padding: 6px 10px; border-radius: 6px; font-size: 14px; }}
nav a:hover {{ background: var(--row-hover); color: var(--ink); }}
nav a.cur {{ background: var(--accent-tint); color: var(--accent-2); font-weight: 500; }}
main {{ min-width: 0; background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 8px 32px 32px; }}
main img {{ max-width: 100%; border: 1px solid var(--border); border-radius: 8px; margin: 8px 0 16px; }}
main h1, main h2, main h3 {{ line-height: 1.25; }}
main h1 {{ font-size: 28px; margin-top: 24px; }}
main h2 {{ font-size: 20px; margin-top: 32px; border-top: 1px solid var(--border); padding-top: 16px; }}
main a {{ color: var(--accent-2); }}
main code {{ font-family: var(--font-mono); background: var(--surface-3); padding: 1px 5px; border-radius: 4px; font-size: 13px; }}
main pre {{ background: var(--surface-2); border: 1px solid var(--border); padding: 12px 14px; border-radius: 8px; overflow: auto; }}
main pre code {{ padding: 0; background: none; }}
main table {{ border-collapse: collapse; margin: 12px 0; width: 100%; font-size: 14px; }}
main th, main td {{ border: 1px solid var(--border); padding: 6px 10px; text-align: left; vertical-align: top; }}
main th {{ background: var(--surface-2); color: var(--ink-2); font-weight: 600; }}
main blockquote {{ border-left: 3px solid var(--accent); margin: 0; padding: 4px 14px; color: var(--ink-2); }}
@media (max-width: 860px) {{ .wrap {{ grid-template-columns: 1fr; }} nav {{ position: static; flex-direction: row; flex-wrap: wrap; }} main {{ padding: 8px 16px 24px; }} }}
</style></head>
<body><header><div class="brand">HyperCore <span>Manager</span> · manual</div><a href="/">← back to the dashboard</a>{f'<a style="margin-left:auto">{html.escape(version)}</a>' if version else ''}</header>
<div class="wrap"><nav>{items}<a href="/manual/administration">Administration guide</a></nav><main>{body_html}</main></div>
</body></html>'''
