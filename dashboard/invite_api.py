"""
invite_api.py — invitations (plans/PLAN-roles.md, "Accounts", E3).

An administrator of a layer (users.manage there) or above invites an email address onto that layer, with a
role on a scope or into a group. The mail's link proves the person controls the address:

- a new address sets its password on /invite/<token> and is signed in;
- an existing account accepts after signing in with that address;
- 'activate' (an account from before E1, given its address by a top-level admin) sets a new password.

The inviter never learns whether the address had an account: the answer is the same either way, and only
the recipient's mail differs. Valid 48 hours, single use, only the token's SHA-256 is stored. On
acceptance the inviter must still be allowed to give that access, or the invitation is void. Without a mail
server (or when it fails) the inviter gets the link to pass on.
"""
from __future__ import annotations

import hashlib
import secrets

from flask import jsonify, redirect, render_template, request, session

import access_db
import mailer
import rbac
from access_db import AccessError

HOURS = 48
_ROW_SQL = '''SELECT i.invitation_id::text AS invitation_id, i.kind, i.email, i.display_name, i.user_id::text AS user_id,
                     i.role_id::text AS role_id, r.name AS role_name, r.builtin_key, i.group_id::text AS group_id,
                     g.name AS group_name, g.node_id::text AS group_node_id, i.scope_kind, i.node_id::text AS node_id,
                     i.cluster_id::text AS cluster_id, c.cluster_name, i.tag, i.vm_uuid, i.vm_name, i.lang, i.state,
                     i.invited_by, i.invited_by_id::text AS invited_by_id, i.created_at, i.expires_at,
                     i.expires_at < NOW() AS expired
                FROM config.invitations i
                LEFT JOIN config.roles r ON r.role_id = i.role_id
                LEFT JOIN config.groups g ON g.group_id = i.group_id
                LEFT JOIN config.clusters c ON c.cluster_id = i.cluster_id'''

