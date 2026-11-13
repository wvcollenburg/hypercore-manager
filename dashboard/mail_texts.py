"""
mail_texts.py — the texts of the mails HCM sends, in the dashboard's four languages (en, nl, de, fr).

Plain text only: it survives every mail client and relay, and nothing in it can be rendered differently
from what was meant. Each entry is (subject, body) with {placeholders}; mailer.render() fills them and
falls back to English for an unknown language. Add a kind in all four languages at once.
"""
from __future__ import annotations

LANGS = ('en', 'nl', 'de', 'fr')

TEXTS: dict[str, dict[str, tuple[str, str]]] = {
    'en': {
        'invite_new': ("You are invited to HyperCore Manager",
                       "You have been given access to {layer} in HyperCore Manager, as {role}.\n\n"
                       "Open this link to confirm your email address and choose a password:\n\n{link}\n\n"
                       "The link works once and for {hours} hours. If you did not expect this, ignore this mail.\n"),
        'invite_existing': ("New access in HyperCore Manager",
                            "You have been given access to {layer} in HyperCore Manager, as {role}.\n\n"
                            "Open this link and sign in with this email address to accept it:\n\n{link}\n\n"
                            "The link works once and for {hours} hours. If you did not expect this, ignore this mail.\n"),
        'activate': ("Your HyperCore Manager account needs a new password",
                     "Your HyperCore Manager account now signs in with this email address.\n\n"
                     "Open this link to confirm the address and choose a new password:\n\n{link}\n\n"
                     "The link works once and for {hours} hours. If you did not expect this, ignore this mail.\n"),
        'test': ("HyperCore Manager: test mail",
                 "This is a test mail from HyperCore Manager, sent by {actor}.\n\n"
                 "If you can read this, mail from HyperCore Manager reaches you.\n"),
    },
    'nl': {
        'invite_new': ("Je bent uitgenodigd voor HyperCore Manager",
                       "Je hebt toegang gekregen tot {layer} in HyperCore Manager, als {role}.\n\n"
                       "Open deze link om je e-mailadres te bevestigen en een wachtwoord te kiezen:\n\n{link}\n\n"
                       "De link werkt één keer en {hours} uur lang. Verwachtte je dit niet, negeer deze mail dan.\n"),
        'invite_existing': ("Nieuwe toegang in HyperCore Manager",
                            "Je hebt toegang gekregen tot {layer} in HyperCore Manager, als {role}.\n\n"
                            "Open deze link en log in met dit e-mailadres om de toegang te aanvaarden:\n\n{link}\n\n"
                            "De link werkt één keer en {hours} uur lang. Verwachtte je dit niet, negeer deze mail dan.\n"),
        'activate': ("Je HyperCore Manager-account heeft een nieuw wachtwoord nodig",
                     "Je HyperCore Manager-account logt voortaan in met dit e-mailadres.\n\n"
                     "Open deze link om het adres te bevestigen en een nieuw wachtwoord te kiezen:\n\n{link}\n\n"
                     "De link werkt één keer en {hours} uur lang. Verwachtte je dit niet, negeer deze mail dan.\n"),
        'test': ("HyperCore Manager: testmail",
                 "Dit is een testmail van HyperCore Manager, verstuurd door {actor}.\n\n"
                 "Als je dit kunt lezen, komt mail van HyperCore Manager bij je aan.\n"),
    },
    'de': {
        'invite_new': ("Sie sind zu HyperCore Manager eingeladen",
                       "Sie haben Zugriff auf {layer} in HyperCore Manager erhalten, als {role}.\n\n"
                       "Öffnen Sie diesen Link, um Ihre E-Mail-Adresse zu bestätigen und ein Passwort zu wählen:\n\n{link}\n\n"
                       "Der Link funktioniert einmal und {hours} Stunden lang. Wenn Sie das nicht erwartet haben, ignorieren Sie diese Mail.\n"),
        'invite_existing': ("Neuer Zugriff in HyperCore Manager",
                            "Sie haben Zugriff auf {layer} in HyperCore Manager erhalten, als {role}.\n\n"
                            "Öffnen Sie diesen Link und melden Sie sich mit dieser E-Mail-Adresse an, um ihn anzunehmen:\n\n{link}\n\n"
                            "Der Link funktioniert einmal und {hours} Stunden lang. Wenn Sie das nicht erwartet haben, ignorieren Sie diese Mail.\n"),
        'activate': ("Ihr HyperCore-Manager-Konto braucht ein neues Passwort",
                     "Ihr HyperCore-Manager-Konto meldet sich jetzt mit dieser E-Mail-Adresse an.\n\n"
                     "Öffnen Sie diesen Link, um die Adresse zu bestätigen und ein neues Passwort zu wählen:\n\n{link}\n\n"
                     "Der Link funktioniert einmal und {hours} Stunden lang. Wenn Sie das nicht erwartet haben, ignorieren Sie diese Mail.\n"),
        'test': ("HyperCore Manager: Testmail",
                 "Dies ist eine Testmail von HyperCore Manager, gesendet von {actor}.\n\n"
                 "Wenn Sie dies lesen können, erreicht Sie Mail von HyperCore Manager.\n"),
    },
    'fr': {
        'invite_new': ("Vous êtes invité à HyperCore Manager",
                       "Vous avez reçu un accès à {layer} dans HyperCore Manager, en tant que {role}.\n\n"
                       "Ouvrez ce lien pour confirmer votre adresse e-mail et choisir un mot de passe :\n\n{link}\n\n"
                       "Le lien fonctionne une fois, pendant {hours} heures. Si vous ne vous y attendiez pas, ignorez cet e-mail.\n"),
        'invite_existing': ("Nouvel accès dans HyperCore Manager",
                            "Vous avez reçu un accès à {layer} dans HyperCore Manager, en tant que {role}.\n\n"
                            "Ouvrez ce lien et connectez-vous avec cette adresse e-mail pour l'accepter :\n\n{link}\n\n"
                            "Le lien fonctionne une fois, pendant {hours} heures. Si vous ne vous y attendiez pas, ignorez cet e-mail.\n"),
        'activate': ("Votre compte HyperCore Manager a besoin d'un nouveau mot de passe",
                     "Votre compte HyperCore Manager se connecte désormais avec cette adresse e-mail.\n\n"
                     "Ouvrez ce lien pour confirmer l'adresse et choisir un nouveau mot de passe :\n\n{link}\n\n"
                     "Le lien fonctionne une fois, pendant {hours} heures. Si vous ne vous y attendiez pas, ignorez cet e-mail.\n"),
        'test': ("HyperCore Manager : e-mail de test",
                 "Ceci est un e-mail de test de HyperCore Manager, envoyé par {actor}.\n\n"
                 "Si vous pouvez lire ceci, les e-mails de HyperCore Manager vous parviennent.\n"),
    },
}
