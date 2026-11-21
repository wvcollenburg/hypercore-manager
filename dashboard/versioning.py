"""
versioning.py — semver helpers shared by the in-app updater.

Kept free of Flask/DB imports so the logic is unit-testable on its own.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

SEMVER_RE = re.compile(r'^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$')


def semver_key(v: str) -> Optional[tuple]:
    """Sort key for semver-like version strings ('1.2.3', 'v1.2.3', '1.2.3-rc.1').

    Pre-release suffixes sort *below* the matching release per the SemVer spec,
    so v0.9.0-rc.1 < v0.9.0. Returns None for unparseable input.
    """
    if v is None:
        return None
    m = SEMVER_RE.match(v.strip())
    if not m:
        return None
    major, minor, patch, pre = m.groups()
    pre_key = (0, pre) if pre else (1, '')
    return (int(major), int(minor), int(patch), pre_key)


def latest_semver_tag(tags: Iterable[str]) -> str:
    """Return the highest semver-like tag from *tags*, or '' if none parse."""
    valid = [(t.strip(), semver_key(t)) for t in tags if t and t.strip()]
    valid = [(t, k) for t, k in valid if k is not None]
    if not valid:
        return ''
    valid.sort(key=lambda x: x[1])
    return valid[-1][0]


def is_newer(remote: str, current: str) -> bool:
    """True when *remote* parses as a strictly higher version than *current*."""
    rk, ck = semver_key(remote or ''), semver_key(current or '')
    return bool(rk and ck and rk > ck)


def origin_to_web_url(origin: str) -> str:
    """The browsable URL of a git remote: 'git@github.com:org/repo.git' and
    'ssh://git@github.com/org/repo.git' become 'https://github.com/org/repo'; an https
    remote loses its '.git' and any credentials. '' when the remote is empty."""
    o = (origin or '').strip()
    if not o:
        return ''
    m = re.match(r'^(?:ssh://)?(?:[\w.-]+@)?([\w.-]+)[:/](.+?)(?:\.git)?/?$', o) if '://' not in o or o.startswith('ssh://') else None
    if m:
        return f'https://{m.group(1)}/{m.group(2)}'
    m = re.match(r'^(https?)://(?:[^@/]+@)?([^/]+)/(.+?)(?:\.git)?/?$', o)
    if m:
        return f'{m.group(1)}://{m.group(2)}/{m.group(3)}'
    return o


# ---------------------------------------------------------------------------------------------------
# Applying an update: exactly the advertised release tag, never "whatever the branch has now"
# ---------------------------------------------------------------------------------------------------
# The check advertises the newest semver tag; the apply used to run `git pull` of the checked-out
# branch. On a branch GitHub deleted after its merge that failed ("no such ref was fetched"); on master
# it also pulled commits made after the tag; and a tag whose commit still carried the old VERSION
# (1.3.0, 2026-11-21) made every install offer the same update again after applying it. The apply now
# moves the checkout to the tag's commit only, after these checks, and changes nothing when one fails.

def plan_apply(branch: str, default_branch: str, tag: str, tag_version: str,
               head_in_tag: bool, tag_in_head: bool) -> dict:
    """What applying `tag` should do to a checkout, from facts git reports (no git calls here).

    branch: the checked-out branch, or 'HEAD' when detached. default_branch: the remote's default
    branch (normally 'master'). tag_version: the VERSION file at the tag. head_in_tag: the checkout's
    commit is an ancestor of the tag (or the same). tag_in_head: the tag is already contained.

    Returns {'action': 'fast_forward' | 'detach' | 'nothing', 'error': None} or
    {'action': None, 'error': why} — the error is shown to the administrator as is."""
    if not tag:
        return {'action': None, 'error': 'no release tag was found on the remote'}
    want = tag[1:] if tag.startswith('v') else tag
    if (tag_version or '').strip() != want:
        return {'action': None, 'error': f'release {tag} is inconsistent: its VERSION file says '
                                         f'{(tag_version or "nothing").strip() or "nothing"}; nothing was changed. '
                                         'Report this to the maintainers.'}
    if tag_in_head and not head_in_tag:
        return {'action': 'nothing', 'error': None}          # already beyond the release
    if branch not in ('HEAD', default_branch):
        return {'action': None, 'error': f'this installation follows branch "{branch}", not "{default_branch}". '
                                         f'Switch it with: git checkout {default_branch} (in the installation '
                                         'directory), then apply the update again. Nothing was changed.'}
    if not head_in_tag:
        return {'action': None, 'error': f'this installation has commits that are not part of {tag} (local '
                                         'changes or another history); nothing was changed. Resolve them by '
                                         f'hand, for example: git status, then git merge --ff-only {tag}.'}
    if tag_in_head:
        return {'action': 'nothing', 'error': None}          # already exactly at the release
    return {'action': 'fast_forward' if branch == default_branch else 'detach', 'error': None}


def _git_ok(repo: str, *args, timeout: float = 30) -> tuple[bool, str]:
    import subprocess
    r = subprocess.run(['git', '-C', repo, *args], capture_output=True, text=True, timeout=timeout)
    return r.returncode == 0, (r.stdout if r.returncode == 0 else (r.stderr or r.stdout)).strip()


def resolve_apply(repo: str, tag: str) -> dict:
    """plan_apply() for the checkout at `repo` and release `tag` (local tags must mirror the remote:
    run `git fetch --tags --force` first). Adds the facts it used, for the log."""
    ok, branch = _git_ok(repo, 'rev-parse', '--abbrev-ref', 'HEAD')
    branch = branch if ok else 'HEAD'
    ok, ref = _git_ok(repo, 'symbolic-ref', '--quiet', '--short', 'refs/remotes/origin/HEAD')
    default = ref.split('/', 1)[1] if ok and '/' in ref else 'master'
    tag_version = ''
    if tag:
        ok, out = _git_ok(repo, 'show', f'{tag}:VERSION')
        tag_version = out if ok else ''
    head_in_tag = bool(tag) and _git_ok(repo, 'merge-base', '--is-ancestor', 'HEAD', tag)[0]
    tag_in_head = bool(tag) and _git_ok(repo, 'merge-base', '--is-ancestor', tag, 'HEAD')[0]
    plan = plan_apply(branch, default, tag, tag_version, head_in_tag, tag_in_head)
    return dict(plan, branch=branch, default_branch=default, tag=tag, tag_version=tag_version.strip())


def move_to_tag(repo: str, plan: dict) -> tuple[bool, str]:
    """Carry out a plan from resolve_apply(): fast-forward the default branch to the tag, or check the tag
    out detached when the checkout was detached already (an install pinned to a release)."""
    if plan.get('action') == 'nothing':
        return True, f"already at {plan['tag']}"
    if plan.get('action') == 'fast_forward':
        return _git_ok(repo, 'merge', '--ff-only', plan['tag'], timeout=120)
    if plan.get('action') == 'detach':
        return _git_ok(repo, 'checkout', '--detach', plan['tag'], timeout=120)
    return False, plan.get('error') or 'nothing to do'
