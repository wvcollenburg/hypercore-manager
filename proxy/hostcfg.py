"""
hostcfg.py — settings the proxy makes on the VM it runs on (only on a proxy VM, never inside the HCM stack).
"""
from __future__ import annotations

import os

PRINTK = '/proc/sys/kernel/printk'
QUIET_CONSOLE_LEVEL = 3


def quiet_kernel_console(path: str = PRINTK, tls: str = None) -> tuple:
    """On a proxy VM, keep kernel errors off the VM's console. nbd prints a line for every I/O on a
    disconnected device ("block nbd6: Attempted send on invalid socket"), and on the text console they
    bury the login prompt: the open console session check cannot see whether the console is logged in.
    The first field of kernel.printk is the console log level; with 3 only critical messages and worse
    reach the console, everything stays in dmesg and the journal. Not inside the HCM stack itself
    (PROXY_TLS=off): there this privileged container would change the setting of a host that is not
    ours. Returns (before, after), or (None, None) when nothing was changed."""
    tls = (os.getenv('PROXY_TLS', 'auto') if tls is None else tls).strip().lower()
    if tls == 'off':
        return None, None
    try:
        with open(path) as f:
            before = f.read().split()
        if not before or int(before[0]) <= QUIET_CONSOLE_LEVEL:
            return ' '.join(before), ' '.join(before)
        after = [str(QUIET_CONSOLE_LEVEL)] + before[1:]
        with open(path, 'w') as f:
            f.write(' '.join(after))
        return ' '.join(before), ' '.join(after)
    except (OSError, ValueError):
        return None, None
