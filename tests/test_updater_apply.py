"""The updater applies exactly the advertised release tag (dashboard/versioning.py: plan_apply, resolve_apply,
move_to_tag), against real git repositories: a remote with releases and installations cloned from it.

History of the problem: 'Apply update' ran `git pull` of whatever branch was checked out. On a branch deleted
after its merge that failed; on master it also took commits made after the tag; and a 1.3.0 tag whose commit
still carried VERSION 1.2.1 made installs offer the same update again after applying it."""
import shutil
import subprocess

import pytest

import versioning as V

pytestmark = pytest.mark.skipif(shutil.which('git') is None, reason='git is not installed in this test environment')


def git(repo, *args):
    r = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True)
    assert r.returncode == 0, (args, r.stderr)
    return r.stdout.strip()


def commit(repo, version=None, note='change'):
    if version is not None:
        (repo / 'VERSION').write_text(version + '\n')
    (repo / 'notes.txt').write_text((repo / 'notes.txt').read_text() + note + '\n' if (repo / 'notes.txt').exists() else note + '\n')
    git(repo, 'add', '-A')
    git(repo, '-c', 'user.name=t', '-c', 'user.email=t@example.com', 'commit', '-q', '-m', note)
    return git(repo, 'rev-parse', 'HEAD')


def tag(repo, name):
    git(repo, '-c', 'user.name=t', '-c', 'user.email=t@example.com', 'tag', '-a', name, '-m', name)


@pytest.fixture
def world(tmp_path):
    """upstream: master with releases v1.0.0 and v1.1.0; an installation cloned at v1.0.0's time."""
    up = tmp_path / 'upstream'
    up.mkdir()
    git(up, 'init', '-q', '-b', 'master')
    commit(up, '1.0.0', 'release 1.0.0'); tag(up, 'v1.0.0')
    install = tmp_path / 'install'
    subprocess.run(['git', 'clone', '-q', str(up), str(install)], check=True)
    commit(up, None, 'feature'); commit(up, '1.1.0', 'release 1.1.0'); tag(up, 'v1.1.0')
    git(install, 'fetch', '--tags', '--force', '--quiet')
    return up, install


def newest(repo):
    return V.latest_semver_tag(git(repo, 'tag', '-l').splitlines())


def test_master_behind_the_release_fast_forwards_to_exactly_the_tag(world):
    up, install = world
    after_tag = commit(up, None, 'merged after the release')          # must NOT be taken
    git(install, 'fetch', '--tags', '--force', '--quiet')
    plan = V.resolve_apply(str(install), newest(install))
    assert plan['action'] == 'fast_forward' and plan['error'] is None and plan['tag'] == 'v1.1.0'
    ok, _ = V.move_to_tag(str(install), plan)
    assert ok
    assert git(install, 'rev-parse', 'HEAD') == git(install, 'rev-parse', 'v1.1.0^{commit}')
    assert git(install, 'rev-parse', 'HEAD') != after_tag
    assert (install / 'VERSION').read_text().strip() == '1.1.0'
    assert git(install, 'rev-parse', '--abbrev-ref', 'HEAD') == 'master', 'still on master, for the next update'


def test_an_installation_pinned_to_a_release_moves_to_the_new_one_detached(world):
    _, install = world
    git(install, 'checkout', '-q', '--detach', 'v1.0.0')
    plan = V.resolve_apply(str(install), newest(install))
    assert plan['action'] == 'detach'
    assert V.move_to_tag(str(install), plan)[0]
    assert git(install, 'rev-parse', 'HEAD') == git(install, 'rev-parse', 'v1.1.0^{commit}')


def test_another_branch_is_refused_and_nothing_changes(world):
    _, install = world
    git(install, 'checkout', '-q', '-b', 'feature/old-work')
    before = git(install, 'rev-parse', 'HEAD')
    plan = V.resolve_apply(str(install), newest(install))
    assert plan['action'] is None and 'feature/old-work' in plan['error'] and 'git checkout master' in plan['error']
    assert not V.move_to_tag(str(install), plan)[0]
    assert git(install, 'rev-parse', 'HEAD') == before


def test_local_commits_are_refused(world):
    _, install = world
    commit(install, None, 'a local tweak')
    before = git(install, 'rev-parse', 'HEAD')
    plan = V.resolve_apply(str(install), newest(install))
    assert plan['action'] is None and 'not part of v1.1.0' in plan['error']
    assert git(install, 'rev-parse', 'HEAD') == before


def test_a_release_whose_version_file_disagrees_is_refused(world):
    """The 1.3.0 case: tagged on a commit that still said the old version."""
    up, install = world
    commit(up, None, 'forgot the bump'); tag(up, 'v1.2.0')            # VERSION still 1.1.0
    git(install, 'fetch', '--tags', '--force', '--quiet')
    plan = V.resolve_apply(str(install), newest(install))
    assert plan['tag'] == 'v1.2.0' and plan['action'] is None
    assert 'inconsistent' in plan['error'] and '1.1.0' in plan['error']


def test_already_at_or_beyond_the_release_changes_nothing(world):
    up, install = world
    git(install, 'merge', '-q', '--ff-only', 'v1.1.0')
    assert V.resolve_apply(str(install), 'v1.1.0')['action'] == 'nothing'
    git(install, 'fetch', '-q', 'origin')
    commit(up, None, 'after the release'); git(install, 'fetch', '-q', 'origin'); git(install, 'merge', '-q', '--ff-only', 'origin/master')
    plan = V.resolve_apply(str(install), 'v1.1.0')
    assert plan['action'] == 'nothing' and V.move_to_tag(str(install), plan)[0]


def test_a_tag_moved_on_the_remote_is_followed_after_a_forced_fetch(world):
    """1.3.1: the check fetches tags with --force; the apply then targets the tag as the remote has it now."""
    up, install = world
    new = commit(up, '1.1.0', 'fix on top, same version')
    git(up, 'tag', '-d', 'v1.1.0'); tag(up, 'v1.1.0')                 # moved (never do this on a published tag)
    git(install, 'fetch', '--tags', '--force', '--quiet')
    plan = V.resolve_apply(str(install), 'v1.1.0')
    assert plan['action'] == 'fast_forward' and V.move_to_tag(str(install), plan)[0]
    assert git(install, 'rev-parse', 'HEAD') == new


@pytest.mark.parametrize('branch, head_in, tag_in, action, err', [
    ('master', True, False, 'fast_forward', None),
    ('HEAD', True, False, 'detach', None),
    ('master', True, True, 'nothing', None),
    ('master', False, True, 'nothing', None),
    ('main', True, False, None, 'follows branch'),
    ('master', False, False, None, 'not part of'),
])
def test_plan_apply_rules(branch, head_in, tag_in, action, err):
    p = V.plan_apply(branch, 'master', 'v2.0.0', '2.0.0', head_in, tag_in)
    assert p['action'] == action and ((err is None and p['error'] is None) or err in p['error'])
    assert V.plan_apply('master', 'master', '', '', True, False)['error'].startswith('no release tag')


def test_the_updater_no_longer_pulls(repo):
    src = (repo / 'dashboard' / 'app.py').read_text()
    body = src.split('def _do_apply_update')[1].split('@app.route')[0]
    assert "'pull'" not in body, 'apply moves to the advertised tag, never git pull of the branch'
    assert 'resolve_apply(_REPO_PATH, _latest_remote_tag())' in body and 'move_to_tag(_REPO_PATH, plan)' in body
