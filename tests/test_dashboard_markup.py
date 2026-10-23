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
