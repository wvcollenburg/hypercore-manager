"""The VM console service (backlog #1/#2): RFB screenshots, the lock-screen classifier, one-time
tickets, and the relay's idle detection. Screens come from tools/fake_hypercore, a synthetic
HyperCore with a scripted RFB console, so nothing here needs a cluster."""
import asyncio
import importlib.util
import os
import socket
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'console'))
sys.path.insert(0, str(REPO / 'tools' / 'fake_hypercore'))

PIL = pytest.importorskip('PIL')
from PIL import Image  # noqa: E402

import classify  # noqa: E402
import rfb  # noqa: E402

aiohttp = pytest.importorskip('aiohttp')
fake = pytest.importorskip('fake_hypercore')

needs_ocr = pytest.mark.skipif(not classify.ocr_available(), reason='tesseract not installed')


def _screen(name: str, screen: str, **kw) -> Image.Image:
    v = {'name': name, 'screen': screen, 'typed': '', 'login_step': 'user', **kw}
    return fake.render(v)


# ------------------------------------------------------------ classifier
def test_blank_screen_is_its_own_verdict_never_a_pass():
    v = classify.classify(Image.new('RGB', (720, 400)), refs=[], use_ocr=False)
    assert v.state == 'blank'


def test_learnt_reference_matches_the_same_screen_and_not_a_desktop():
    lock = _screen('kiosk01', 'picture')
    ref = classify.dhash(lock)
    assert classify.classify(lock, [ref], use_ocr=False).state == 'locked'
    desk = _screen('kiosk01', 'windesk')
    assert classify.classify(desk, [ref], use_ocr=False).state == 'open'


def test_hash_tolerates_the_clock_changing():
    """The lock screen's clock changes every minute; a learnt reference must still match."""
    a, b = _screen('dc01', 'picture'), _screen('dc01', 'picture')
    from PIL import ImageDraw
    ImageDraw.Draw(b).rectangle([60, 520, 400, 620], fill=(30, 60, 120))     # a different time
    assert classify.hamming(classify.dhash(a), classify.dhash(b)) <= classify.MATCH_BITS


@pytest.mark.parametrize('lines, verdict', [
    (['[ OK ] Started ssh.service', 'Ubuntu 24.04.1 LTS web01 tty1', 'web01 login: _'], 'login prompt'),
    (['web01 login: root', 'Password:'], 'login prompt'),                                   # typed a name, walked away
    (['web01 login: root', 'Password:', 'Welcome to Ubuntu', 'root@web01:~# _'], 'shell'),    # logged in
    (['Last login: Thu Nov 12 09:14:02 2026 from 10.0.0.5', 'root@web01:~#'], 'shell'),
    (['[root@db01 ~]#'], 'shell'),
    (['web01 login: admin', 'Login incorrect', 'web01 login:'], 'login prompt'),
    # seen on Lab 241 (9.8.4, 2026-11-12):
    (['Ubuntu 22.04.5 LTS mineco1 ttyt'], 'login banner'),                                  # banner, prompt not drawn
    (['Ubuntu 22.04.5 LTS container01 tty1', "container01 login: [ 136.652466] cloud-init[2748]: Cloud-init v. 25.2 running 'modules:final'",
      '[ 136.795075] cloud-init[2748]: Cloud-init v. 25.2 finished at Mon, 09 Nov 2026 10:56:38 +0000.'], 'login prompt'),
    (['Red Hat Enterprise Linux 9.4 (Plow)', 'Kernel 5.14.0-427.el9.x86_64 on an x86_64', 'rh01 login:'], 'login prompt'),
    (['Press Ctrl+Alt+Delete to unlock.', '10:42', 'Thursday, November 12'], 'lock screen'),
    (['Druk op Ctrl+Alt+Del om te ontgrendelen'], 'lock screen'),
    (['Strg+Alt+Entf drücken zum Entsperren'], 'lock screen'),
    (['Server Manager', 'Dashboard Local Server'], None),
    (['[275620.164415] Buffer I/O error on dev nbd4, logical block 3', '[275622.504546] block nbd6: Attempted send on invalid socket'], None),
    ([], None),
])
def test_text_rules(lines, verdict):
    assert classify.text_verdict(lines) == verdict


