"""The in-app updater's state and its failure paths (1.8.1).

Before: Apply wrote current_version=<new>, update_available=False right after moving the checkout, so a build
that failed or timed out, a failed migration or a failed restart container left "Up to date" on show with the
old containers running and no Apply button; a stash pop with conflicts went on to build with conflict
markers; the restart step started every non-database service, also ones the admin had stopped; docker run
--env-file parsed .env unlike Compose; and apply_in_progress, kept in a /tmp file by four workers, was never
reset, so a restart during the build left Apply answering 409 for good.
"""
from __future__ import annotations

import json
import subprocess
import types

import pytest

from test_updater_services import _restart_command


def test_the_restart_leaves_stopped_services_stopped(tmp_path):
    fake = tmp_path / 'docker-compose'
    fake.write_text('#!/bin/sh\n'
                    'case "$*" in\n'
                    '  "config --services") printf "influxdb\\ncollector\\nbalancer\\ndashboard\\nconsole\\npostgres\\n";;\n'
                    '  "ps --services --filter status=running") printf "collector\\ndashboard\\ninfluxdb\\n";;\n'
                    '  "ps -a --services") printf "collector\\nbalancer\\ndashboard\\ninfluxdb\\npostgres\\n";;\n'
                    '  *) echo "$@";;\n'
                    'esac\n')
    fake.chmod(0o755)
    out = subprocess.run(['sh', '-c', _restart_command()()], capture_output=True, text=True,
                         env={'PATH': f'{tmp_path}:/usr/bin:/bin'}).stdout.split()
    # balancer was stopped by the admin: not started; console has no container yet (new): started
    assert out == ['up', '-d', '--no-deps', 'collector', 'dashboard', 'console']


def test_no_env_file_for_the_helper_containers(repo):
    src = (repo / 'dashboard' / 'app.py').read_text()
    assert "'--env-file'" not in src


@pytest.fixture
def updater(dash, monkeypatch, tmp_path):
    state_file = tmp_path / 'hcm_update.json'
    monkeypatch.setattr(dash, '_UPDATE_FILE', str(state_file))
    monkeypatch.setattr(dash, '_restore_ownership', lambda log=None: None)
    monkeypatch.setattr(dash, '_read_version_file', lambda: '1.9.0')
    monkeypatch.setattr(dash, '_latest_remote_tag', lambda: 'v1.9.0')
    monkeypatch.setattr(dash, 'resolve_apply', lambda repo, tag: {'error': None, 'action': 'fast_forward', 'branch': 'master',
                                                                  'tag': 'v1.9.0', 'tag_version': '1.9.0'})
    monkeypatch.setattr(dash, 'move_to_tag', lambda repo, plan: (True, 'Now at v1.9.0.'))
    calls = []
    results = {}

    def run(cmd, **kw):
        calls.append(cmd)
        key = next((k for k in results if k in ' '.join(cmd)), None)
        rc, out = results.get(key, (0, ''))
        if rc == 'timeout':
            raise subprocess.TimeoutExpired(cmd, 600)
        return types.SimpleNamespace(returncode=rc, stdout=out, stderr=out)
    monkeypatch.setattr(dash.subprocess, 'run', run)

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(dash, '_pg', lambda: _Conn())
    import migrations
    monkeypatch.setattr(migrations, 'apply_pending', lambda conn, d: [])
    state_file.write_text(json.dumps({'current_version': '1.8.0', 'update_available': True, 'apply_in_progress': True}))
    read = lambda: json.loads(state_file.read_text())          # noqa: E731
    return dash, calls, results, read


def test_a_failed_build_keeps_the_old_version_and_apply_on_offer(updater):
    dash, calls, results, read = updater
    results['compose build'] = (1, 'no space left on device')
    dash._do_apply_update()
    s = read()
    assert s['current_version'] == '1.8.0' and s['update_available'] is True and s['apply_in_progress'] is False
    assert 'build failed' in s['apply_failed']
    results['compose build'] = ('timeout', '')
    dash._do_apply_update()
    assert read()['update_available'] is True and 'within 10 minutes' in read()['apply_failed']


def test_a_failed_restart_container_keeps_apply_on_offer(updater):
    dash, calls, results, read = updater
    results['hcm-auto-updater'] = (1, 'no such image')
    dash._do_apply_update()
    assert read()['update_available'] is True and read()['current_version'] == '1.8.0'


def test_the_new_version_is_shown_once_the_restart_is_on_its_way(updater):
    dash, calls, results, read = updater
    results['docker run'] = (0, 'abcdef0123456789')
    dash._do_apply_update()
    s = read()
    assert s['current_version'] == '1.9.0' and s['update_available'] is False and not s['apply_failed']


def test_conflicting_local_changes_stop_before_the_build(updater):
    dash, calls, results, read = updater
    results['stash pop'] = (1, 'CONFLICT (content): Merge conflict in docker-compose.yaml')
    results['git -C'] = (0, 'Saved working directory and index state')
    dash._do_apply_update()
    assert not any('build' in c for c in calls), 'nothing is built with conflict markers in the tree'
    assert 'conflict' in read()['apply_failed'] and read()['update_available'] is True


def test_an_interrupted_apply_does_not_block_apply_forever(updater, monkeypatch):
    dash, calls, results, read = updater
    (open(dash._UPDATE_FILE, 'w')).write(json.dumps({'apply_in_progress': True, 'apply_marker': 'an earlier life',
                                                     'apply_pid': 1, 'apply_started': 0}))
    assert dash._apply_is_stale(read())
    dash._clear_stale_apply()
    assert read()['apply_in_progress'] is False
    live = {'apply_in_progress': True, 'apply_marker': dash._container_marker(), 'apply_pid': dash.os.getpid(),
            'apply_started': dash.time.time()}
    assert not dash._apply_is_stale(live)
