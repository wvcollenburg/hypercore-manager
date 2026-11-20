"""
mailer.py — HCM's one way to send mail (plans/PLAN-roles.md, "Accounts", E2).

One SMTP relay for the whole installation (config.mail_settings, set by an administrator of the whole
installation under Settings → Mail). Plain SMTP to a relay or a mail service with STARTTLS or TLS and an
optional login; Microsoft Graph is a later wish.

deliver() never raises for a mail problem: it reports {'sent': bool, 'error': str | None}, and audits a
failure, so a caller (an invitation) can hand the link to the person who sent it instead. Nothing here
logs a password or a message body.
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

import access_db
import hcm_crypto
import mail_texts
from access_db import AccessError

logger = logging.getLogger(__name__)

TIMEOUT = 15                      # seconds for the whole SMTP conversation's steps
SECURITY = ('starttls', 'tls', 'none')
_FIELDS = ('host', 'port', 'security', 'username', 'sender', 'sender_name', 'public_url')


class MailError(Exception):
    pass


# ── settings ────────────────────────────────────────────────────────────────────────────────────────
def settings(with_secret: bool = False) -> dict:
    """The mail settings; the password only as has_password unless with_secret (then decrypted)."""
    conn = access_db._connect()
    try:
        with conn.cursor() as cur:
            try:
                cur.execute('SELECT * FROM config.mail_settings WHERE id = 1')
                row = cur.fetchone()
            except Exception as exc:  # noqa: BLE001 — before migration 047
                if not access_db._missing(exc):
                    raise
                row = None
    finally:
        conn.close()
    row = dict(row) if row else {}
    out = {k: row.get(k) for k in _FIELDS}
    out['port'] = out['port'] or 587
    out['security'] = out['security'] or 'starttls'
    out['has_password'] = bool(row.get('password_enc'))
    out['configured'] = bool(out['host'] and out['sender'])
    out['updated_at'] = row['updated_at'].isoformat() if row.get('updated_at') else None
    out['updated_by'] = row.get('updated_by')
    if with_secret:
        out['password'] = hcm_crypto.decrypt(row['password_enc']) if row.get('password_enc') else None
    return out


def configured() -> bool:
    try:
        return settings()['configured']
    except Exception:  # noqa: BLE001 — no database: no mail
        return False


def save(body: dict, actor: str) -> dict:
    """Store the settings. A password field left out (or None) keeps the stored one; '' removes it."""
    host = str(body.get('host') or '').strip() or None
    if host and (len(host) > 253 or any(c.isspace() or c in '/@' for c in host)):
        raise AccessError('enter a host name or address, without a scheme')
    try:
        port = 587 if body.get('port') in (None, '') else int(body['port'])   # 0 is refused, not defaulted
    except (TypeError, ValueError):
        port = 0
    if not 1 <= port <= 65535:
        raise AccessError('the port must be between 1 and 65535')
    security = body.get('security') or 'starttls'
    if security not in SECURITY:
        raise AccessError('security must be starttls, tls or none')
    sender = access_db.normalise_email(body['sender']) if body.get('sender') else None
    if host and not sender:
        raise AccessError('a sender address is required')
    name = str(body.get('sender_name') or '').strip()[:100] or None
    if name and any(c in name for c in '\r\n'):
        raise AccessError('the sender name cannot contain line breaks')
    url = str(body.get('public_url') or '').strip().rstrip('/') or None
    if url and not (url.startswith('https://') or url.startswith('http://')) or (url and any(c.isspace() for c in url)):
        raise AccessError('the address in links must start with https:// (or http://)')
    username = str(body.get('username') or '').strip()[:200] or None
    conn = access_db._connect()
    try:
        with conn.cursor() as cur:
            cur.execute('''INSERT INTO config.mail_settings (id, host, port, security, username, sender, sender_name, public_url,
                                                             updated_at, updated_by)
                           VALUES (1, %s, %s, %s, %s, %s, %s, %s, NOW(), %s)
                           ON CONFLICT (id) DO UPDATE SET host = EXCLUDED.host, port = EXCLUDED.port,
                               security = EXCLUDED.security, username = EXCLUDED.username, sender = EXCLUDED.sender,
                               sender_name = EXCLUDED.sender_name, public_url = EXCLUDED.public_url,
                               updated_at = NOW(), updated_by = EXCLUDED.updated_by''',
                        (host, port, security, username, sender, name, url, actor))
            if body.get('password') is not None:
                pw = str(body['password'])
                cur.execute('UPDATE config.mail_settings SET password_enc = %s WHERE id = 1',
                            (hcm_crypto.encrypt(pw) if pw else None,))
            access_db.audit(cur, actor, 'mail.settings', host or '(mail off)',
                            detail={'port': port, 'security': security, 'sender': sender, 'login': bool(username)})
        conn.commit()
    finally:
        conn.close()
    return settings()


def link_base(request_host_url: str | None = None) -> str:
    """The address put in links: the setting, else the address this request came in on."""
    try:
        url = settings().get('public_url')
    except Exception:  # noqa: BLE001
        url = None
    return (url or (request_host_url or '')).rstrip('/')


# ── sending ─────────────────────────────────────────────────────────────────────────────────────────
def render(kind: str, lang: str | None, **values) -> tuple[str, str]:
    """(subject, body) of a mail kind in a language (English when unknown)."""
    texts = mail_texts.TEXTS.get((lang or 'en')[:2].lower()) or mail_texts.TEXTS['en']
    subject, body = texts.get(kind) or mail_texts.TEXTS['en'][kind]
    return subject.format(**values), body.format(**values)


def _send(cfg: dict, to: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg['From'] = formataddr((cfg.get('sender_name') or 'HyperCore Manager', cfg['sender']))
    msg['To'] = to
    msg['Subject'] = subject
    msg['Message-ID'] = make_msgid(domain=cfg['sender'].split('@', 1)[1])
    msg['Auto-Submitted'] = 'auto-generated'
    msg.set_content(body)
    ctx = ssl.create_default_context()
    try:
        if cfg['security'] == 'tls':
            smtp = smtplib.SMTP_SSL(cfg['host'], cfg['port'], timeout=TIMEOUT, context=ctx)
        else:
            smtp = smtplib.SMTP(cfg['host'], cfg['port'], timeout=TIMEOUT)
        with smtp:
            smtp.ehlo()
            if cfg['security'] == 'starttls':
                smtp.starttls(context=ctx)
                smtp.ehlo()
            if cfg.get('username'):
                smtp.login(cfg['username'], cfg.get('password') or '')
            smtp.send_message(msg)
    except smtplib.SMTPAuthenticationError as exc:
        raise MailError('the mail server refused the login') from exc
    except smtplib.SMTPNotSupportedError as exc:
        raise MailError(f'the mail server does not support this: {exc}') from exc
    except smtplib.SMTPRecipientsRefused as exc:
        raise MailError('the mail server refused the recipient') from exc
    except smtplib.SMTPException as exc:
        raise MailError(f'the mail server answered: {exc}') from exc
    except ssl.SSLError as exc:
        raise MailError(f'TLS failed: {exc.reason or exc}') from exc
    except OSError as exc:                   # DNS, refused, timeout
        raise MailError(f'cannot reach {cfg["host"]}:{cfg["port"]}: {exc.strerror or exc}') from exc


def deliver(to: str, kind: str, lang: str | None = None, by: str | None = None, **values) -> dict:
    """Send one mail of `kind` to `to`. {'sent': True} or {'sent': False, 'error': why}; never raises for
    a mail problem (no relay set, unreachable, refused), and audits a failure (by: who caused it)."""
    try:
        cfg = settings(with_secret=True)
    except Exception as exc:  # noqa: BLE001 — database, or a password the key cannot decrypt
        return _failed(to, kind, by, f'mail settings unreadable: {exc.__class__.__name__}')
    if not cfg['configured']:
        return {'sent': False, 'error': 'mail is not set up'}
    try:
        subject, body = render(kind, lang, **values)
        _send(cfg, to, subject, body)
    except MailError as exc:
        return _failed(to, kind, by, str(exc))
    return {'sent': True, 'error': None}


def _failed(to: str, kind: str, actor: str | None, why: str) -> dict:
    logger.warning('mail %s to %s failed: %s', kind, to, why)
    try:
        conn = access_db._connect()
        try:
            with conn.cursor() as cur:
                access_db.audit(cur, actor or 'system', 'mail.failed', to, detail={'kind': kind, 'error': why[:300]})
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — the audit is best effort here; the caller gets the error anyway
        pass
    return {'sent': False, 'error': why}
