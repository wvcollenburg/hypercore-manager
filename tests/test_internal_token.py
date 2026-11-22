"""/internal/proxy-image* want the backup token, also when none is configured (1.8.1).

They accepted every request when BACKUP_API_TOKEN was empty, on the dashboard's public port, and compared
the header with ==.
"""
from __future__ import annotations

import types


def test_no_token_configured_means_no_access(dash, monkeypatch):
    monkeypatch.delenv('BACKUP_API_TOKEN', raising=False)
    monkeypatch.delenv('EXPORTS_API_TOKEN', raising=False)
    c = dash.app.test_client()
    assert c.get('/internal/proxy-image/version').status_code == 401
    assert c.get('/internal/proxy-image/version', headers={'Authorization': 'Bearer '}).status_code == 401


def test_the_right_token_and_only_that(dash, monkeypatch):
    monkeypatch.setenv('BACKUP_API_TOKEN', 's3cret')
    monkeypatch.setattr(dash.subprocess, 'run', lambda *a, **k: types.SimpleNamespace(returncode=1, stdout='', stderr=''))
    c = dash.app.test_client()
    assert c.get('/internal/proxy-image/version', headers={'Authorization': 'Bearer wrong'}).status_code == 401
    assert c.get('/internal/proxy-image/version', headers={'Authorization': 'Bearer s3cret'}).status_code == 200
    src = (dash.__file__ and open(dash.__file__).read())
    assert 'hmac.compare_digest(auth' in src
