"""
account_api.py — self-service for one's own account (plans/PLAN-roles.md, "Accounts", E4).

Credentials are never a tenant matter (Accounts 5): the person manages their own password, address and
name; only an administrator of the whole installation sets a password or resets two-factor for someone else.

- Forgot password: /account/forgot mails a link (60 minutes, once) to the account's address. The page says
  the same whether or not the address has an account; at most three links per account per hour.
- Change of address: the Account page asks for the current password; a link goes to the NEW address
  (60 minutes, once) and only confirming it changes the login. The old address gets a notice and is kept as an
  alias, so log rows written under it still name the same person. A taken address is refused only on the
  confirmation page, never to the person asking (that would tell them someone else's address has an account).
- The links open a page first and act on a POST, so a mail scanner that follows links cannot use them.
"""
from __future__ import annotations

import hashlib
import secrets

from flask import jsonify, render_template, request, session

import access_db
import mailer
from access_db import AccessError

RESET_MINUTES = 60
EMAIL_MINUTES = 60
RESETS_PER_HOUR = 3

PAGE = {
    'en': {
        'forgot_title': "Forgot your password?", 'forgot_sub': "Enter the email address you sign in with. If it has an account, a link to choose a new password is mailed to it.",
        'email': "Email address", 'send': "Send link",
        'forgot_sent': "If {email} has an account, a mail with a link is on its way. The link works for {minutes} minutes.",
        'no_mail': "Password reset by mail is not set up on this installation. Ask an administrator.",
        'reset_title': "Choose a new password", 'reset_sub': "For {email}.", 'password': "New password", 'password2': "Repeat the password",
        'save': "Save password", 'reset_done': "Your password is changed. Sign in with it.",
        'email_title': "Confirm your new address", 'email_sub': "Your account will sign in with {new} instead of {old}.", 'confirm': "Confirm",
        'email_done': "Done. Sign in with {new} from now on.", 'email_taken': "{new} cannot be used for your account. Nothing was changed.",
        'invalid_title': "This link no longer works", 'invalid_sub': "It was used already or has expired. Ask for a new one.",
        'err_short': "The password needs at least 8 characters.", 'err_match': "The passwords do not match.",
        'sign_in': "Sign in", 'back': "Back to sign in",
    },
    'nl': {
        'forgot_title': "Wachtwoord vergeten?", 'forgot_sub': "Vul het e-mailadres in waarmee je inlogt. Heeft het een account, dan krijgt het een link om een nieuw wachtwoord te kiezen.",
        'email': "E-mailadres", 'send': "Link sturen",
        'forgot_sent': "Als {email} een account heeft, is er een mail met een link onderweg. De link werkt {minutes} minuten.",
        'no_mail': "Wachtwoordherstel per mail is op deze installatie niet ingesteld. Vraag een beheerder.",
        'reset_title': "Kies een nieuw wachtwoord", 'reset_sub': "Voor {email}.", 'password': "Nieuw wachtwoord", 'password2': "Herhaal het wachtwoord",
        'save': "Wachtwoord opslaan", 'reset_done': "Je wachtwoord is gewijzigd. Log ermee in.",
        'email_title': "Bevestig je nieuwe adres", 'email_sub': "Je account logt voortaan in met {new} in plaats van {old}.", 'confirm': "Bevestigen",
        'email_done': "Klaar. Log voortaan in met {new}.", 'email_taken': "{new} kan niet voor je account worden gebruikt. Er is niets gewijzigd.",
        'invalid_title': "Deze link werkt niet meer", 'invalid_sub': "Hij is al gebruikt of verlopen. Vraag een nieuwe aan.",
        'err_short': "Het wachtwoord moet minstens 8 tekens hebben.", 'err_match': "De wachtwoorden komen niet overeen.",
        'sign_in': "Inloggen", 'back': "Terug naar inloggen",
    },
    'de': {
        'forgot_title': "Passwort vergessen?", 'forgot_sub': "Geben Sie die E-Mail-Adresse ein, mit der Sie sich anmelden. Hat sie ein Konto, erhält sie einen Link zur Wahl eines neuen Passworts.",
        'email': "E-Mail-Adresse", 'send': "Link senden",
        'forgot_sent': "Wenn {email} ein Konto hat, ist eine Mail mit einem Link unterwegs. Der Link funktioniert {minutes} Minuten.",
        'no_mail': "Das Zurücksetzen per Mail ist auf dieser Installation nicht eingerichtet. Wenden Sie sich an einen Administrator.",
        'reset_title': "Neues Passwort wählen", 'reset_sub': "Für {email}.", 'password': "Neues Passwort", 'password2': "Passwort wiederholen",
        'save': "Passwort speichern", 'reset_done': "Ihr Passwort wurde geändert. Melden Sie sich damit an.",
        'email_title': "Neue Adresse bestätigen", 'email_sub': "Ihr Konto meldet sich künftig mit {new} statt {old} an.", 'confirm': "Bestätigen",
        'email_done': "Erledigt. Melden Sie sich künftig mit {new} an.", 'email_taken': "{new} kann für Ihr Konto nicht verwendet werden. Es wurde nichts geändert.",
        'invalid_title': "Dieser Link funktioniert nicht mehr", 'invalid_sub': "Er wurde bereits verwendet oder ist abgelaufen. Fordern Sie einen neuen an.",
        'err_short': "Das Passwort braucht mindestens 8 Zeichen.", 'err_match': "Die Passwörter stimmen nicht überein.",
        'sign_in': "Anmelden", 'back': "Zurück zur Anmeldung",
    },
    'fr': {
        'forgot_title': "Mot de passe oublié ?", 'forgot_sub': "Saisissez l'adresse e-mail avec laquelle vous vous connectez. Si elle a un compte, un lien pour choisir un nouveau mot de passe lui est envoyé.",
        'email': "Adresse e-mail", 'send': "Envoyer le lien",
        'forgot_sent': "Si {email} a un compte, un e-mail avec un lien est en route. Le lien fonctionne {minutes} minutes.",
        'no_mail': "La réinitialisation par e-mail n'est pas configurée sur cette installation. Demandez à un administrateur.",
        'reset_title': "Choisissez un nouveau mot de passe", 'reset_sub': "Pour {email}.", 'password': "Nouveau mot de passe", 'password2': "Répétez le mot de passe",
        'save': "Enregistrer le mot de passe", 'reset_done': "Votre mot de passe a été modifié. Connectez-vous avec.",
        'email_title': "Confirmez votre nouvelle adresse", 'email_sub': "Votre compte se connectera avec {new} au lieu de {old}.", 'confirm': "Confirmer",
        'email_done': "Terminé. Connectez-vous désormais avec {new}.", 'email_taken': "{new} ne peut pas être utilisée pour votre compte. Rien n'a été modifié.",
        'invalid_title': "Ce lien ne fonctionne plus", 'invalid_sub': "Il a déjà été utilisé ou a expiré. Demandez-en un nouveau.",
        'err_short': "Le mot de passe doit comporter au moins 8 caractères.", 'err_match': "Les mots de passe ne correspondent pas.",
        'sign_in': "Se connecter", 'back': "Retour à la connexion",
    },
}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _lang() -> str:
    cookie = (request.cookies.get('hcm_lang') or '')[:2]
    return cookie if cookie in PAGE else (request.accept_languages.best_match(list(PAGE)) or 'en')