# the page the link opens, in the dashboard's four languages (the invitation's language, else the browser's)
PAGE = {
    'en': {
        'title_new': "Welcome", 'sub_new': "You have been given access to {layer} as {role}. Choose a password for {email}.",
        'name': "Full name (optional)", 'password': "Password", 'password2': "Repeat the password",
        'submit_new': "Create my account",
        'title_activate': "Choose a new password", 'sub_activate': "Your account now signs in with {email}. Choose a new password.",
        'submit_activate': "Save password",
        'title_accept': "New access", 'sub_accept': "You have been given access to {layer} as {role}.", 'submit_accept': "Accept",
        'sign_in': "Sign in as {email} to accept this invitation.", 'sign_in_btn': "Sign in",
        'other_account': "You are signed in as {me}, but this invitation is for {email}.", 'switch_btn': "Sign out and switch",
        'title_invalid': "This link no longer works",
        'sub_invalid': "It was used already, expired after 48 hours, or was withdrawn. Ask whoever invited you for a new one.",
        'err_short': "The password needs at least 8 characters.", 'err_match': "The passwords do not match.",
        'done_sign_in': "Done. Sign in with your new password.",
    },
    'nl': {
        'title_new': "Welkom", 'sub_new': "Je hebt toegang gekregen tot {layer} als {role}. Kies een wachtwoord voor {email}.",
        'name': "Volledige naam (optioneel)", 'password': "Wachtwoord", 'password2': "Herhaal het wachtwoord",
        'submit_new': "Mijn account aanmaken",
        'title_activate': "Kies een nieuw wachtwoord", 'sub_activate': "Je account logt voortaan in met {email}. Kies een nieuw wachtwoord.",
        'submit_activate': "Wachtwoord opslaan",
        'title_accept': "Nieuwe toegang", 'sub_accept': "Je hebt toegang gekregen tot {layer} als {role}.", 'submit_accept': "Aanvaarden",
        'sign_in': "Log in als {email} om deze uitnodiging te aanvaarden.", 'sign_in_btn': "Inloggen",
        'other_account': "Je bent ingelogd als {me}, maar deze uitnodiging is voor {email}.", 'switch_btn': "Uitloggen en wisselen",
        'title_invalid': "Deze link werkt niet meer",
        'sub_invalid': "Hij is al gebruikt, na 48 uur verlopen of ingetrokken. Vraag wie je uitnodigde om een nieuwe.",
        'err_short': "Het wachtwoord moet minstens 8 tekens hebben.", 'err_match': "De wachtwoorden komen niet overeen.",
        'done_sign_in': "Klaar. Log in met je nieuwe wachtwoord.",
    },
    'de': {
        'title_new': "Willkommen", 'sub_new': "Sie haben Zugriff auf {layer} als {role} erhalten. Wählen Sie ein Passwort für {email}.",
        'name': "Vollständiger Name (optional)", 'password': "Passwort", 'password2': "Passwort wiederholen",
        'submit_new': "Mein Konto anlegen",
        'title_activate': "Neues Passwort wählen", 'sub_activate': "Ihr Konto meldet sich jetzt mit {email} an. Wählen Sie ein neues Passwort.",
        'submit_activate': "Passwort speichern",
        'title_accept': "Neuer Zugriff", 'sub_accept': "Sie haben Zugriff auf {layer} als {role} erhalten.", 'submit_accept': "Annehmen",
        'sign_in': "Melden Sie sich als {email} an, um diese Einladung anzunehmen.", 'sign_in_btn': "Anmelden",
        'other_account': "Sie sind als {me} angemeldet, diese Einladung gilt aber {email}.", 'switch_btn': "Abmelden und wechseln",
        'title_invalid': "Dieser Link funktioniert nicht mehr",
        'sub_invalid': "Er wurde bereits verwendet, ist nach 48 Stunden abgelaufen oder wurde zurückgezogen. Bitten Sie die einladende Person um einen neuen.",
        'err_short': "Das Passwort braucht mindestens 8 Zeichen.", 'err_match': "Die Passwörter stimmen nicht überein.",
        'done_sign_in': "Erledigt. Melden Sie sich mit Ihrem neuen Passwort an.",
    },
    'fr': {
        'title_new': "Bienvenue", 'sub_new': "Vous avez reçu un accès à {layer} en tant que {role}. Choisissez un mot de passe pour {email}.",
        'name': "Nom complet (facultatif)", 'password': "Mot de passe", 'password2': "Répétez le mot de passe",
        'submit_new': "Créer mon compte",
        'title_activate': "Choisissez un nouveau mot de passe", 'sub_activate': "Votre compte se connecte désormais avec {email}. Choisissez un nouveau mot de passe.",
        'submit_activate': "Enregistrer le mot de passe",
        'title_accept': "Nouvel accès", 'sub_accept': "Vous avez reçu un accès à {layer} en tant que {role}.", 'submit_accept': "Accepter",
        'sign_in': "Connectez-vous en tant que {email} pour accepter cette invitation.", 'sign_in_btn': "Se connecter",
        'other_account': "Vous êtes connecté en tant que {me}, mais cette invitation est pour {email}.", 'switch_btn': "Se déconnecter et changer",
        'title_invalid': "Ce lien ne fonctionne plus",
        'sub_invalid': "Il a déjà été utilisé, a expiré après 48 heures ou a été retiré. Demandez-en un nouveau à la personne qui vous a invité.",
        'err_short': "Le mot de passe doit comporter au moins 8 caractères.", 'err_match': "Les mots de passe ne correspondent pas.",
        'done_sign_in': "Terminé. Connectez-vous avec votre nouveau mot de passe.",
    },
}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _lang(inv: dict | None) -> str:
    want = ((inv or {}).get('lang') or '')[:2]
    if want in PAGE:
        return want
    return request.accept_languages.best_match(list(PAGE)) or 'en'


def _rows(where: str = '', params=()) -> list[dict]:
    conn = access_db._connect()
    try:
        with conn.cursor() as cur:
            cur.execute(_ROW_SQL + (' WHERE ' + where if where else '') + ' ORDER BY i.created_at DESC', params)
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _by_token(token: str) -> dict | None:
    if not token or len(token) > 200:
        return None
    rows = _rows('i.token_hash = %s', (_hash(token),))
    return rows[0] if rows else None


