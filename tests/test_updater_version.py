"""The version shown is the one that runs (1.8.1).

The updater read VERSION from the checkout at its last check: a stack showed 1.7.4 on a 1.8.0 checkout for a
day, and a checkout moved to a release whose build failed showed the new number while the old containers ran.
The image now carries its VERSION, which is reported as installed; Apply is offered while the checkout is ahead.
"""
from __future__ import annotations

import json
import re
import types

import pytest


def test_the_image_carries_its_version(repo):
    docker = (repo / 'dashboard' / 'Dockerfile').read_text()
    assert re.search(r'^COPY VERSION \.$', docker, re.M)
    ignore = (repo / '.dockerignore').read_text().split()
    assert 'VERSION' not in ignore and '*' not in ignore


@pytest.fixture
def check(dash, monkeypatch, tmp_path):
    monkeypatch.setattr(dash, '_UPDATE_FILE', str(tmp_path / 'u.json'))
    monkeypatch.setattr(dash, '_restore_ownership', lambda log=None: None)
    monkeypatch.setattr(dash.subprocess, 'run', lambda *a, **k: types.SimpleNamespace(returncode=0, stdout='', stderr=''))
    monkeypatch.setattr(dash, '_changelog_between', lambda a, b: [])
    monkeypatch.setattr(dash, '_origin_url', lambda: '')
    monkeypatch.setattr(dash, 'resolve_apply', lambda repo, tag: {'error': None})

    def run(image, checkout, tag):
        monkeypatch.setattr(dash, '_image_version', lambda: image)
        monkeypatch.setattr(dash, '_read_version_file', lambda: checkout)
        monkeypatch.setattr(dash, '_latest_remote_tag', lambda: tag)
        dash._check_for_update()
        return json.loads((tmp_path / 'u.json').read_text())
    return run


def test_the_running_image_is_what_is_installed(check):
    s = check('1.7.4', '1.8.0', 'v1.8.0')
    assert s['current_version'] == '1.7.4' and s['checkout_version'] == '1.8.0'
    assert s['update_available'] is True and s['remote_version'] == '1.8.0'


def test_a_checkout_ahead_of_the_image_offers_a_rebuild(check):
    s = check('1.8.0', '1.8.1', 'v1.8.0')                  # e.g. a manual git pull without a rebuild
    assert s['update_available'] is True and s['rebuild_needed'] is True and s['remote_version'] == '1.8.1'


def test_up_to_date_when_image_checkout_and_release_agree(check):
    s = check('1.8.1', '1.8.1', 'v1.8.1')
    assert s['update_available'] is False and s['rebuild_needed'] is False


def test_an_image_from_before_falls_back_to_the_checkout(check):
    s = check('', '1.8.0', 'v1.8.0')
    assert s['current_version'] == '1.8.0' and s['update_available'] is False
