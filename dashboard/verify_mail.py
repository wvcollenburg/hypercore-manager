"""
verify_mail.py — mails about failed and overdue backup verifications (plans/PLAN-chain-verify.md).

The backup service records each new failure (and each chain not verified for twice its folder's interval)
as a row in backup.verify_alerts. It has no idea who the people are: that is the dashboard's tenant tree.
So a thread here (one per gunicorn worker; a row is claimed atomically, so it is mailed once) collects the
new rows every minute, waits two minutes so a burst becomes one mail, and mails them per folder to the
**closest admin**: whoever holds the Admin role nearest the folder, so the folder's own admins, else the
customer's, else the MSP's, else the installation's. A level only counts when someone there can be mailed
(an enabled account with a confirmed email address).

Nothing is retried: a mail that cannot be sent is recorded on the row (mail_result) and the audit log; the
alert stays on the cluster card either way.
"""
from __future__ import annotations

import logging
import threading
import time

import access_db
import mailer
import rbac

logger = logging.getLogger(__name__)

GATHER_SECONDS = 120          # a new alert waits this long, so the rest of a night's failures join its mail
POLL_SECONDS = 60
MAX_LINES = 50


def closeness(g: rbac.Grant, tree: rbac.Tree, folder_id: str | None) -> int | None:
    """How near an Admin grant sits to a folder (higher is nearer); None: not an admin of it."""
    if not rbac.ALL <= g.perms:
        return None
    if g.kind == 'root':
        return 0
    if g.kind == 'node' and folder_id and g.node_id in tree.ancestors(folder_id):
        return 10 + len(tree.ancestors(g.node_id))
    return None


def closest_admins(candidates, tree: rbac.Tree, folder_id: str | None) -> list[dict]:
    """candidates: [(user, [Grant])] of accounts that can be mailed. The users at the nearest level."""
    best, out = None, []
    for user, grants in candidates:
        near = max((c for c in (closeness(g, tree, folder_id) for g in grants) if c is not None), default=None)
        if near is None:
            continue
        if best is None or near > best:
            best, out = near, [user]
        elif near == best:
            out.append(user)
    return out


def _mailable_candidates():
    return [(u, access_db.grants_of_principal(user_id=u['user_id'])) for u in access_db.users()
            if u['enabled'] and not u.get('awaiting_email') and not u.get('awaiting_access') and '@' in (u['username'] or '')]


def claim(conn) -> list[dict]:
    """Take the open, unclaimed alerts older than the gather time (no other worker gets them)."""
    with conn.cursor() as cur:
        cur.execute('''UPDATE backup.verify_alerts SET mail_claimed_at = NOW()
                        WHERE id IN (SELECT id FROM backup.verify_alerts
                                      WHERE mail_claimed_at IS NULL AND resolved_at IS NULL
                                        AND created_at < NOW() - make_interval(secs => %s)
                                      ORDER BY id FOR UPDATE SKIP LOCKED)
                    RETURNING id, kind, folder_id::text AS folder_id, cluster_id::text AS cluster_id, vm_name, seq,
                              detail, created_at''', (GATHER_SECONDS,))
        rows = [dict(r) for r in cur.fetchall()]
    conn.commit()
    return rows


def _record(conn, ids: list[int], result: str, sent: bool) -> None:
    with conn.cursor() as cur:
        cur.execute('UPDATE backup.verify_alerts SET mailed_at = CASE WHEN %s THEN NOW() END, mail_result = %s WHERE id = ANY(%s)',
                    (sent, result[:500], ids))
    conn.commit()


def _names(conn, rows: list[dict]) -> tuple[dict, dict]:
    with conn.cursor() as cur:
        cur.execute('SELECT folder_id::text AS id, name FROM config.folders')
        folders = {r['id']: r['name'] for r in cur.fetchall()}
        cur.execute('SELECT cluster_id::text AS id, cluster_name FROM config.clusters')
        clusters = {r['id']: r['cluster_name'] for r in cur.fetchall()}
    return folders, clusters


def line(a: dict, clusters: dict) -> str:
    where = clusters.get(a.get('cluster_id') or '', '?')
    point = f" #{a['seq']}" if a.get('seq') is not None else ''
    return f"- {a.get('vm_name') or '?'}{point} ({where}): {a.get('detail') or ''}"


def compose(rows: list[dict], folder_name: str, clusters: dict, link: str, lang: str = 'en') -> list[tuple[str, dict]]:
    """[(mail kind, values)] for one folder's alerts: one mail for failures, one for overdue chains."""
    out = []
    for kind, mail_kind in (('failed', 'verify_failed'), ('overdue', 'verify_overdue')):
        mine = [a for a in rows if a['kind'] == kind]
        if not mine:
            continue
        lines = [line(a, clusters) for a in mine[:MAX_LINES]]
        if len(mine) > MAX_LINES:
            lines.append(f'(+{len(mine) - MAX_LINES})')
        out.append((mail_kind, {'folder': folder_name, 'n': len(mine), 'items': '\n'.join(lines),
                                'link': link or '-'}))
    return out


def run_once(conn=None) -> int:
    """One pass: claim, group per folder, mail. Returns the number of mails sent."""
    own = conn is None
    conn = conn or access_db._connect()
    sent = 0
    try:
        try:
            rows = claim(conn)
        except Exception as exc:                        # noqa: BLE001 — before migration 052
            if access_db._missing(exc):
                conn.rollback()
                return 0
            raise
        if not rows:
            return 0
        folders, clusters = _names(conn, rows)
        if not mailer.configured():
            _record(conn, [r['id'] for r in rows], 'mail is not set up', False)
            return 0
        tree = access_db.load_tree()
        candidates = _mailable_candidates()
        link = mailer.link_base(None)
        by_folder: dict = {}
        for r in rows:
            by_folder.setdefault(r['folder_id'], []).append(r)
        for folder_id, items in by_folder.items():
            to = [u['username'] for u in closest_admins(candidates, tree, folder_id)]
            ids = [r['id'] for r in items]
            if not to:
                _record(conn, ids, 'no admin with a confirmed email address above this folder', False)
                continue
            fname = folders.get(folder_id or '', '') or '(no folder)'
            results = []
            for kind, values in compose(items, fname, clusters, link):
                for addr in to:
                    r = mailer.deliver(addr, kind, 'en', by='system', **values)
                    results.append(f"{addr}: {'sent' if r['sent'] else r['error']}")
                    sent += 1 if r['sent'] else 0
            _record(conn, ids, '; '.join(results), any(x.endswith(': sent') for x in results))
    finally:
        if own:
            conn.close()
    return sent


def _loop() -> None:
    time.sleep(POLL_SECONDS)                             # the migrations run at start-up
    while True:
        try:
            run_once()
        except Exception:                               # noqa: BLE001 — a database hiccup: next minute
            logger.warning('verification mail pass failed', exc_info=True)
        time.sleep(POLL_SECONDS)


def start() -> None:
    threading.Thread(target=_loop, daemon=True, name='hcm-verify-mail').start()
