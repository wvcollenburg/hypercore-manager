"""Server strings in the dashboard's HTML templates: escaped, and never compiled as code.

The HTML parser decodes an attribute before the browser compiles an inline handler, so
`onclick="f('${_clEsc(name)}')"` runs a VM tag or file name such as `')-alert(1)-('` as script:
`&#39;` is a quote again by the time the handler is compiled. Server strings go into a
`data-*` attribute and the handler reads them from `this.dataset`."""
import re

import pytest

FILES = ('dashboard/static/index.html', 'dashboard/static/access.js', 'dashboard/static/mail.js')

# ids the server generates (UUIDs, serial numbers), optionally escaped or defaulted to ''
_ID = re.compile(r"^(?:_clEsc|_exEsc)?\(?(?:[a-z]+\.)?(?:id|seq|uuid|vm_uuid|node_uuid|cluster_id|user_id|group_id|role_id"
                 r"|assignment_id|invitation_id|folder_id|node_id)(?:\s*\|\|\s*'')?\)?$")
# a choice between two literals: p.enabled ? 'false' : 'true'
_CHOICE = re.compile(r"^[\w.]+(?:\s*===\s*'\w+')?\s*\?\s*'\w*'\s*:\s*'\w*'$")
# names that only ever hold a number, an id or a constant of the page's own code
_CODE = {
    'i', 'id', 'uid', 'gid', 'cid', 'clusterId', 'folderId', 'definedAt', 'cur ? cur.id : 0',
    'key',        # a folder id / '__unfiled__', or a tunable's key from CTRL_INFO / SENT_INFO
    'k',          # a sub-tab name of Settings > Access
    'kind',       # 'vm' / 'folder' in the new-policy button
    'prefix', 'activeTab', 'addLevel', 'it.kind', 'def.key', 'meta.key', 'group.id',
    'v',          # a segment value of a CTRL_GROUPS control
    'open',       # acOpenGroup('<id>') / acOpenUser('<id>') built from ids
    'c.parent', "c.parent ? `'${c.parent}'` : 'null'",
}


def _interpolations(value):
    out, i = [], 0
    while (j := value.find('${', i)) >= 0:
        k, depth = j + 2, 1
        while depth and k < len(value):
            depth += {'{': 1, '}': -1}.get(value[k], 0)
            k += 1
        out.append(value[j + 2:k - 1].strip())
        i = k
    return out


def _inline_handlers(src):
    """(line, attribute value) of every inline handler that holds a ${...}."""
    for m in re.finditer(r'\bon[a-z]+="', src):
        i, depth = m.end(), 0
        while i < len(src):
            if src.startswith('${', i):
                depth, i = depth + 1, i + 2
                continue
            ch = src[i]
            if depth and ch in '{}':
                depth += 1 if ch == '{' else -1
            elif not depth and ch == '"':
                break
            i += 1
        value = src[m.end():i]
        if '${' in value:
            yield src.count('\n', 0, m.start()) + 1, value


@pytest.mark.parametrize('rel', FILES)
def test_inline_handlers_interpolate_only_ids_and_constants(repo, rel):
    src = (repo / rel).read_text(encoding='utf-8')
    bad = [f'{rel}:{line}: ${{{e}}}' for line, value in _inline_handlers(src) for e in _interpolations(value)
           if not (_ID.match(e) or _CHOICE.match(e) or e in _CODE)]
    assert not bad, 'put these in a data-* attribute and read this.dataset in the handler:\n' + '\n'.join(bad)


def test_the_scanner_sees_the_old_sinks():
    """The guard itself: the 1.8.0 VM group header and restore-point file links are found."""
    old = ("<tr onclick=\"toggleVmGroup(decodeURIComponent('${encodeURIComponent(gk)}'))\">"
           "<a onclick=\"_flrLoad('${_exEsc(full)}'); return false;\">")
    found = [e for _, v in _inline_handlers(old) for e in _interpolations(v)]
    assert found == ['encodeURIComponent(gk)', '_exEsc(full)']
    assert not any(_ID.match(e) or _CHOICE.match(e) or e in _CODE for e in found)


# fields that carry text from HyperCore, the balancer or Foresight: VM names, IPs, reasons, labels
_SERVER_TEXT = re.compile(r"\$\{\s*[a-z]+\.(?:vm_name|reason|source_ip|target_ip|entity_label|source_vm|bottleneck|engine|mode)"
                          r"\s*(?:\|\|[^}]*)?\}")


def test_server_text_is_escaped_in_html_templates(repo):
    """The migration log, forecast runs, predictions, right-sizing and What-If put VM names, IPs and reasons
    into innerHTML as they came (a VM named <img src=x onerror=...> ran in every admin's browser)."""
    src = (repo / 'dashboard' / 'static' / 'index.html').read_text(encoding='utf-8')
    raw = [f'line {src.count(chr(10), 0, m.start()) + 1}: {m.group(0)}' for m in _SERVER_TEXT.finditer(src)]
    assert not raw, 'wrap these in _clEsc():\n' + '\n'.join(raw)
    for sink in ("value=\"${v.vm_name}\"", "' ' + sm.source_vm", '{tz: c.timezone}', '{msg: e.message})}',
                 '<span class="sub-item-name">${v.name}</span>', "'operatingSystem' ? t('vmi.os') : `${k}: ${v}`"):
        assert sink not in src, f'unescaped: {sink}'


@pytest.mark.parametrize('rel', FILES)
def test_no_quote_only_escaping(repo, rel):
    """Replacing only " leaves < and & live: a title or value built that way is still HTML."""
    src = (repo / rel).read_text(encoding='utf-8')
    hits = [l for l in src.splitlines() if ".replace(/\"/g" in l and "&amp;" not in l]
    assert not hits, 'use _clEsc():\n' + '\n'.join(h.strip()[:120] for h in hits)
