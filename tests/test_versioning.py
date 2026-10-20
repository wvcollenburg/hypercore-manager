"""dashboard/versioning.py — semver helpers used by the in-app updater."""
import pytest

from versioning import semver_key, latest_semver_tag, is_newer


@pytest.mark.parametrize('v, expected', [
    ('1.2.3',        (1, 2, 3, (1, ''))),
    ('v1.2.3',       (1, 2, 3, (1, ''))),
    (' v0.9.2 ',     (0, 9, 2, (1, ''))),
    ('0.9.0-rc.1',   (0, 9, 0, (0, 'rc.1'))),
    ('v10.0.1-beta', (10, 0, 1, (0, 'beta'))),
])
def test_semver_key_parses(v, expected):
    assert semver_key(v) == expected


@pytest.mark.parametrize('v', ['', 'latest', '1.2', 'v1', '1.2.3.4', 'release-1', None])
def test_semver_key_rejects_garbage(v):
    assert semver_key(v) is None


def test_prerelease_sorts_below_release():
    assert semver_key('0.9.0-rc.1') < semver_key('0.9.0')
    assert semver_key('0.9.0') < semver_key('0.9.1')


def test_numeric_not_lexicographic():
    assert semver_key('0.10.0') > semver_key('0.9.9')
    assert semver_key('2.0.0') > semver_key('1.99.99')


def test_latest_semver_tag_picks_highest_and_ignores_noise():
    tags = ['v0.9.0', 'v0.9.2', 'v0.9.1', 'nightly', '', 'v0.10.0-rc.1', 'v0.10.0']
    assert latest_semver_tag(tags) == 'v0.10.0'


def test_latest_semver_tag_prefers_release_over_prerelease():
    assert latest_semver_tag(['v1.0.0-rc.2', 'v1.0.0']) == 'v1.0.0'
    assert latest_semver_tag(['v1.0.0-rc.2', 'v0.9.9']) == 'v1.0.0-rc.2'


def test_latest_semver_tag_empty():
    assert latest_semver_tag([]) == ''
    assert latest_semver_tag(['main', 'foo']) == ''


@pytest.mark.parametrize('remote, current, expected', [
    ('0.9.3', '0.9.2', True),
    ('v0.9.3', '0.9.2', True),
    ('0.9.2', '0.9.2', False),
    ('0.9.1', '0.9.2', False),
    ('1.0.0', '0.9.2-rc.1', True),
    ('', '0.9.2', False),
    ('0.9.3', '', False),
    ('garbage', '0.9.2', False),
])
def test_is_newer(remote, current, expected):
    assert is_newer(remote, current) is expected


def test_version_file_is_valid_semver(repo):
    assert semver_key((repo / 'VERSION').read_text()) is not None
