"""
mail_api.py — Settings → Mail: the installation's SMTP relay (plans/PLAN-roles.md, "Accounts", E2).
Administrators of the whole installation only (install.manage).
"""
from __future__ import annotations

import os

from flask import jsonify, request, session

import access_db
import mailer
from access_db import AccessError


def register(app, requires):
    def actor() -> str:
        return session.get('username') or 'anonymous'

    @app.route('/api/mail/settings')
    @requires('install.manage', 'root')
    def mail_settings_get():
        out = mailer.settings()
        out['suggested_public_url'] = request.host_url.rstrip('/')     # a placeholder for the admin, never used in a mail
        # links in mails come from public_url only: without it no reset, invitation or approval link is mailed
        out['public_url_missing'] = bool(out.get('configured') and not out.get('public_url'))
        if out['public_url_missing']:
            out['warning'] = mailer.LINK_ADDRESS_MISSING
        out['env_guest_url'] = (os.getenv('HCM_GUEST_BASE_URL') or '').strip().rstrip('/') or None
        return jsonify(out)

    @app.route('/api/mail/settings', methods=['PUT'])
    @requires('install.manage', 'root')
    def mail_settings_put():
        return jsonify(mailer.save(request.get_json(silent=True) or {}, actor()))

    @app.route('/api/mail/test', methods=['POST'])
    @requires('install.manage', 'root')
    def mail_test():
        """Send the test mail to an address (default: the sender's own login when it is one)."""
        b = request.get_json(silent=True) or {}
        to = access_db.normalise_email(b.get('to') or (actor() if '@' in actor() else ''))
        if not mailer.configured():
            raise AccessError('mail is not set up: save a host and a sender address first', 409)
        r = mailer.deliver(to, 'test', b.get('lang'), by=actor(), actor=actor())
        conn = access_db._connect()
        try:
            with conn.cursor() as cur:
                access_db.audit(cur, actor(), 'mail.test', to, detail={'sent': r['sent']})
            conn.commit()
        finally:
            conn.close()
        return jsonify(r), (200 if r['sent'] else 502)
