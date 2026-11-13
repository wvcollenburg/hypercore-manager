"""
rfb.py — just enough of the RFB (VNC) protocol to take one screenshot through HyperCore's console
websocket, and to tell user input apart from screen-update requests in a relayed stream.

HyperCore's console websocket carries plain RFB 3.8 with security type None (the session in the URL
is the only protection). A screenshot is: handshake, ask for 32-bit true colour and the Raw encoding,
optionally wake the screen (a pointer nudge or a Shift tap), request one full frame, read it. The
connection is opened with the shared flag, so an admin who has the console open stays connected.
"""
from __future__ import annotations

import asyncio
import struct
from typing import Optional

from PIL import Image

RAW = 0
DESKTOP_SIZE = -223
XK_SHIFT_L = 0xFFE1


class RFBError(Exception):
    pass


class WsReader:
    """Byte-stream reads over websocket messages (aiohttp ClientWebSocketResponse or anything with
    `receive_bytes()` and `send_bytes()`)."""
    def __init__(self, ws, timeout: float = 15):
        self.ws, self.timeout, self.buf = ws, timeout, bytearray()

    async def read(self, n: int) -> bytes:
        while len(self.buf) < n:
            try:
                chunk = await asyncio.wait_for(self.ws.receive_bytes(), self.timeout)
            except asyncio.TimeoutError:
                raise RFBError('console did not answer in time') from None
            except TypeError as e:          # aiohttp: a close or text frame where bytes were expected
                raise RFBError(f'console closed the connection ({e})') from None
            self.buf.extend(chunk)
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    async def send(self, data: bytes) -> None:
        await self.ws.send_bytes(data)


async def _reason(r: WsReader) -> str:
    (n,) = struct.unpack('>I', await r.read(4))
    return (await r.read(n)).decode('utf-8', 'replace')


async def handshake(r: WsReader) -> tuple[int, int, str]:
    """RFB 3.3/3.7/3.8 handshake with security None. Returns (width, height, desktop name)."""
    banner = await r.read(12)
    if not banner.startswith(b'RFB '):
        raise RFBError(f'not an RFB server: {banner!r}')
    major, minor = int(banner[4:7]), int(banner[8:11])
    version = (3, 8) if (major, minor) >= (3, 8) else (3, 7) if (major, minor) >= (3, 7) else (3, 3)
    await r.send(b'RFB %03d.%03d\n' % version)
    if version == (3, 3):
        (sec,) = struct.unpack('>I', await r.read(4))
        if sec == 0:
            raise RFBError(await _reason(r))
        if sec != 1:
            raise RFBError(f'VNC authentication required (type {sec}); only None is supported')
    else:
        (count,) = struct.unpack('>B', await r.read(1))
        if count == 0:
            raise RFBError(await _reason(r))
        types = await r.read(count)
        if 1 not in types:
            raise RFBError(f'no security type None offered ({list(types)})')
        await r.send(b'\x01')
        if version == (3, 8):
            (result,) = struct.unpack('>I', await r.read(4))
            if result != 0:
                raise RFBError(await _reason(r))
    await r.send(b'\x01')                                           # ClientInit: shared
    w, h = struct.unpack('>HH', await r.read(4))
    await r.read(16)                                                # server pixel format, replaced below
    name = await _reason(r)
    pixfmt = struct.pack('>BBBBHHHBBB3x', 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
    await r.send(struct.pack('>B3x', 0) + pixfmt)                   # SetPixelFormat
    await r.send(struct.pack('>BxH', 2, 2) + struct.pack('>ii', RAW, DESKTOP_SIZE))   # SetEncodings
    return w, h, name


def pointer(x: int, y: int, mask: int = 0) -> bytes:
    return struct.pack('>BBHH', 5, mask, max(0, x), max(0, y))


def key(keysym: int, down: bool) -> bytes:
    return struct.pack('>BBxxI', 4, 1 if down else 0, keysym)


def update_request(w: int, h: int, incremental: bool = False) -> bytes:
    return struct.pack('>BBHHHH', 3, 1 if incremental else 0, 0, 0, w, h)


async def read_frame(r: WsReader, w: int, h: int) -> Image.Image:
    """Request one full frame and read server messages until it has arrived."""
    await r.send(update_request(w, h))
    fb = Image.new('RGB', (w, h))
    while True:
        (mtype,) = struct.unpack('>B', await r.read(1))
        if mtype == 0:                                              # FramebufferUpdate
            _, nrects = struct.unpack('>BH', await r.read(3))
            resized = False
            for _ in range(nrects):
                x, y, rw, rh, enc = struct.unpack('>HHHHi', await r.read(12))
                if enc == RAW:
                    data = await r.read(rw * rh * 4)
                    if rw and rh:
                        fb.paste(Image.frombytes('RGB', (rw, rh), data, 'raw', 'BGRX'), (x, y))
                elif enc == DESKTOP_SIZE:
                    w, h, resized = rw, rh, True
                    fb = Image.new('RGB', (w, h))
                else:
                    raise RFBError(f'unexpected encoding {enc}')
            if resized:
                await r.send(update_request(w, h))
                continue
            return fb
        if mtype == 1:                                              # SetColourMapEntries
            _, _, n = struct.unpack('>BHH', await r.read(5))
            await r.read(n * 6)
        elif mtype == 2:                                            # Bell
            continue
        elif mtype == 3:                                            # ServerCutText
            await r.read(3)
            (n,) = struct.unpack('>I', await r.read(4))
            await r.read(n)
        else:
            raise RFBError(f'unexpected server message {mtype}')


async def screenshot(ws, wake: str = 'none', settle: float = 1.5, timeout: float = 15) -> Image.Image:
    """One frame from an open console websocket. `wake`: 'none', 'pointer' (move the pointer by one
    pixel and back) or 'shift' (press and release Shift), then wait `settle` seconds for the guest
    to redraw before the frame is taken."""
    r = WsReader(ws, timeout)
    w, h, _ = await handshake(r)
    if wake == 'pointer':
        await r.send(pointer(w // 2 + 1, h // 2) + pointer(w // 2, h // 2))
    elif wake == 'shift':
        await r.send(key(XK_SHIFT_L, True) + key(XK_SHIFT_L, False))
    if wake in ('pointer', 'shift'):
        await asyncio.sleep(settle)
    return await read_frame(r, w, h)


# ------------------------------------------------------------- relay helper
_SIZES = {3: 10, 4: 8, 5: 6}


def is_user_input(msg: bytes, handshake_done: bool) -> bool:
    """True when a browser -> console websocket message carries user input. noVNC asks for a
    screen update after every update it receives, so an idle console still sends a steady trickle
    of FramebufferUpdateRequests; those (alone or batched) are not activity. Everything else is:
    key and pointer events, clipboard, and the handshake itself."""
    if not handshake_done or not msg:
        return True
    i = 0
    while i < len(msg):
        if msg[i] != 3:
            return True
        i += _SIZES[3]
    return i != len(msg)


def first_message_type(msg: bytes) -> Optional[int]:
    return msg[0] if msg else None
