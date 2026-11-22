"""A minimal SMTP server for the integration tests: it accepts every mail and keeps it in memory.

Plain SMTP only (EHLO/HELO, MAIL, RCPT, DATA, RSET, NOOP, QUIT); no TLS, no login. The integration test
runs it in a thread and gives its container a network alias, so the dashboard can deliver to it.
"""
from __future__ import annotations

import email
import email.policy
import socketserver
import threading


class _Handler(socketserver.StreamRequestHandler):
    def _say(self, line: str) -> None:
        self.wfile.write((line + '\r\n').encode())

    def handle(self):
        self._say('220 sink ESMTP')
        rcpt, sender = [], None
        while True:
            raw = self.rfile.readline(4096)
            if not raw:
                return
            cmd = raw.decode(errors='replace').strip()
            verb = cmd[:4].upper()
            if verb == 'EHLO':
                self.wfile.write(b'250-sink\r\n250 8BITMIME\r\n')
            elif verb == 'HELO':
                self._say('250 sink')
            elif verb == 'MAIL':
                sender, rcpt = cmd.split(':', 1)[1].strip(), []
                self._say('250 ok')
            elif verb == 'RCPT':
                rcpt.append(cmd.split(':', 1)[1].strip().strip('<>'))
                self._say('250 ok')
            elif verb == 'DATA':
                self._say('354 go on')
                lines = []
                while True:
                    line = self.rfile.readline(1 << 20)
                    if not line or line in (b'.\r\n', b'.\n'):
                        break
                    lines.append(line[1:] if line.startswith(b'..') else line)
                msg = email.message_from_bytes(b''.join(lines), policy=email.policy.default)
                self.server.mails.append({'from': sender, 'to': rcpt, 'msg': msg,
                                          'subject': str(msg['Subject']), 'body': msg.get_content()})
                self._say('250 queued')
            elif verb == 'RSET':
                rcpt, sender = [], None
                self._say('250 ok')
            elif verb == 'NOOP':
                self._say('250 ok')
            elif verb == 'QUIT':
                self._say('221 bye')
                return
            else:
                self._say('502 not here')


class Sink(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, port: int = 2525):
        super().__init__(('0.0.0.0', port), _Handler)
        self.mails: list[dict] = []
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)

    def start(self) -> 'Sink':
        self._thread.start()
        return self

    def stop(self) -> None:
        self.shutdown()
        self.server_close()

    def to(self, address: str) -> list[dict]:
        return [m for m in self.mails if address in m['to']]

    def wait_to(self, address: str, n: int = 1, timeout: float = 15) -> list[dict]:
        """The mails to `address` once there are at least `n` (mails sent in the background, such as a reset)."""
        import time
        end = time.monotonic() + timeout
        while len(self.to(address)) < n and time.monotonic() < end:
            time.sleep(0.2)
        return self.to(address)
