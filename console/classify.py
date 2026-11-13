"""
classify.py — is this console screenshot a locked / logged-off screen, or an open session?

Order of the checks:
  1. blank   one colour across the whole screen (a blanked text console, a display turned off).
             Ambiguous: a logged-in session that blanked looks the same as a locked one, so it is
             its own verdict, never a pass. Waking the screen before the grab avoids most of these.
  2. learnt  the admin put the VM in its locked state and clicked "learn locked state"; a grab whose
             perceptual hash is close to one of those references is locked. Several references per
             VM cover lock screens that change (rotating backgrounds, a different boot log).
  3. text    OCR (tesseract): a text console whose last login prompt is not followed by a shell
             prompt is logged off; lock-screen phrases ("Press Ctrl+Alt+Delete to unlock",
             "Sign-in options") mean a graphical lock or sign-in screen. Needs no learning.
  4. open    nothing matched: the flag. "Learn this as locked" from the flag fixes a false alarm.
"""
from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Optional

from PIL import Image, ImageOps, ImageStat

HASH_SIZE = 16                    # 16x16 difference hash = 256 bits
MATCH_BITS = 24                   # at most this many differing bits counts as the same screen
THUMB_WIDTH = 320

# a login prompt anywhere in a line: "<host> login:", "Password:" (and a few translations). Not at the end
# only: cloud-init and the kernel print onto the prompt's line ("container01 login: [ 136.65] cloud-init...").
# "Last login: ..." in a logged-in motd is not one.
_LOGIN_LINE = re.compile(r'(?<!last )\b(login|password|passwort|wachtwoord|mot de passe|kennwort)\s*:', re.I)
# getty's banner above the prompt (/etc/issue): "Ubuntu 22.04.5 LTS host tty1", "Kernel 5.14 on an x86_64".
# Seen on its own (prompt not drawn yet, or cleared) it still means nobody is logged in on that tty.
# OCR reads tty1 as ttyt / ttyl, so any word starting with tty counts.
_ISSUE_LINE = re.compile(r'(\btty\w{0,3}|\bon an? (x86_64|aarch64|i686))\s*[_|]?\s*$', re.I)
# a shell prompt after it means someone logged in: "user@host:~$", "[root@host ~]#", "host:~ #"
_SHELL_LINE = re.compile(r'([\w.-]+@[\w.-]+.*[$#%>]|[$#])\s*[_|]?\s*$')   # a text cursor may follow
# graphical lock and sign-in screens (letters only, accents stripped, lower case)
LOCK_PHRASES = (
    'ctrlaltdel', 'ctrlaltentf',                       # "Press Ctrl+Alt+Delete to unlock / to sign in"; de: Strg+Alt+Entf
    'tounlock', 'signinoptions', 'otheruser',
    'aanmeldingsopties', 'ontgrendelen', 'anderegebruiker',
    'anmeldeoptionen', 'entsperren', 'andererbenutzer',
    'optionsdeconnexion', 'deverrouiller', 'autreutilisateur',
)


@dataclass
class Verdict:
    state: str                    # locked | open | blank | unknown
    detail: str
    hash: str


# ------------------------------------------------------------------ hashing
def dhash(img: Image.Image, size: int = HASH_SIZE) -> str:
    """Difference hash: grey, shrink to (size+1) x size, one bit per horizontal neighbour pair."""
    g = img.convert('L').resize((size + 1, size), Image.Resampling.LANCZOS)
    px = g.tobytes()                  # one byte per pixel in mode L
    bits = 0
    for row in range(size):
        base = row * (size + 1)
        for col in range(size):
            bits = (bits << 1) | (1 if px[base + col] > px[base + col + 1] else 0)
    return f'{bits:0{size * size // 4}x}'


def hamming(a: str, b: str) -> int:
    try:
        return bin(int(a, 16) ^ int(b, 16)).count('1')
    except ValueError:
        return HASH_SIZE * HASH_SIZE


def best_match(h: str, refs: Iterable[str]) -> Optional[int]:
    """Smallest distance to any reference, or None without references."""
    dists = [hamming(h, r) for r in refs]
    return min(dists) if dists else None


def is_blank(img: Image.Image) -> bool:
    lo, hi = img.convert('L').getextrema()
    return hi - lo < 10


def thumbnail(img: Image.Image, width: int = THUMB_WIDTH) -> bytes:
    t = img.copy()
    t.thumbnail((width, width))
    out = io.BytesIO()
    t.save(out, 'PNG', optimize=True)
    return out.getvalue()


