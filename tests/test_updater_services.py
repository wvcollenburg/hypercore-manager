"""The updater's restart step and the self-heal of services added after 1.1.x (1.2.1)."""
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _restart_command():
    src = (REPO / 'dashboard' / 'app.py').read_text()
    block = src[src.index('_APP_SERVICES = ('):src.index('# Services added after 1.1.x')]
    ns = {}
    exec(block, ns)
    return ns['restart_command']


def test_restart_command_takes_the_services_from_the_new_compose_file(tmp_path):
    """Run the shell command with a stand-in docker-compose: the services come from `config --services`,
    minus the databases; without output the fixed list is used."""
    fake = tmp_path / 'docker-compose'
    fake.write_text('#!/bin/sh\nif [ "$1" = config ]; then printf "influxdb\\ncollector\\npostgres\\nconsole\\nnewthing\\n"; else echo "$@"; fi\n')
    fake.chmod(0o755)
    cmd = _restart_command()()
    out = subprocess.run(['sh', '-c', cmd], capture_output=True, text=True, env={'PATH': f'{tmp_path}:/usr/bin:/bin'}).stdout.split()
    assert out[:3] == ['up', '-d', '--no-deps'] and out[3:] == ['collector', 'console', 'newthing']
    fake.write_text('#!/bin/sh\nif [ "$1" = config ]; then exit 1; else echo "$@"; fi\n')
    out = subprocess.run(['sh', '-c', cmd], capture_output=True, text=True, env={'PATH': f'{tmp_path}:/usr/bin:/bin'}).stdout.split()
    assert 'console' in out and 'proxy' in out and 'postgres' not in out, 'fallback: the fixed list'


def test_self_heal_is_limited_to_services_newer_than_1_1():
    src = (REPO / 'dashboard' / 'app.py').read_text()
    m = re.search(r"SELF_HEAL_SERVICES = \((.*?)\)", src)
    assert set(re.findall(r"'([a-z]+)'", m.group(1))) == {'console'}