def test_ocr_result_is_used_when_nothing_was_learnt():
    img = _screen('web01', 'login')
    assert classify.classify(img, [], ocr=lambda _: ['web01 login:']).state == 'locked'
    assert classify.classify(img, [], ocr=lambda _: ['root@web01:~#']).state == 'open'


def test_a_screen_that_could_not_be_read_is_unknown_never_open():
    def boom(_):
        raise classify.OCRFailed('text recognition took longer than 60 s')
    v = classify.classify(_screen('rds01', 'windesk'), [], ocr=boom)
    assert v.state == 'unknown' and '60 s' in v.detail


def test_a_text_console_full_of_log_output_is_unknown_not_an_alarm():
    img = _screen('web01', 'login')                     # dark text console
    log = ['[275620.1] I/O error, dev nbd4, sector 0', '[275620.2] Buffer I/O error on dev nbd4', '[275622.5] block nbd6: Attempted send']
    assert classify.classify(img, [], ocr=lambda _: log).state == 'unknown'
    desk = _screen('rds01', 'windesk')                  # a light desktop with a few words stays an open session
    assert classify.classify(desk, [], ocr=lambda _: ['Server Manager', 'Dashboard', 'Local Server']).state == 'open'


@needs_ocr
@pytest.mark.parametrize('screen, expected', [
    ('login', 'locked'), ('shell', 'open'), ('winlock', 'locked'), ('windesk', 'open'),
    ('picture', 'open'),            # a graphical lock screen without text needs a learnt reference
])
def test_real_ocr_on_synthetic_screens(screen, expected):
    assert classify.classify(_screen('web01', screen)).state == expected


def test_thumbnail_is_small_png():
    data = classify.thumbnail(_screen('dc01', 'winlock'))
    assert data[:8] == b'\x89PNG\r\n\x1a\n' and len(data) < 200_000


# ------------------------------------------------------------- RFB relay
def test_framebuffer_update_requests_alone_are_not_user_input():
    req = rfb.update_request(1024, 768, incremental=True)
    assert not rfb.is_user_input(req, True)
    assert not rfb.is_user_input(req * 3, True)                   # batched
    assert rfb.is_user_input(rfb.key(0x61, True), True)
    assert rfb.is_user_input(rfb.pointer(5, 5), True)
    assert rfb.is_user_input(req + rfb.key(0x61, True), True)
    assert rfb.is_user_input(b'RFB 003.008\n', True)


# ------------------------------------------------------------- end to end
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


async def _with_fake(fn):
    from aiohttp import web
    port = _free_port()
    runner = web.AppRunner(fake.make_app())
    await runner.setup()
    await web.TCPSite(runner, '127.0.0.1', port).start()
    try:
        async with aiohttp.ClientSession() as http:
            r = await http.post(f'http://127.0.0.1:{port}/rest/v1/login', json={'username': 'x', 'password': 'fake'})
            sid = (await r.json())['sessionID']
            return await fn(http, f'127.0.0.1:{port}', sid)
    finally:
        await runner.cleanup()


def _vm(name):
    return next(v for v in fake.state['vms'].values() if v['name'] == name)


def test_screenshot_through_the_console_websocket():
    async def go(http, host, sid):
        v = _vm('dc01')
        node = fake.NODES[v['node']]['uuid']
        ws = await http.ws_connect(f'http://{host}/node/{node}/console?sessionID={sid}&vm={v["uuid"]}', protocols=('binary',))
        img = await rfb.screenshot(ws)
        await ws.close()
        return img
    img = asyncio.run(_with_fake(go))
    assert img.size == (1024, 768)
    assert img.getpixel((10, 10)) != (0, 0, 0)


def test_wrong_node_or_session_is_refused_like_hypercore():
    async def go(http, host, sid):
        v = _vm('web01')
        out = []
        for node, s in (('not-the-node', sid), (fake.NODES[v['node']]['uuid'], 'bad-session')):
            ws = await http.ws_connect(f'http://{host}/node/{node}/console?sessionID={s}&vm={v["uuid"]}')
            with pytest.raises(rfb.RFBError):
                await rfb.screenshot(ws, timeout=3)
            out.append(True)
        return out
    assert asyncio.run(_with_fake(go)) == [True, True]


