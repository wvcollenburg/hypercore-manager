"""Links in mails come from the configured "address in links" only, never from a request's Host header.

Before 1.8.1 an unauthenticated POST /account/forgot with `Host: evil.example` mailed the account holder a
genuine reset link pointing at the attacker whenever Settings -> Mail had no address in links (the default).
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

for _name in ('pyotp', 'qrcode', 'qrcode.image', 'qrcode.image.pil'):   # account_api imports user_db lazily
    sys.modules.setdefault(_name, types.ModuleType(_name))

from flask import Flask                                  # noqa: E402

import access_db                                         # noqa: E402
import account_api                                       # noqa: E402
import invite_api                                        # noqa: E402
import mailer                                            # noqa: E402
from access_db import AccessError                        # noqa: E402

TEMPLATES = Path(__file__).resolve().parent.parent / 'dashboard' / 'templates'


def _settings(public_url):
    return lambda with_secret=False: {'public_url': public_url, 'configured': True}


def test_link_base_is_the_setting_or_nothing(monkeypatch):
    monkeypatch.setattr(mailer, 'settings', _settings(None))
    assert mailer.link_base() is None
    monkeypatch.setattr(mailer, 'settings', _settings('https://hcm.example.com/'))
    assert mailer.link_base() == 'https://hcm.example.com'


def test_a_relay_without_the_address_in_links_is_refused():
    with pytest.raises(AccessError, match='address in links is required'):
        mailer.save({'host': 'relay.example', 'sender': 'hcm@example.com'}, 'admin')


@pytest.fixture
def forgot(monkeypatch):
    sent = []
    app = Flask('t', template_folder=str(TEMPLATES))
    app.secret_key = 'test'
    passthrough = lambda *a, **k: (lambda f: f)          # noqa: E731
    account_api.register(app, passthrough, passthrough, SimpleNamespace(me_id=lambda: None, actor=lambda: 'x'))
    monkeypatch.setattr(mailer, 'configured', lambda: True)
    monkeypatch.setattr(mailer, 'deliver', lambda to, kind, lang=None, by=None, **v: sent.append((to, kind, v)) or {'sent': True})
    monkeypatch.setattr(access_db, 'user_id_by_login', lambda e: 'u1')
    monkeypatch.setattr(access_db, 'user', lambda uid: {'user_id': 'u1', 'username': 'victim@x.example', 'enabled': True,
                                                         'awaiting_email': False})
    monkeypatch.setattr(account_api, '_recent_resets', lambda uid: 0)
    monkeypatch.setattr(account_api, '_new_token', lambda *a, **k: 'TOKEN')
    monkeypatch.setattr(account_api, '_audit', lambda *a, **k: None)
    return app.test_client(), sent


def test_forgot_never_mails_a_link_to_the_host_the_request_named(monkeypatch, forgot):
    client, sent = forgot
    monkeypatch.setattr(mailer, 'settings', _settings(None))
    r = client.post('/account/forgot', data={'email': 'victim@x.example'}, headers={'Host': 'evil.example'})
    assert r.status_code == 200 and sent == [], 'no address in links: no mail, never one built from Host'


def test_forgot_mails_the_configured_address_whatever_the_host(monkeypatch, forgot):
    client, sent = forgot
    monkeypatch.setattr(mailer, 'settings', _settings('https://hcm.example.com'))
    client.post('/account/forgot', data={'email': 'victim@x.example'}, headers={'Host': 'evil.example'})
    assert [s[2]['link'] for s in sent] == ['https://hcm.example.com/account/reset/TOKEN']


def test_an_invitation_without_the_address_is_not_mailed_but_shown_to_the_inviter(monkeypatch):
    sent = []
    monkeypatch.setattr(mailer, 'settings', _settings(None))
    monkeypatch.setattr(mailer, 'deliver', lambda *a, **k: sent.append(k) or {'sent': True, 'error': None})
    r = invite_api._send({'kind': 'activate', 'email': 'new@x.example', 'lang': 'en'}, 'TOK', 'http://hcm.lan:5000/', 'admin')
    assert sent == [] and not r['sent'] and r['link'] == 'http://hcm.lan:5000/invite/TOK'
    assert 'address in links' in r['error']
    monkeypatch.setattr(mailer, 'settings', _settings('https://hcm.example.com'))
    r = invite_api._send({'kind': 'activate', 'email': 'new@x.example', 'lang': 'en'}, 'TOK', 'http://evil.example/', 'admin')
    assert r['sent'] and [k['link'] for k in sent] == ['https://hcm.example.com/invite/TOK']
