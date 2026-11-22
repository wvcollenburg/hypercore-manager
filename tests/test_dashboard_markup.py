"""The dashboard's tab containers must be siblings: a tab nested inside another tab is
hidden together with it (the Foresight-Central removal dropped a closing div and took
Settings, Account, Backup and Modules down with the Foresight tab)."""
import os
from html.parser import HTMLParser

import pytest

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


def test_enter_in_a_confirmation_does_not_press_the_danger_button_from_cancel(repo):
    """Enter anywhere in hcmConfirm clicked the last (confirm/danger) button, also with focus on Cancel; a dialog
    without a field left focus on the button behind the backdrop, so Enter or Space opened a second dialog."""
    src = (repo / 'dashboard' / 'static' / 'access.js').read_text()
    confirm = src[src.index('function hcmConfirm('):src.index('function hcmPrompt(')]
    assert "e.target.tagName === 'BUTTON'" in confirm
    modal = src[src.index('function hcmModal('):src.index('function hcmConfirm(')]
    assert "back.querySelector('.hcm-modal-foot button')" in modal and 'activeElement?.blur' in modal


def test_irreversible_deletes_ask_for_the_name(repo):
    """Deleting every restore point of a VM, a proxy's VM or a backup target was one click on OK."""
    src = (repo / 'dashboard' / 'static' / 'index.html').read_text()
    for key in ('ex.confirm_delete_data', 'proxies.confirm_remove_vm', 'shares.confirm_remove'):
        call = src[src.index(f"hcmConfirm(t('{key}'"):][:160]
        assert 'typeToConfirm: name' in call, f'{key} must ask for the name to be typed'


def test_a_lost_session_goes_to_the_login_page(repo):
    """The API answers 401 when the session is gone; no wrapper looked at it, so the page sat on empty
    lists and 'HTTP 401' toasts until it was reloaded by hand."""
    src = (repo / 'dashboard' / 'static' / 'index.html').read_text()
    wrap = src.index('window.fetch = async function')
    body = src[wrap:wrap + 700]
    assert 'r.status === 401' in body and "location.href = '/login'" in body and '_hcmSignedOut = true' in body
    main = src[src.index('<script>', 100):]          # the app script: nothing fetches before the wrapper is set
    assert 'fetch(' not in main[:main.index('window.fetch = async function')]
    for wrapper in ('async function apiFetch(', 'async function _exCall(', 'async function _folderCall('):
        assert src.index(wrapper) > wrap, f'{wrapper} must be defined after the 401 check'


PER_CLUSTER_LOADERS = ('loadNodes', 'loadVMs', 'loadVmIndex', 'loadVmPending', 'loadMigrations', 'loadForecastRuns',
                       'loadPredictions', 'loadActivity', 'loadSentinelOverview', 'loadSentinelForecasts',
                       'loadSentinelRightsizing', 'loadSentinelExhaustion', '_loadWhatifVmList', 'exLoadSchedules',
                       'exLoadPolicies', 'exLoadVms', 'exLoadRuns', 'exLoadPoints')


@pytest.mark.parametrize('loader', PER_CLUSTER_LOADERS)
def test_a_late_answer_for_the_previous_cluster_is_dropped(repo, loader):
    """A slow answer for cluster A that arrived after a switch to B overwrote B's VM list, charts and tables."""
    src = (repo / 'dashboard' / 'static' / 'index.html').read_text()
    start = src.index(f'async function {loader}(')
    body = src[start:src.index('\n}\n', start)]
    assert 'cid = currentCluster' in body, f'{loader} must note the cluster it asks for'
    first_await = body.index('await ')
    assert 'cid !== currentCluster' in body[first_await:], f'{loader} must drop an answer for another cluster'


def test_vm_edit_keeps_typed_values_when_a_button_redraws_the_form(repo):
    """+ Add disk, + Add NIC, Remove/Keep and the boot order buttons redrew the VM edit form from the VM as loaded:
    a memory size or VLAN typed before was silently back to the old value, and Apply changed less than intended."""
    import re
    src = (repo / 'dashboard' / 'static' / 'index.html').read_text()
    render = src[src.index('function _veRender()'):src.index('function _veSync()')]
    assert '_ve.form' in render, '_veRender must put back the values _veSync kept'
    for m in re.finditer(r'_veRender\(\)', src):
        line = src[src.rfind('\n', 0, m.start()) + 1:src.find('\n', m.start())]
        if 'function _veRender' in line or line.strip() == '_veRender();':   # the definition, and openVmEdit's first draw
            continue
        assert '_veSync()' in line[:line.index('_veRender()')], f'redraw without _veSync(): {line.strip()[:120]}'


def test_a_role_on_a_tag_shows_its_buttons_without_a_search(repo):
    """_vmTags() reads the VM index, which loadVMs only fetched while the search box had text: a user whose role
    is assigned on a tag saw no Edit or Console button until they typed something."""
    src = (repo / 'dashboard' / 'static' / 'index.html').read_text()
    body = src[src.index('async function loadVMs()'):][:1500]
    assert 'if (vmQuery || _hasTagGrant(cid)) { await loadVmIndex(' in body
    helper = src[src.index('function _hasTagGrant('):][:200]
    assert 'g.tag' in helper