def test_wake_turns_a_blanked_console_into_its_real_state():
    async def go(http, host, sid):
        v = _vm('app02')
        v['screen'], v['wake_to'] = 'blank', 'shell'
        node = fake.NODES[v['node']]['uuid']
        url = f'http://{host}/node/{node}/console?sessionID={sid}&vm={v["uuid"]}'
        ws = await http.ws_connect(url)
        before = await rfb.screenshot(ws, wake='none')
        await ws.close()
        ws = await http.ws_connect(url)
        after = await rfb.screenshot(ws, wake='pointer', settle=0.3)
        await ws.close()
        return before, after
    before, after = asyncio.run(_with_fake(go))
    assert classify.is_blank(before) and not classify.is_blank(after)


# ---------------------------------------------------------------- tickets
@pytest.fixture
def consoles(monkeypatch):
    for name in ('hc', 'store'):
        if name not in sys.modules:
            spec = importlib.util.spec_from_file_location(name, REPO / 'console' / f'{name}.py')
            mod = importlib.util.module_from_spec(spec)
            try:
                spec.loader.exec_module(mod)
            except Exception:  # noqa: BLE001 — psycopg2 / registry stubs are enough for tickets
                pass
            sys.modules[name] = mod
    import consoles as c
    c._tickets.clear()
    return c


def test_ticket_is_single_use(consoles):
    t = consoles.mint('c', 'vm', 'vm', 'alice', 'b' * 64, None)
    assert consoles.consume(t, 'b' * 64) is not None
    assert consoles.consume(t, 'b' * 64) is None


def test_ticket_is_bound_to_the_browser_session_and_burnt_by_a_wrong_one(consoles):
    t = consoles.mint('c', 'vm', 'vm', 'alice', 'b' * 64, None)
    assert consoles.consume(t, 'x' * 64) is None
    assert consoles.consume(t, 'b' * 64) is None                # a stolen ticket tried elsewhere is gone


def test_ticket_expires(consoles, monkeypatch):
    t = consoles.mint('c', 'vm', 'vm', 'alice', 'b' * 64, None)
    real = time.monotonic
    monkeypatch.setattr(consoles.time, 'monotonic', lambda: real() + consoles.TICKET_TTL + 1)
    assert consoles.consume(t, 'b' * 64) is None


def test_a_refused_console_connection_never_carries_the_hypercore_session(consoles, monkeypatch):
    """aiohttp's WSServerHandshakeError renders as status, message and the full URL, sessionID included; that text
    went to the log, the open-session check details on the dashboard and API error bodies."""
    import types
    from aiohttp import web

    async def refuse(request):
        return web.Response(status=403, text='no')

    async def go():
        port = _free_port()
        app = web.Application(); app.router.add_get('/node/{node}/console', refuse)
        runner = web.AppRunner(app); await runner.setup(); await web.TCPSite(runner, '127.0.0.1', port).start()
        target = types.SimpleNamespace(url=f'ws://127.0.0.1:{port}/node/n1/console?sessionID=S3CRET-SESSION&vm=v1',
                                       ssl=False, vm_name='dc01', node_uuid='n1')
        monkeypatch.setattr(consoles.hc, 'console_target', lambda c, v: target, raising=False)
        try:
            async with aiohttp.ClientSession() as http:
                with pytest.raises(consoles.hc.ConsoleError) as ei:
                    await consoles.open_upstream(http, 'c', 'v1')
                return ei.value
        finally:
            await runner.cleanup()
    e = asyncio.run(go())
    assert 'S3CRET-SESSION' not in str(e) and 'sessionID' not in str(e) and '127.0.0.1' not in str(e)
    assert str(e) == 'console connection to the cluster failed: the cluster answered HTTP 403' and e.status == 502
    assert consoles._upstream_failure(OSError('x sessionID=abc&vm=1')) == 'x sessionID=***&vm=1'


def test_no_ticket_no_binding(consoles):
    assert consoles.consume('', '') is None
    t = consoles.mint('c', 'vm', 'vm', 'alice', 'b' * 64, None)
    assert consoles.consume(t, '') is None


# ------------------------------------------------------- repository guards
def test_hypercore_session_never_reaches_the_browser():
    """The console page and the dashboard never handle HyperCore's sessionID: only the console
    service builds the cluster URL."""
    page = (REPO / 'dashboard' / 'templates' / 'console.html').read_text()
    app = (REPO / 'dashboard' / 'app.py').read_text()
    assert 'sessionID' not in page and 'sessionID' not in app
    assert "sessionID" in (REPO / 'console' / 'hc.py').read_text()


