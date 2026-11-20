"""The mailer (plans/PLAN-roles.md, "Accounts", E2): texts in four languages, what goes over SMTP, and
that a mail problem is reported, never raised."""
from __future__ import annotations

import string
import smtplib

import pytest

import mail_texts
import mailer


def test_every_kind_in_every_language_with_the_same_placeholders():
    fields = lambda s: {f for _, f, _, _ in string.Formatter().parse(s) if f}  # noqa: E731
    en = mail_texts.TEXTS['en']
    for lang in mail_texts.LANGS:
        texts = mail_texts.TEXTS[lang]
        assert set(texts) == set(en), lang
        for kind, (subject, body) in texts.items():
            assert fields(subject) | fields(body) == fields(en[kind][0]) | fields(en[kind][1]), (lang, kind)
            assert '\n' not in subject, (lang, kind)


def test_render_falls_back_to_english():
    assert mailer.render('test', 'xx', actor='a')[0] == mail_texts.TEXTS['en']['test'][0]
    assert mailer.render('test', 'nl-NL', actor='a')[0] == mail_texts.TEXTS['nl']['test'][0]


class _FakeSMTP:
    sent: list = []
    calls: list = []

    def __init__(self, host, port, timeout=None, context=None):
        self.calls.append(('connect', host, port, timeout))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def ehlo(self):
        self.calls.append(('ehlo',))

    def starttls(self, context=None):
        self.calls.append(('starttls',))

    def login(self, user, pw):
        self.calls.append(('login', user))
        if pw != 'right':
            raise smtplib.SMTPAuthenticationError(535, b'no')

    def send_message(self, msg):
        self.sent.append(msg)


@pytest.fixture()
def fake_smtp(monkeypatch):
    _FakeSMTP.sent, _FakeSMTP.calls = [], []
    monkeypatch.setattr(mailer.smtplib, 'SMTP', _FakeSMTP)
    monkeypatch.setattr(mailer.smtplib, 'SMTP_SSL', _FakeSMTP)
    monkeypatch.setattr(mailer, '_failed', lambda to, kind, by, why: {'sent': False, 'error': why})
    return _FakeSMTP


def _cfg(**kw):
    base = {'configured': True, 'host': 'relay.example', 'port': 587, 'security': 'starttls', 'username': 'hcm',
            'password': 'right', 'sender': 'hcm@example.com', 'sender_name': None}
    base.update(kw)
    return base


def test_deliver_sends_over_starttls_with_login(monkeypatch, fake_smtp):
    monkeypatch.setattr(mailer, 'settings', lambda with_secret=False: _cfg())
    assert mailer.deliver('jsmith@contoso.example', 'test', 'de', actor='admin') == {'sent': True, 'error': None}
    kinds = [c[0] for c in fake_smtp.calls]
    assert kinds.index('starttls') < kinds.index('login'), 'never a login before TLS'
    msg = fake_smtp.sent[0]
    assert msg['To'] == 'jsmith@contoso.example' and 'HyperCore Manager' in msg['From']
    assert msg['Subject'] == mail_texts.TEXTS['de']['test'][0] and msg['Auto-Submitted'] == 'auto-generated'


def test_no_login_without_a_username_and_no_starttls_on_none(monkeypatch, fake_smtp):
    monkeypatch.setattr(mailer, 'settings', lambda with_secret=False: _cfg(security='none', username=None, port=25))
    assert mailer.deliver('a@b.example', 'test', actor='x')['sent']
    assert not any(c[0] in ('starttls', 'login') for c in fake_smtp.calls)


def test_a_mail_problem_is_reported_not_raised(monkeypatch, fake_smtp):
    monkeypatch.setattr(mailer, 'settings', lambda with_secret=False: _cfg(password='wrong'))
    r = mailer.deliver('a@b.example', 'test', actor='x')
    assert r == {'sent': False, 'error': 'the mail server refused the login'}
    monkeypatch.setattr(mailer, 'settings', lambda with_secret=False: dict(_cfg(), configured=False))
    assert mailer.deliver('a@b.example', 'test', actor='x') == {'sent': False, 'error': 'mail is not set up'}

    def unreachable(*a, **k):
        raise OSError(111, 'Connection refused')
    monkeypatch.setattr(mailer.smtplib, 'SMTP', unreachable)
    monkeypatch.setattr(mailer, 'settings', lambda with_secret=False: _cfg())
    r = mailer.deliver('a@b.example', 'test', actor='x')
    assert not r['sent'] and 'relay.example:587' in r['error'] and 'Connection refused' in r['error']


def test_a_header_cannot_be_injected(monkeypatch, fake_smtp):
    monkeypatch.setattr(mailer, 'settings', lambda with_secret=False: _cfg())
    with pytest.raises(ValueError):
        mailer._send(_cfg(), 'a@b.example\r\nBcc: evil@x.example', 'subject', 'body')