BUTTON_PERMS = (('onclick="exRunNow(', "_exCan('backup.run'"), ('onclick="exToggle(${s.id})', "_exCan('backup.schedule'"),
                ('onclick="exRemove(', "_exCan('backup.schedule'"), ('onclick="exVerifyChain(', "_exCan('backup.verify'"),
                ('onclick="exDeleteChain(', "_exCan('backup.delete'"), ('onclick="exRestoreForm(', 'mayRestore ?'),
                ('onclick="exFilesOpen(', 'mayBrowse ?'), ('onclick="exPolicyRunNow(', "_exFolderCan('backup.run')"),
                ('onclick="exPolicyEdit(${p.id})', "_exFolderCan('backup.schedule')"), ('onclick="trRun(', "_exFolderCan('backup.run')"),
                ('onclick="exToggleForm(', "_exCan('backup.schedule'"), ('onclick="muteAlert(', "can('cluster.alerts.mute', cid)"),
                ('onclick="vePower(\'START\')', "may('vm.power')"), ('onclick="veDelete()', "may('vm.delete')"))


@pytest.mark.parametrize('button,guard', BUTTON_PERMS)
def test_buttons_show_only_with_the_permission_their_route_needs(repo, button, guard):
    """The Backup tab showed Run now, Pause, Remove, Verify, Delete data, Restore and Files to anyone with backup.view
    (_exIsAdmin() was always true), the alerts dialog showed Mute without cluster.alerts.mute, and the VM edit dialog
    showed power and delete controls to whoever held any one of the VM change permissions."""
    src = (repo / 'dashboard' / 'static' / 'index.html').read_text()
    assert '_exIsAdmin' not in src
    at = src.index(button)
    assert guard in src[max(0, at - 900):at], f'{button} must be guarded by {guard}'


def test_update_and_console_check_need_their_permission(repo):
    """The Update button led to Check now / Apply update (install.manage) for everyone, and the engineer role's
    Settings tab called /api/console/check (console_check.manage) and got 403 instead of its audit log."""
    src = (repo / 'dashboard' / 'static' / 'index.html').read_text()
    assert 'id="updateBadgeBtn"' in src and 'data-i18n="update.button" hidden>Update<' in src
    body = src[src.index('async function loadUpdateStatus()'):][:500]
    assert "badge.hidden = !can('install.manage')" in body
    panel = src[src.index('async function _loadConsolePanel()'):][:700]
    assert "const mayCheck = can('console_check.manage', currentCluster)" in panel
    assert "!mayCheck ? Promise.resolve({}) : fetch(_withCluster('/api/console/check'))" in panel


def test_data_quality_banner_waits_for_the_clusters_and_leaves_room(repo):
    """A fixed 1.5 s timer ran the check before /api/clusters had answered on a cold load: 'collecting history
    for : 9.5 d' with an empty name. The fixed bar also covered the last table row."""
    src = (repo / 'dashboard' / 'static' / 'index.html').read_text()
    assert 'setTimeout(checkDataQuality' not in src
    loader = src[src.index('async function loadClusters()'):][:1800]
    assert 'checkDataQuality()' in loader
    check = src[src.index('async function checkDataQuality()'):][:2600]
    assert 'if (!name) return;' in check
    assert "classList.toggle('has-dq-banner'" in check and 'body.has-dq-banner { padding-bottom:' in src


def test_small_frontend_regressions_stay_fixed(repo):
    """Module icons appended a hex alpha to var(--…) (invalid CSS, no tint); restore point dates had no year; the
    guest link and code, shown once, could be closed by Escape or the backdrop before they were copied; a VM
    deleted from the edit dialog was reopened 2.5 s later; Escape on VM details skipped closeVmModal()."""
    src = (repo / 'dashboard' / 'static' / 'index.html').read_text()
    assert 'background:${m.color}20' not in src and 'color-mix(in srgb, ${m.color} 12%, transparent)' in src
    fmt = src[src.index('const _exFmt = iso =>'):][:400]
    assert "o.year = 'numeric'" in fmt
    submit = src[src.index('async function _veSubmit('):][:1800]
    assert "kind === 'delete' && !when.run_at" in submit and 'closeVmEdit()' in submit
    assert "classList.contains('visible')) closeVmModal();" in src
    acc = (repo / 'dashboard' / 'static' / 'access.js').read_text()
    secrets = acc[acc.index('function _gcShowSecrets('):][:300]
    assert 'closable: false' in secrets


@pytest.mark.parametrize('rel', ('dashboard/static/index.html', 'dashboard/static/access.js'))
def test_a_network_error_does_not_leave_a_spinner(repo, rel):
    """An `await fetch(...)` straight in a function body throws on a network error: the VM edit, New VM, cluster
    test and console check screens kept their spinner or 'Testing…' for good. Those calls go through _netFetch,
    which turns the error into a 503 answer the !r.ok branch already shows."""
    import re
    src = (repo / rel).read_text(encoding='utf-8')
    bare = [l.strip()[:100] for l in src.splitlines()
            if re.match(r'^  (const r = )?await fetch\(', l)]
    assert not bare, 'use _netFetch() or a try/catch:\n' + '\n'.join(bare)