def test_console_service_is_internal_only():
    import re
    compose = (REPO / 'docker-compose.yaml').read_text()
    block = re.search(r'\n  console:\n(.*?)\n  # -{10,}', compose, re.S).group(1)
    assert 'ports:' not in block


def test_carries_input_for_read_only_viewers():
    """A watcher's browser may ask for screen updates but never send keys, pointer or clipboard."""
    import importlib, sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'console'))
    rfb = importlib.import_module('rfb')
    fbur = bytes([3, 1, 0, 0, 0, 0, 4, 0, 3, 0])
    assert not rfb.carries_input(fbur)
    assert not rfb.carries_input(fbur * 3)
    assert rfb.carries_input(bytes([4, 1, 0, 0, 0, 0, 0, 0x61]))           # key event
    assert rfb.carries_input(bytes([5, 1, 0, 10, 0, 10]))                   # pointer event
    assert rfb.carries_input(bytes([6, 0, 0, 0, 0, 0, 0, 1, 0x61]))         # clipboard
    assert rfb.carries_input(fbur + bytes([4, 1, 0, 0, 0, 0, 0, 0x61]))     # batched behind an update request
    assert not rfb.carries_input(b'RFB 003.008\n') and not rfb.carries_input(b'\x01')


# ------------------------------------------------------------------ boot verdict (test restores)
@pytest.mark.parametrize('lines, verdict', [
    (['Ubuntu 24.04 LTS btest01 tty1', 'btest01 login:'], 'pass'),
    (['Press Ctrl+Alt+Delete to sign in.'], 'pass'),
    (['root@appliance:~#'], 'pass'),
    (['Your PC ran into a problem and needs to restart.', 'Stop code: INACCESSIBLE_BOOT_DEVICE'], 'fail'),
    (['BitLocker recovery', 'Enter the recovery key for this drive'], 'fail'),
    (['No bootable device -- insert boot disk and press any key'], 'fail'),
    (['grub rescue>'], 'fail'),
    (['You are in emergency mode. After logging in, type "journalctl -xb"'], 'fail'),
    (['[  OK  ] Started Network Manager.', '[  OK  ] Reached target Basic System.', 'Starting cloud-init...'], 'wait'),
])
def test_boot_verdict(lines, verdict):
    img = Image.new('RGB', (720, 400), (40, 40, 40))
    img.putpixel((5, 5), (250, 250, 250))                 # not blank
    assert classify.boot_verdict(img, ocr=lambda _i: lines, ocr2=lambda _i: [])[0] == verdict


def test_a_blank_or_unreadable_screen_means_wait_never_pass():
    assert classify.boot_verdict(Image.new('RGB', (720, 400)), ocr=lambda _i: ['login:'], ocr2=lambda _i: [])[0] == 'wait'
    img = Image.new('RGB', (720, 400), (40, 40, 40)); img.putpixel((5, 5), (250, 250, 250))

    def broken(_i):
        raise classify.OCRFailed('tesseract died')
    assert classify.boot_verdict(img, ocr=broken, ocr2=lambda _i: [])[0] == 'wait'
    assert classify.boot_verdict(img, use_ocr=False)[0] == 'wait'


def test_a_windows_lock_screen_with_only_its_clock_passes_through_the_white_text_pass():
    """Live 2026-11-21: a restored Windows 11 VM showed only clock and date; the normal OCR pass read '-'."""
    img = Image.new('RGB', (1024, 768), (20, 50, 200)); img.putpixel((5, 5), (250, 250, 250))
    assert classify.boot_verdict(img, ocr=lambda _i: ['-'], ocr2=lambda _i: ['23:36', 'Saturday, 21 November']) == \
        ('pass', 'lock screen (clock and date)')
    assert classify.boot_verdict(img, ocr=lambda _i: ['-'], ocr2=lambda _i: ['Your PC ran into a problem'])[0] == 'fail'
    assert classify.boot_verdict(img, ocr=lambda _i: ['-'], ocr2=lambda _i: ['12:00'])[0] == 'wait', 'a clock alone is not enough'
