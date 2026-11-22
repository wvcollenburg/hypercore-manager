"""
login_texts.py — the sign-in page in the dashboard's four languages (en, nl, de, fr).

The language is the dashboard's own choice (cookie hcm_lang, set by its language menu), else the
browser's, else English: the same rule as the account and invitation pages. Add a text in all four
languages at once.
"""
from __future__ import annotations

TEXTS: dict[str, dict[str, str]] = {
    'en': {
        'title': 'Sign in', 'tagline': 'Cluster Management Interface',
        'sub': 'Enter your credentials to access the dashboard.',
        'sub_otp_next': 'You will be asked for a verification code after signing in.',
        'email': 'Email address', 'password': 'Password', 'sign_in': 'Sign in', 'continue': 'Continue',
        'forgot': 'Forgot your password?',
        'otp_title': 'Two-factor authentication', 'otp_sub': 'Enter the 6-digit code from your authenticator app.',
        'otp_label': 'Verification code', 'otp_hint': 'Code refreshes every 30 seconds.', 'verify': 'Verify',
        'wrong_account': 'Wrong account?', 'start_over': 'Start over',
        'err_login': 'Invalid email address or password.',
        'err_otp': 'Invalid code. Check your authenticator app and try again.',
        'err_invited': 'An invitation to set a new password was sent to this address. Use the link in it.',
        'err_needs_email': 'Your account needs an email address. An administrator will send you an invitation.',
        'err_awaiting': 'Your account is waiting for an administrator to assign its access.',
        'err_disabled': 'Your account is disabled. Ask an administrator.',
        'err_locked': 'Too many failed sign-in attempts. Wait 15 minutes and try again.',
        'err_otp_restart': 'Too many wrong codes. Sign in with your password again.',
    },
    'nl': {
        'title': 'Aanmelden', 'tagline': 'Beheeromgeving voor clusters',
        'sub': 'Meld je aan om het dashboard te openen.',
        'sub_otp_next': 'Na het aanmelden wordt om een verificatiecode gevraagd.',
        'email': 'E-mailadres', 'password': 'Wachtwoord', 'sign_in': 'Aanmelden', 'continue': 'Verder',
        'forgot': 'Wachtwoord vergeten?',
        'otp_title': 'Tweestapsverificatie', 'otp_sub': 'Voer de 6-cijferige code uit je authenticator-app in.',
        'otp_label': 'Verificatiecode', 'otp_hint': 'De code vernieuwt elke 30 seconden.', 'verify': 'Controleren',
        'wrong_account': 'Verkeerd account?', 'start_over': 'Opnieuw beginnen',
        'err_login': 'Onjuist e-mailadres of wachtwoord.',
        'err_otp': 'Onjuiste code. Controleer je authenticator-app en probeer het opnieuw.',
        'err_invited': 'Er is een uitnodiging om een nieuw wachtwoord te kiezen naar dit adres gestuurd. Gebruik de link daarin.',
        'err_needs_email': 'Je account heeft een e-mailadres nodig. Een beheerder stuurt je een uitnodiging.',
        'err_awaiting': 'Je account wacht tot een beheerder er toegang aan geeft.',
        'err_disabled': 'Je account is uitgeschakeld. Vraag het een beheerder.',
        'err_locked': 'Te veel mislukte aanmeldpogingen. Wacht 15 minuten en probeer het opnieuw.',
        'err_otp_restart': 'Te veel onjuiste codes. Meld je opnieuw aan met je wachtwoord.',
    },
    'de': {
        'title': 'Anmelden', 'tagline': 'Verwaltungsoberfläche für Cluster',
        'sub': 'Melden Sie sich an, um das Dashboard zu öffnen.',
        'sub_otp_next': 'Nach der Anmeldung werden Sie nach einem Bestätigungscode gefragt.',
        'email': 'E-Mail-Adresse', 'password': 'Passwort', 'sign_in': 'Anmelden', 'continue': 'Weiter',
        'forgot': 'Passwort vergessen?',
        'otp_title': 'Zwei-Faktor-Authentifizierung', 'otp_sub': 'Geben Sie den 6-stelligen Code aus Ihrer Authenticator-App ein.',
        'otp_label': 'Bestätigungscode', 'otp_hint': 'Der Code erneuert sich alle 30 Sekunden.', 'verify': 'Prüfen',
        'wrong_account': 'Falsches Konto?', 'start_over': 'Neu beginnen',
        'err_login': 'E-Mail-Adresse oder Passwort ist falsch.',
        'err_otp': 'Ungültiger Code. Prüfen Sie Ihre Authenticator-App und versuchen Sie es erneut.',
        'err_invited': 'An diese Adresse wurde eine Einladung zum Festlegen eines neuen Passworts gesendet. Verwenden Sie den Link darin.',
        'err_needs_email': 'Ihr Konto braucht eine E-Mail-Adresse. Ein Administrator sendet Ihnen eine Einladung.',
        'err_awaiting': 'Ihr Konto wartet darauf, dass ein Administrator ihm Zugriff gibt.',
        'err_disabled': 'Ihr Konto ist deaktiviert. Wenden Sie sich an einen Administrator.',
        'err_locked': 'Zu viele fehlgeschlagene Anmeldeversuche. Warten Sie 15 Minuten und versuchen Sie es erneut.',
        'err_otp_restart': 'Zu viele falsche Codes. Melden Sie sich erneut mit Ihrem Passwort an.',
    },
    'fr': {
        'title': 'Connexion', 'tagline': 'Interface de gestion des clusters',
        'sub': 'Saisissez vos identifiants pour accéder au tableau de bord.',
        'sub_otp_next': 'Un code de vérification vous sera demandé après la connexion.',
        'email': 'Adresse e-mail', 'password': 'Mot de passe', 'sign_in': 'Se connecter', 'continue': 'Continuer',
        'forgot': 'Mot de passe oublié ?',
        'otp_title': 'Authentification à deux facteurs', 'otp_sub': 'Saisissez le code à 6 chiffres de votre application d\'authentification.',
        'otp_label': 'Code de vérification', 'otp_hint': 'Le code change toutes les 30 secondes.', 'verify': 'Vérifier',
        'wrong_account': 'Mauvais compte ?', 'start_over': 'Recommencer',
        'err_login': 'Adresse e-mail ou mot de passe incorrect.',
        'err_otp': 'Code incorrect. Vérifiez votre application d\'authentification et réessayez.',
        'err_invited': 'Une invitation à choisir un nouveau mot de passe a été envoyée à cette adresse. Utilisez le lien qu\'elle contient.',
        'err_needs_email': 'Votre compte a besoin d\'une adresse e-mail. Un administrateur vous enverra une invitation.',
        'err_awaiting': 'Votre compte attend qu\'un administrateur lui attribue un accès.',
        'err_disabled': 'Votre compte est désactivé. Adressez-vous à un administrateur.',
        'err_locked': 'Trop de tentatives de connexion échouées. Attendez 15 minutes et réessayez.',
        'err_otp_restart': 'Trop de codes incorrects. Reconnectez-vous avec votre mot de passe.',
    },
}


def lang(request) -> str:
    cookie = (request.cookies.get('hcm_lang') or '')[:2]
    return cookie if cookie in TEXTS else (request.accept_languages.best_match(list(TEXTS)) or 'en')