def _by_id(invitation_id: str) -> dict | None:
    rows = _rows('i.invitation_id::text = %s', (invitation_id,))
    return rows[0] if rows else None


def _role_label(inv: dict) -> str:
    if inv['group_id']:
        return inv['group_name'] or ''
    if inv['builtin_key'] and inv['builtin_key'] in rbac.BUILTIN_ROLES:
        return rbac.BUILTIN_ROLES[inv['builtin_key']]['name']
    return inv['role_name'] or ''


def _layer_label(inv: dict) -> str:
    """What the invitee is invited to, as they will see it: the scope's own name, never the layers above."""
    tree = access_db.load_tree()
    if inv['group_id']:
        return tree.name.get(inv['group_node_id'], 'HyperCore Manager') if inv['group_node_id'] else 'HyperCore Manager'
    k = inv['scope_kind']
    if k == 'node':
        return tree.name.get(inv['node_id'], '')
    if k == 'cluster':
        return inv['cluster_name'] or ''
    if k == 'tag':
        return f"{inv['cluster_name']} · {inv['tag']}"
    if k == 'vm':
        return inv['vm_name'] or inv['vm_uuid'] or ''
    return 'HyperCore Manager'


def _scope(inv: dict) -> dict:
    return {'kind': inv['scope_kind'], 'node_id': inv['node_id'], 'cluster_id': inv['cluster_id'], 'tag': inv['tag'],
            'vm_uuid': inv['vm_uuid'], 'vm_name': inv['vm_name']}


def _link(token: str, host_url: str | None) -> str:
    return f'{mailer.link_base(host_url)}/invite/{token}'


def _send(inv: dict, token: str, host_url: str | None, by: str) -> dict:
    """Mail the link; without mail (or on failure) the link comes back for the inviter to pass on."""
    link = _link(token, host_url)
    if inv['kind'] == 'activate':
        kind, values = 'activate', {}
    else:
        exists = access_db.user_id_by_login(inv['email']) is not None
        kind, values = ('invite_existing' if exists else 'invite_new'), {'layer': _layer_label(inv), 'role': _role_label(inv)}
    r = mailer.deliver(inv['email'], kind, inv['lang'], by=by, link=link, hours=HOURS, **values)
    return {'sent': r['sent'], 'error': r['error'], 'link': None if r['sent'] else link}


def _insert(fields: dict) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    cols = dict(fields, token_hash=_hash(token))
    conn = access_db._connect()
    try:
        with conn.cursor() as cur:
            names = ', '.join(cols)
            marks = ', '.join(['%s'] * len(cols))
            cur.execute(f"INSERT INTO config.invitations ({names}, expires_at) VALUES ({marks}, NOW() + INTERVAL '{HOURS} hours') "
                        "RETURNING invitation_id::text AS id", list(cols.values()))
            iid = cur.fetchone()['id']
            access_db.audit(cur, fields.get('invited_by'), 'invitation.send', fields['email'],
                            node_id=fields.get('node_id'), detail={'kind': fields.get('kind', 'access')})
        conn.commit()
    finally:
        conn.close()
    return iid, token


def send_activation(u: dict, email: str, by: str, host_url: str | None) -> dict:
    """A top-level admin gives an account from before E1 its address: the address is reserved for it now, and
    an invitation to set a new password goes there (PLAN-roles, Accounts 7)."""
    email = access_db.normalise_email(email)
    conn = access_db._connect()
    try:
        with conn.cursor() as cur:
            try:
                cur.execute('UPDATE config.users SET username = %s, updated_at = NOW() WHERE user_id = %s AND awaiting_email',
                            (email, u['user_id']))
            except Exception as exc:  # noqa: BLE001
                if 'duplicate key' in str(exc):
                    raise AccessError(f'{email} already has an account', 409) from exc
                raise
            access_db.audit(cur, by, 'user.email', email, detail={'from': u['username'], 'pending': True})
        conn.commit()
    finally:
        conn.close()
    access_db.invalidate()
    by_id = access_db.user_id_by_login(by)
    iid, token = _insert({'kind': 'activate', 'email': email, 'user_id': u['user_id'], 'invited_by': by,
                          'invited_by_id': by_id, 'lang': None})
    return dict(_send(_by_id(iid), token, host_url, by), invitation_id=iid)


