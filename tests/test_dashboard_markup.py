"""The dashboard's tab containers must be siblings: a tab nested inside another tab is
hidden together with it (the Foresight-Central removal dropped a closing div and took
Settings, Account, Backup and Modules down with the Foresight tab)."""
import os
from html.parser import HTMLParser

INDEX = os.path.join(os.path.dirname(__file__), '..', 'dashboard', 'static', 'index.html')
VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'source', 'track', 'wbr'}


class _Tabs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack, self.tabs = [], {}   # tab id -> list of open tab ids above it

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if 'tab-content' in (a.get('class') or '').split():
            self.tabs[a.get('id')] = [t for t in self.stack if t]
            self.stack.append(a.get('id'))
        elif tag not in VOID:
            self.stack.append(None)

    def handle_endtag(self, tag):
        if tag not in VOID and self.stack:
            self.stack.pop()


def test_tab_containers_are_not_nested():
    p = _Tabs()
    p.feed(open(INDEX, encoding='utf-8').read())
    assert len(p.tabs) >= 6, p.tabs
    nested = {tab: above for tab, above in p.tabs.items() if above}
    assert not nested, f'tab containers nested inside another tab: {nested}'


def test_foresight_calls_name_the_selected_cluster(repo):
    """Report, VM list, What-If, saving the Foresight settings, the branding and 'Run analysis now' used a bare
    fetch: without ?cluster= the server takes the first cluster the user may see, so with cluster B selected
    the settings were saved to A and the report was A's."""
    import re
    src = (repo / 'dashboard' / 'static' / 'index.html').read_text()
    bare = re.findall(r"fetch\(\s*(?:`\$\{API_BASE\}|'|`)/api/sentinel/[^)\n]*", src)
    assert not bare, f'route these through _withCluster(): {bare}'


def test_staged_settings_do_not_outlive_their_form(repo):
    """An unapplied 'Dry run off' on cluster A was posted to cluster B by the next Apply of that group there:
    the staged edits were only cleared after a successful Apply. Every settings loader drops them."""
    src = (repo / 'dashboard' / 'static' / 'index.html').read_text()
    for loader, marker in (('async function loadSettings()', '_dropStaged(groups);'),
                           ('async function loadBalancerSettings()', '_dropStaged(groups);'),
                           ('async function loadSentinelSettings()', 'delete _sStagedSent[k]')):
        body = src[src.index(loader):][:1600]
        assert marker in body, f'{loader} must drop the edits staged on the previous form'