def _new_token(kind: str, user_id: str, minutes: int, new_email: str | None = None) -> str:
    token = secrets.token_urlsafe(32)
    conn = access_db._connect()
    try:
        with conn.cursor() as cur:
            # a new link of a kind replaces the unused ones before it
            cur.execute('UPDATE config.account_tokens SET used_at = NOW() WHERE user_id = %s AND kind = %s AND used_at IS NULL',
                        (user_id, kind))
            cur.execute(f"""INSERT INTO config.account_tokens (token_hash, kind, user_id, new_email, expires_at)
                            VALUES (%s, %s, %s, %s, NOW() + INTERVAL '{int(minutes)} minutes')""",
                        (_hash(token), kind, user_id, new_email))
        conn.commit()
    finally:
        conn.close()
    return token


def _token(token: str, kind: str) -> dict | None:
    if not token or len(token) > 200:
        return None
    conn = access_db._connect()
    try:
        with conn.cursor() as cur:
            cur.execute('''SELECT t.token_hash, t.user_id::text AS user_id, t.new_email, u.username, u.enabled
                             FROM config.account_tokens t JOIN config.users u ON u.user_id = t.user_id
                            WHERE t.token_hash = %s AND t.kind = %s AND t.used_at IS NULL AND t.expires_at > NOW()''',
                        (_hash(token), kind))
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        conn.close()