def _claim(invitation_id: str, accepted_user: str | None) -> bool:
    """Single use: only the first claim of a pending, unexpired invitation wins."""
    conn = access_db._connect()
    try:
        with conn.cursor() as cur:
            cur.execute('''UPDATE config.invitations SET state = 'accepted', accepted_at = NOW(), accepted_user = %s
                            WHERE invitation_id = %s AND state = 'pending' AND expires_at > NOW() RETURNING email''',
                        (accepted_user, invitation_id))
            row = cur.fetchone()
            if row:
                access_db.audit(cur, row['email'], 'invitation.accept', row['email'])
        conn.commit()
        return bool(row)
    finally:
        conn.close()


def register(app, requires, public, access, h):
    """h: the checks shared with access_api (grant_check, parse_scope, scope_node, seen, nameable,
    may_manage_group, top, actor, me_id)."""

    def may_give(acc: rbac.Access, inv: dict) -> None:
        """May `acc` hand out what this invitation gives? (AccessError when not.)"""
        if inv['kind'] == 'activate':
            if not h.top(acc):
                raise AccessError('not found', 404)
            return
        if inv['group_id']:
            g = access_db.group(inv['group_id'])
            if not g or not h.may_manage_group(acc, g):
                raise AccessError('not found', 404)
            for a in access_db.assignments(group_id=g['group_id']):
                h.grant_check(acc, access_db.role(a['role_id']), {'kind': a['scope_kind'], 'node_id': a['node_id'],
                              'cluster_id': a['cluster_id'], 'tag': a['tag'], 'vm_uuid': a['vm_uuid']})
            return
        r = access_db.role(inv['role_id'] or '')
        if not r:
            raise AccessError('unknown role', 404)
        h.grant_check(acc, r, _scope(inv))

    def in_reach(acc: rbac.Access, inv: dict) -> bool:
        if h.top(acc):
            return True
        if inv['kind'] == 'activate':
            return False
        if inv['group_id']:
            return acc.can('users.manage', node_id=inv['group_node_id'])
        return access_db.assignment_in_reach(acc, {'scope_kind': inv['scope_kind'], 'node_id': inv['node_id'],
                                                   'cluster_id': inv['cluster_id']})

    def listed(acc: rbac.Access, inv: dict) -> dict:
        names = h.nameable(acc)
        tree = acc.tree
        label = (inv['group_name'] if inv['group_id'] else
                 access_db.scope_label(dict(inv, scope_kind=inv['scope_kind'] or 'root'), tree, h.seen(acc)))
        return {'invitation_id': inv['invitation_id'], 'kind': inv['kind'], 'email': inv['email'],
                'display_name': inv['display_name'], 'role_name': inv['role_name'], 'builtin_key': inv['builtin_key'],
                'group_id': inv['group_id'], 'group_name': inv['group_name'], 'scope_kind': inv['scope_kind'],
                'node_id': inv['node_id'], 'cluster_id': inv['cluster_id'], 'scope_label': label,
                'state': 'expired' if inv['state'] == 'pending' and inv['expired'] else inv['state'],
                'created_at': inv['created_at'].isoformat(), 'expires_at': inv['expires_at'].isoformat(),
                'invited_by': inv['invited_by'] if names is None or inv['invited_by'] in names else None}

    # -- the inviter's side ------------------------------------------------------------------------------
    @app.route('/api/access/invitations', methods=['POST'])
    @requires('users.manage', 'any')
    def invitation_create():
        b = request.get_json(silent=True) or {}
        acc = access()
        tree = acc.tree
        email = access_db.normalise_email(b.get('email'))
        if email == (h.actor() or '').lower():
            raise AccessError('that is your own address', 409)
        fields = {'kind': 'access', 'email': email, 'display_name': (str(b.get('display_name') or '').strip()[:120] or None),
                  'invited_by': h.actor(), 'invited_by_id': h.me_id(), 'lang': (str(b.get('lang') or '')[:2] or None)}
        if b.get('group_id'):
            g = access_db.group(str(b['group_id']))
            if not g:
                raise AccessError('not found', 404)
            fields['group_id'] = g['group_id']
        else:
            r = access_db.role(str(b.get('role_id') or ''))
            if not r:
                raise AccessError('choose the role and where it applies')
            scope = h.parse_scope(b, tree)
            fields.update(role_id=r['role_id'], scope_kind=scope['kind'], node_id=scope.get('node_id'),
                          cluster_id=scope.get('cluster_id'), tag=scope.get('tag'), vm_uuid=scope.get('vm_uuid'),
                          vm_name=scope.get('vm_name'))
        may_give(acc, {k: fields.get(k) for k in ('kind', 'group_id', 'role_id', 'scope_kind', 'node_id', 'cluster_id',
                                                  'tag', 'vm_uuid', 'vm_name')})
        iid, token = _insert({k: v for k, v in fields.items() if v is not None})
        out = _send(_by_id(iid), token, request.host_url, h.actor())
        return jsonify(dict(out, ok=True, invitation_id=iid, hours=HOURS)), 201

    @app.route('/api/access/invitations')
    @requires('users.manage', 'any')
    def invitation_list():
        """Open invitations in the reader's reach (expired ones for a week after, to resend)."""
        acc = access()
        rows = _rows("i.state = 'pending' AND i.expires_at > NOW() - INTERVAL '7 days'")
        return jsonify({'invitations': [listed(acc, r) for r in rows if in_reach(acc, r)], 'hours': HOURS})

    def managed(invitation_id: str) -> dict:
        inv = _by_id(str(invitation_id))
        acc = access()
        if not inv or inv['state'] != 'pending' or not in_reach(acc, inv):
            raise AccessError('not found', 404)
        may_give(acc, inv)
        return inv

    @app.route('/api/access/invitations/<invitation_id>/resend', methods=['POST'])
    @requires('users.manage', 'any')
    def invitation_resend(invitation_id):
        """A new link, valid 48 hours from now; the old link stops working."""
        inv = managed(invitation_id)
        token = secrets.token_urlsafe(32)
        conn = access_db._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(f"""UPDATE config.invitations SET token_hash = %s, expires_at = NOW() + INTERVAL '{HOURS} hours'
                                WHERE invitation_id = %s""", (_hash(token), inv['invitation_id']))
                access_db.audit(cur, h.actor(), 'invitation.resend', inv['email'], node_id=inv['node_id'])
            conn.commit()
        finally:
            conn.close()
        return jsonify(dict(_send(_by_id(inv['invitation_id']), token, request.host_url, h.actor()), ok=True, hours=HOURS))

    @app.route('/api/access/invitations/<invitation_id>', methods=['DELETE'])
    @requires('users.manage', 'any')
    def invitation_withdraw(invitation_id):
        inv = managed(invitation_id)
        conn = access_db._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE config.invitations SET state = 'withdrawn' WHERE invitation_id = %s AND state = 'pending'",
                            (inv['invitation_id'],))
                access_db.audit(cur, h.actor(), 'invitation.withdraw', inv['email'], node_id=inv['node_id'])
            conn.commit()
        finally:
            conn.close()
        return jsonify({'ok': True})

    # -- the invitee's side (public: the token is the credential) ---------------------------------------
    def page(inv, state, **kw):
        lang = _lang(inv)
        t = PAGE[lang]
        vals = {'email': (inv or {}).get('email', ''), 'layer': _layer_label(inv) if inv and inv['kind'] == 'access' else '',
                'role': _role_label(inv) if inv and inv['kind'] == 'access' else '', 'me': session.get('username') or ''}
        txt = {k: v.format(**vals) for k, v in t.items()}
        return render_template('invite.html', state=state, t=txt, lang=lang, inv=inv, **kw), (404 if state == 'invalid' else 200)

    def still_valid(inv) -> bool:
        """Pending, unexpired, and its inviter may still give it (rights change; accounts get disabled)."""
        if not inv or inv['state'] != 'pending' or inv['expired']:
            return False
        if not inv['invited_by_id']:
            return False
        st = access_db.user_state(inv['invited_by_id'])
        if not st or not st.get('enabled', True):
            return False
        try:
            may_give(access_db.access_for(inv['invited_by_id']), inv)
        except AccessError:
            return False
        return True

    def give(inv, user_id: str) -> None:
        if inv['group_id']:
            access_db.set_group_member(inv['group_id'], user_id, True, inv['invited_by'])
            return
        scope = _scope(inv)
        same = [a for a in access_db.assignments(user_id=user_id)
                if a['role_id'] == inv['role_id'] and a['scope_kind'] == scope['kind'] and a['node_id'] == scope['node_id']
                and a['cluster_id'] == scope['cluster_id'] and a['tag'] == scope['tag'] and a['vm_uuid'] == scope['vm_uuid']]
        if not same:
            tree = access_db.load_tree()
            audit_node = scope['node_id'] if scope['kind'] == 'node' else tree.cluster_folder.get(scope['cluster_id'])
            access_db.add_assignment({'user_id': user_id}, inv['role_id'], scope, inv['invited_by'], audit_node)

    def sign_in(user_id: str) -> bool:
        """Sign the new or reactivated account in, unless it has two-factor (then the login page asks for it)."""
        import user_db
        u = access_db.user(user_id)
        full = user_db.get_by_username(u['username']) if u else None
        if not full or full.get('otp_enabled') or not full.get('enabled', True):
            return False
        session.clear()
        session['user_id'], session['username'], session['role'] = str(full['user_id']), full['username'], full['role']
        return True

    @app.route('/invite/<token>', methods=['GET', 'POST'])
    @public('an invitation link: the token is the credential')
    def invite_page(token):
        inv = _by_token(token)
        if not still_valid(inv):
            return page(inv if inv and inv['state'] == 'pending' else None, 'invalid')
        existing = access_db.user_id_by_login(inv['email'])
        if inv['kind'] == 'activate':
            state = 'activate'
        elif existing:
            state = 'accept' if (session.get('username') or '').lower() == inv['email'] else \
                    ('other' if session.get('user_id') else 'sign_in')
        else:
            state = 'new'
        if request.method == 'GET':
            if state == 'sign_in':
                session['next'] = f'/invite/{token}'
            return page(inv, state, token=token, name=inv['display_name'] or '')

        t = PAGE[_lang(inv)]
        if state in ('new', 'activate'):
            pw, pw2 = request.form.get('password', ''), request.form.get('password2', '')
            name = request.form.get('display_name', '').strip()[:120]
            err = t['err_short'] if len(pw) < 8 else t['err_match'] if pw != pw2 else None
            if err:
                return page(inv, state, token=token, name=name, error=err)
            if state == 'new':
                try:
                    uid = access_db.create_invited_user(inv['email'], pw, name or inv['display_name'])
                except AccessError:                   # the address got an account meanwhile: sign in and accept
                    session['next'] = f'/invite/{token}'
                    return page(inv, 'sign_in', token=token)
                if not _claim(inv['invitation_id'], uid):
                    access_db.delete_user(uid, 'invitation')     # lost the race (used, withdrawn, expired)
                    return page(None, 'invalid')
                give(inv, uid)
            else:
                uid = inv['user_id']
                if not _claim(inv['invitation_id'], uid):
                    return page(None, 'invalid')
                access_db.activate_user(uid, pw, name or None)
            if sign_in(uid):
                return redirect('/')
            return page(inv, 'done', login=True)
        if state == 'accept':
            if not _claim(inv['invitation_id'], existing):
                return page(None, 'invalid')
            give(inv, existing)
            return redirect('/')
        return page(inv, state, token=token)
