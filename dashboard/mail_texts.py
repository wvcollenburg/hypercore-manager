"""
mail_texts.py — the texts of the mails HCM sends, in the dashboard's four languages (en, nl, de, fr).

Plain text only: it survives every mail client and relay, and nothing in it can be rendered differently
from what was meant. Each entry is (subject, body) with {placeholders}; mailer.render() fills them and
falls back to English for an unknown language. Add a kind in all four languages at once.
"""
from __future__ import annotations

LANGS = ('en', 'nl', 'de', 'fr')

TEXTS: dict[str, dict[str, tuple[str, str]]] = {
    'en': {
        'test': ("HyperCore Manager: test mail",
                 "This is a test mail from HyperCore Manager, sent by {actor}.\n\n"
                 "If you can read this, mail from HyperCore Manager reaches you.\n"),
    },
    'nl': {
        'test': ("HyperCore Manager: testmail",
                 "Dit is een testmail van HyperCore Manager, verstuurd door {actor}.\n\n"
                 "Als je dit kunt lezen, komt mail van HyperCore Manager bij je aan.\n"),
    },
    'de': {
        'test': ("HyperCore Manager: Testmail",
                 "Dies ist eine Testmail von HyperCore Manager, gesendet von {actor}.\n\n"
                 "Wenn Sie dies lesen können, erreicht Sie Mail von HyperCore Manager.\n"),
    },
    'fr': {
        'test': ("HyperCore Manager : e-mail de test",
                 "Ceci est un e-mail de test de HyperCore Manager, envoyé par {actor}.\n\n"
                 "Si vous pouvez lire ceci, les e-mails de HyperCore Manager vous parviennent.\n"),
    },
}