def _use(token_hash: str) -> bool:
    """Single use: the first to mark it wins."""
    conn = access_db._connect()
    try:
        with conn.cursor() as cur:
            cur.execute('UPDATE config.account_tokens SET used_at = NOW() WHERE token_hash = %s AND used_at IS NULL '
                        'AND expires_at > NOW() RETURNING 1', (token_hash,))
            ok = cur.fetchone() is not None
        conn.commit()
        return ok
    finally:
        conn.close()


def _recent_resets(user_id: str) -> int:
    conn = access_db._connect()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT COUNT(*) AS n FROM config.account_tokens WHERE user_id = %s AND kind = 'reset'
                            AND created_at > NOW() - INTERVAL '1 hour'""", (user_id,))
            return cur.fetchone()['n']
    finally:
        conn.close()


def _audit(actor: str, action: str, target: str, detail: dict | None = None) -> None:
    conn = access_db._connect()
    try:
        with conn.cursor() as cur:
            access_db.audit(cur, actor, action, target, detail=detail)
        conn.commit()
    finally:
        conn.close()


def register(app, requires, public, h):
    """h: the checks shared with access_api (top, actor, me_id)."""

    def page(state, status=200, **vals):
        t = {k: v.format(**{'minutes': RESET_MINUTES, 'hours': EMAIL_MINUTES // 60, 'email': '', 'new': '', 'old': '', **vals})
             for k, v in PAGE[_lang()].items()}
        return render_template('account.html', state=state, t=t, lang=_lang(), **vals), status

    # -- forgot password ----------------------------------------------------------------------------------
    @app.route('/account/forgot', methods=['GET', 'POST'])
    @public('asking for a password reset link: the answer is the same for every address')
    def account_forgot():
        if not mailer.configured():
            return page('no_mail')
        base = mailer.link_base()
        if not base:
            # the link is never built from this request's Host header (anyone can set it): no address, no link
            app.logger.warning('password reset by mail is off: %s', mailer.LINK_ADDRESS_MISSING)
            return page('no_mail')
        if request.method == 'GET':
            return page('forgot')
        raw = request.form.get('email', '')
        try:
            email = access_db.normalise_email(raw)
        except AccessError:
            return page('forgot', email=raw[:254])
        uid = access_db.user_id_by_login(email)
        u = access_db.user(uid) if uid else None
        # only an enabled account with a confirmed address, and not more than a few links an hour
        if u and u['enabled'] and not u['awaiting_email'] and '@' in u['username'] and _recent_resets(uid) < RESETS_PER_HOUR:
            token = _new_token('reset', uid, RESET_MINUTES)
            link = f"{base}/account/reset/{token}"
            mailer.deliver(u['username'], 'reset', _lang(), by=u['username'], link=link, minutes=RESET_MINUTES)
            _audit(u['username'], 'user.reset_requested', u['username'])
        return page('forgot_sent', email=email)

    @app.route('/account/reset/<token>', methods=['GET', 'POST'])
    @public('a password reset link: the token is the credential')
    def account_reset(token):
        tok = _token(token, 'reset')
        if not tok or not tok['enabled']:
            return page('invalid', 404)
        if request.method == 'GET':
            return page('reset', email=tok['username'], token=token)
        pw, pw2 = request.form.get('password', ''), request.form.get('password2', '')
        t = PAGE[_lang()]
        err = t['err_short'] if len(pw) < 8 else t['err_match'] if pw != pw2 else None
        if err:
            return page('reset', email=tok['username'], token=token, error=err)
        if not _use(tok['token_hash']):
            return page('invalid', 404)
        import user_db
        user_db.set_password(tok['username'], pw)
        _audit(tok['username'], 'user.password_reset_self', tok['username'])
        session.clear()
        return page('reset_done')

    # -- change of address ----------------------------------------------------------------------------------
    @app.route('/api/users/me/email', methods=['PUT'])
    @requires(None)
    def account_email_change():
        """Ask to sign in with another address: the current password proves it is the person, the link to the
        new address proves they hold it. The answer never says whether the new address has an account."""
        import user_db
        b = request.get_json(silent=True) or {}
        me = session.get('username') or ''
        if '@' not in me:
            raise AccessError('this account signs in with a username; its address is set in .env', 409)
        if not user_db.verify_password(me, b.get('password') or ''):
            raise AccessError('the current password is not right', 403)
        new = access_db.normalise_email(b.get('email'))
        if new == me.lower():
            raise AccessError('that is the address you use now')
        if not mailer.configured():
            raise AccessError('changing your address needs mail, which is not set up here: ask an administrator', 409)
        token = _new_token('email', session['user_id'], EMAIL_MINUTES, new_email=new)
        # the person asked from their own signed-in browser, with their password, for a mail to an address they
        # typed: the address they reach HCM on is a fair fallback here (never on the public routes)
        link = f"{mailer.link_base() or request.host_url.rstrip('/')}/account/email/{token}"
        r = mailer.deliver(new, 'email_confirm', b.get('lang') or _lang(), by=me, link=link, minutes=EMAIL_MINUTES, new=new)
        _audit(me, 'user.email_requested', me, detail={'to': new})
        if not r['sent']:
            raise AccessError(f'the confirmation mail could not be sent: {r["error"]}', 502)
        return jsonify({'ok': True, 'sent': True, 'minutes': EMAIL_MINUTES})

    @app.route('/account/email/<token>', methods=['GET', 'POST'])
    @public('an address confirmation link: the token is the credential')
    def account_email_confirm(token):
        tok = _token(token, 'email')
        if not tok:
            return page('invalid', 404)
        old, new = tok['username'], tok['new_email']
        if request.method == 'GET':
            return page('email', new=new, old=old, token=token)
        if not _use(tok['token_hash']):
            return page('invalid', 404)
        try:
            access_db.change_address(tok['user_id'], new, actor=old, verified=True)
        except AccessError:
            return page('email_taken', new=new)
        mailer.deliver(old, 'email_notice', _lang(), by=new, new=new)
        if session.get('user_id') == tok['user_id']:
            session.clear()
        return page('email_done', new=new)

    @app.route('/api/users/me/password', methods=['PUT'])
    @requires(None)
    def account_me_password():
        """The person's own password: always with the current one, also for administrators."""
        import user_db
        b = request.get_json(silent=True) or {}
        me = session.get('username')
        if not session.get('user_id') or not me:
            raise AccessError('sign in first', 401)
        new = str(b.get('new_password') or '')
        if not user_db.verify_password(me, str(b.get('current_password') or '')):
            _audit(me, 'user.password_change_refused', me)
            raise AccessError('the current password is not correct', 403)
        if len(new) < 8:
            raise AccessError('the password needs at least 8 characters')
        user_db.set_password(me, new)
        _audit(me, 'user.password_changed_self', me)
        return jsonify({'ok': True})

    @app.route('/api/users/me', methods=['PUT'])
    @requires(None)
    def account_me_update():
        """The person's own display name."""
        b = request.get_json(silent=True) or {}
        if 'display_name' in b and session.get('user_id'):
            access_db.update_user(session['user_id'], session.get('username'), display_name=str(b['display_name'] or ''))
        return jsonify({'ok': True})

    # -- top-level admins for someone else --------------------------------------------------------------
    @app.route('/api/access/users/<user_id>/otp-reset', methods=['POST'])
    @requires('users.manage', 'root')
    def account_otp_reset(user_id):
        """Two-factor off for someone who lost their device (top level only; they set it up again)."""
        import user_db
        u = access_db.user(str(user_id)) if access_db._UUID_RE.match(str(user_id)) else None
        if not u:
            raise AccessError('not found', 404)
        if u['user_id'] == h.me_id():
            raise AccessError('turn off your own two-factor on your Account page', 409)
        h.require_touch_account(h.access(), u)      # resetting someone's 2FA is half of signing in as them
        user_db.disable_otp(u['username'])
        _audit(h.actor(), 'user.otp_reset', u['username'])
        access_db.invalidate()
        return jsonify({'ok': True})