def png(img: Image.Image) -> bytes:
    out = io.BytesIO()
    img.save(out, 'PNG', optimize=True)
    return out.getvalue()


# ---------------------------------------------------------------------- OCR
def ocr_available() -> bool:
    return shutil.which('tesseract') is not None


class OCRFailed(Exception):
    pass


def ocr_lines(img: Image.Image, timeout: float = 60) -> list[str]:
    """Text lines on the screen, top to bottom. Dark screens (text consoles) are inverted first and
    small ones (a 720x400 VGA text console) enlarged: tesseract reads dark text on a light background
    best. 1024 px and wider are read as they are: enlarging only costs time (measured on Lab 241: a
    1280x800 console full of boot log 7.6 s at native size, 11.6 s at 2x, at half a CPU).
    Raises OCRFailed when tesseract fails or takes too long: a screen that could not be read must
    never count as an open session, nor as a locked one."""
    g = img.convert('L')
    if ImageStat.Stat(g).mean[0] < 110:
        g = ImageOps.invert(g)
    if g.width < 1000:
        f = 2 if g.width >= 640 else 3
        g = g.resize((g.width * f, g.height * f), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    g.save(buf, 'PNG')
    # one thread: under a CPU limit tesseract's OpenMP threads only fight each other (33 s instead of 12)
    env = dict(os.environ, OMP_THREAD_LIMIT='1')
    try:
        r = subprocess.run(['tesseract', 'stdin', 'stdout', '--psm', '6'], input=buf.getvalue(),
                           capture_output=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise OCRFailed(f'text recognition took longer than {timeout:g} s') from None
    except OSError as e:
        raise OCRFailed(f'text recognition unavailable: {e}') from None
    if r.returncode != 0:
        raise OCRFailed('text recognition failed: ' + r.stderr.decode('utf-8', 'replace').strip()[:120])
    return [ln.strip() for ln in r.stdout.decode('utf-8', 'replace').splitlines() if ln.strip()]


def _letters(s: str) -> str:
    s = unicodedata.normalize('NFKD', s.lower())
    return ''.join(c for c in s if 'a' <= c <= 'z')


def text_verdict(lines: list[str]) -> Optional[str]:
    """What the text on the screen says: 'login prompt' or 'login banner' (a text console nobody is
    logged in on), 'lock screen' (a graphical lock or sign-in screen), 'shell' (a shell prompt after
    the last login prompt: someone is logged in), or None (none of these visible)."""
    meaningful = [ln for ln in lines if sum(ch.isalnum() for ch in ln) >= 3 or ln.endswith(('$', '#'))]
    last = lambda rx: max((i for i, ln in enumerate(meaningful) if rx.search(ln)), default=-1)  # noqa: E731
    login, issue, shell = last(_LOGIN_LINE), last(_ISSUE_LINE), last(_SHELL_LINE)
    if shell > max(login, issue):
        return 'shell'
    if login >= 0 and login >= issue:
        return 'login prompt'
    if issue >= 0:
        return 'login banner'
    flat = _letters(' '.join(lines))
    if any(p in flat for p in LOCK_PHRASES):
        return 'lock screen'
    return None


# ------------------------------------------------------------------ verdict
def classify(img: Image.Image, refs: Iterable[str] = (), use_ocr: bool = True, ocr=ocr_lines) -> Verdict:
    h = dhash(img)
    if is_blank(img):
        return Verdict('blank', 'screen is blank', h)
    dist = best_match(h, refs)
    if dist is not None and dist <= MATCH_BITS:
        return Verdict('locked', f'matches a learnt locked screen ({dist} bits apart)', h)
    if use_ocr:
        try:
            lines = ocr(img)
        except OCRFailed as e:
            return Verdict('unknown', str(e), h)
        found = text_verdict(lines)
        if found == 'shell':
            return Verdict('open', 'shell prompt on the console', h)
        if found:
            return Verdict('locked', found, h)
        if len(lines) >= 3 and ImageStat.Stat(img.convert('L')).mean[0] < 40:
            # a text console full of log output (kernel or cloud-init messages) with neither a login nor
            # a shell prompt in view: undecidable without pressing a key, so never a pass and never an alarm
            return Verdict('unknown', 'only log output on the console, no prompt in view', h)
    return Verdict('open', 'no lock or login screen recognised', h)
