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
        'reset': ("Reset your HyperCore Manager password",
                  "Someone asked to reset the password of the HyperCore Manager account for this address.\n\n"
                  "Open this link to choose a new password:\n\n{link}\n\n"
                  "The link works once and for {minutes} minutes. If it was not you, ignore this mail: nothing changes.\n"),
        'email_confirm': ("Confirm your new address for HyperCore Manager",
                          "Your HyperCore Manager account is to sign in with this address from now on: {new}\n\n"
                          "Open this link to confirm it:\n\n{link}\n\n"
                          "The link works once and for {minutes} minutes. If you did not ask for this, ignore this mail.\n"),
        'email_notice': ("Your HyperCore Manager address was changed",
                         "Your HyperCore Manager account no longer signs in with this address; it now uses {new}.\n\n"
                         "If you did not do this, contact your administrator at once.\n"),
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
        'guest_request': ("HyperCore Manager: console access requested for {vm}",
                          "Someone asks for outside console access to a VM you may give it for.\n\n"
                          "VM: {vm} (cluster {cluster})\nGuest: {guest}\n\n"
                          "Approve or reject it in HyperCore Manager:\n\n{link}\n\n"
                          "Nothing is granted until someone approves it there. Anyone who may give this access can decide; "
                          "if a colleague already did, the link shows it.\n"),
        'verify_failed': ("HyperCore Manager: backup verification failed in {folder}",
                          "{n} restore point(s) in {folder} no longer match the checksums written when they were made. "
                          "A restore from these points, or from later points of the same chain, cannot be trusted.\n\n"
                          "{items}\n\n"
                          "Check the storage share, then make a new full backup "
                          "of these VMs (a new chain) as soon as you can.\n\n"
                          "HyperCore Manager: {link}\n\n"
                          "You receive this as the closest administrator of this folder.\n"),
        'verify_overdue': ("HyperCore Manager: backups in {folder} are not being verified",
                           "{n} backup chain(s) in {folder} have restore points that were not verified for twice the folder's interval.\n\n"
                           "{items}\n\n"
                           "Usually the proxies get too little time: widen the folder's verification hours, mark a passive cluster as "
                           "DR so its proxies verify around the clock, or check that the proxies are up.\n\n"
                           "HyperCore Manager: {link}\n\n"
                           "You receive this as the closest administrator of this folder.\n"),
        'test_failed': ('HyperCore Manager: test restore failed in {folder}',
                        '{n} test restore(s) in {folder} failed: the newest backup of these VMs was restored as a test VM and did not come up as it should.\n\n{items}\n\nThe backup may not be usable for a real restore. Look at the screenshot under Backup → Runs → Test restores, fix the cause and run the test again.\n\nHyperCore Manager: {link}\n\nYou receive this as the closest administrator of this folder.\n'),
        'test_overdue': ('HyperCore Manager: VMs in {folder} are not being test-restored',
                         "{n} VM(s) in a test plan of {folder} have had no passed test restore for twice the plan's interval.\n\n{items}\n\nCheck that the plan is enabled and that its target cluster has room and time for the tests.\n\nHyperCore Manager: {link}\n\nYou receive this as the closest administrator of this folder.\n"),
        'test': ("HyperCore Manager: test mail",
                 "This is a test mail from HyperCore Manager, sent by {actor}.\n\n"
                 "If you can read this, mail from HyperCore Manager reaches you.\n"),
    },
    'nl': {
        'reset': ("Je HyperCore Manager-wachtwoord herstellen",
                  "Iemand vroeg om het wachtwoord te herstellen van het HyperCore Manager-account met dit adres.\n\n"
                  "Open deze link om een nieuw wachtwoord te kiezen:\n\n{link}\n\n"
                  "De link werkt één keer en {minutes} minuten lang. Was jij het niet, negeer deze mail dan: er verandert niets.\n"),
        'email_confirm': ("Bevestig je nieuwe adres voor HyperCore Manager",
                          "Je HyperCore Manager-account logt voortaan in met dit adres: {new}\n\n"
                          "Open deze link om het te bevestigen:\n\n{link}\n\n"
                          "De link werkt één keer en {minutes} minuten lang. Heb je hier niet om gevraagd, negeer deze mail dan.\n"),
        'email_notice': ("Je HyperCore Manager-adres is gewijzigd",
                         "Je HyperCore Manager-account logt niet meer in met dit adres; het gebruikt nu {new}.\n\n"
                         "Heb je dit niet zelf gedaan, neem dan meteen contact op met je beheerder.\n"),
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
        'guest_request': ("HyperCore Manager: consoletoegang aangevraagd voor {vm}",
                          "Iemand vraagt externe consoletoegang tot een VM waarvoor jij die toegang mag geven.\n\n"
                          "VM: {vm} (cluster {cluster})\nGast: {guest}\n\n"
                          "Keur de aanvraag goed of af in HyperCore Manager:\n\n{link}\n\n"
                          "Er wordt niets gegeven tot iemand de aanvraag daar goedkeurt. Iedereen die deze toegang mag geven kan beslissen; "
                          "heeft een collega dat al gedaan, dan toont de link het.\n"),
        'verify_failed': ("HyperCore Manager: controle van back-ups mislukt in {folder}",
                          "{n} herstelpunt(en) in {folder} komen niet meer overeen met de controlesommen van toen ze gemaakt werden. "
                          "Een herstel van deze punten, of van latere punten van dezelfde keten, is niet betrouwbaar.\n\n"
                          "{items}\n\n"
                          "Controleer de opslagshare en maak zo snel mogelijk "
                          "een nieuwe volledige back-up van deze VM's (een nieuwe keten).\n\n"
                          "HyperCore Manager: {link}\n\n"
                          "Je ontvangt dit als dichtstbijzijnde beheerder van deze map.\n"),
        'verify_overdue': ("HyperCore Manager: back-ups in {folder} worden niet gecontroleerd",
                           "{n} back-upketen(s) in {folder} hebben herstelpunten die twee keer de interval van de map niet gecontroleerd zijn.\n\n"
                           "{items}\n\n"
                           "Meestal krijgen de proxy's te weinig tijd: verruim de controle-uren van de map, markeer een passief cluster "
                           "als DR zodat de proxy's daar de klok rond controleren, of kijk of de proxy's draaien.\n\n"
                           "HyperCore Manager: {link}\n\n"
                           "Je ontvangt dit als dichtstbijzijnde beheerder van deze map.\n"),
        'test_failed': ('HyperCore Manager: testherstel mislukt in {folder}',
                        "{n} testherstel(len) in {folder} mislukt: de nieuwste back-up van deze VM's is als test-VM hersteld en kwam niet op zoals het hoort.\n\n{items}\n\nDe back-up is mogelijk niet bruikbaar voor een echt herstel. Bekijk de schermafbeelding onder Back-up → Runs → Testherstel, los de oorzaak op en test opnieuw.\n\nHyperCore Manager: {link}\n\nJe ontvangt dit als dichtstbijzijnde beheerder van deze map.\n"),
        'test_overdue': ("HyperCore Manager: VM's in {folder} worden niet testhersteld",
                         "{n} VM('s) in een testplan van {folder} hebben twee keer de interval van het plan geen geslaagd testherstel gehad.\n\n{items}\n\nControleer of het plan aan staat en of het doelcluster ruimte en tijd heeft voor de tests.\n\nHyperCore Manager: {link}\n\nJe ontvangt dit als dichtstbijzijnde beheerder van deze map.\n"),
        'test': ("HyperCore Manager: testmail",
                 "Dit is een testmail van HyperCore Manager, verstuurd door {actor}.\n\n"
                 "Als je dit kunt lezen, komt mail van HyperCore Manager bij je aan.\n"),
    },
    'de': {
        'reset': ("Ihr HyperCore-Manager-Passwort zurücksetzen",
                  "Jemand hat das Zurücksetzen des Passworts für das HyperCore-Manager-Konto mit dieser Adresse angefordert.\n\n"
                  "Öffnen Sie diesen Link, um ein neues Passwort zu wählen:\n\n{link}\n\n"
                  "Der Link funktioniert einmal und {minutes} Minuten lang. Waren Sie es nicht, ignorieren Sie diese Mail: es ändert sich nichts.\n"),
        'email_confirm': ("Bestätigen Sie Ihre neue Adresse für HyperCore Manager",
                          "Ihr HyperCore-Manager-Konto meldet sich künftig mit dieser Adresse an: {new}\n\n"
                          "Öffnen Sie diesen Link, um sie zu bestätigen:\n\n{link}\n\n"
                          "Der Link funktioniert einmal und {minutes} Minuten lang. Haben Sie das nicht angefordert, ignorieren Sie diese Mail.\n"),
        'email_notice': ("Ihre HyperCore-Manager-Adresse wurde geändert",
                         "Ihr HyperCore-Manager-Konto meldet sich nicht mehr mit dieser Adresse an; es verwendet jetzt {new}.\n\n"
                         "Wenn Sie das nicht waren, wenden Sie sich sofort an Ihren Administrator.\n"),
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
        'guest_request': ("HyperCore Manager: Konsolenzugang für {vm} beantragt",
                          "Jemand beantragt externen Konsolenzugang zu einer VM, für die Sie diesen Zugang vergeben dürfen.\n\n"
                          "VM: {vm} (Cluster {cluster})\nGast: {guest}\n\n"
                          "Genehmigen oder ablehnen in HyperCore Manager:\n\n{link}\n\n"
                          "Es wird nichts vergeben, bis jemand den Antrag dort genehmigt. Jeder, der diesen Zugang vergeben darf, kann entscheiden; "
                          "hat ein Kollege das schon getan, zeigt der Link es an.\n"),
        'verify_failed': ("HyperCore Manager: Prüfung von Backups in {folder} fehlgeschlagen",
                          "{n} Wiederherstellungspunkt(e) in {folder} stimmen nicht mehr mit den beim Erstellen geschriebenen Prüfsummen überein. "
                          "Einer Wiederherstellung aus diesen Punkten oder aus späteren Punkten derselben Kette ist nicht zu trauen.\n\n"
                          "{items}\n\n"
                          "Prüfen Sie die Speicherfreigabe und erstellen Sie so bald "
                          "wie möglich ein neues Vollbackup dieser VMs (eine neue Kette).\n\n"
                          "HyperCore Manager: {link}\n\n"
                          "Sie erhalten dies als nächstgelegener Administrator dieses Ordners.\n"),
        'verify_overdue': ("HyperCore Manager: Backups in {folder} werden nicht geprüft",
                           "{n} Backup-Kette(n) in {folder} haben Wiederherstellungspunkte, die seit dem doppelten Intervall des Ordners nicht geprüft wurden.\n\n"
                           "{items}\n\n"
                           "Meist bekommen die Proxys zu wenig Zeit: erweitern Sie die Prüfzeiten des Ordners, markieren Sie einen passiven "
                           "Cluster als DR, damit seine Proxys rund um die Uhr prüfen, oder prüfen Sie, ob die Proxys laufen.\n\n"
                           "HyperCore Manager: {link}\n\n"
                           "Sie erhalten dies als nächstgelegener Administrator dieses Ordners.\n"),
        'test_failed': ('HyperCore Manager: Test-Wiederherstellung in {folder} fehlgeschlagen',
                        '{n} Test-Wiederherstellung(en) in {folder} fehlgeschlagen: das neueste Backup dieser VMs wurde als Test-VM wiederhergestellt und startete nicht wie erwartet.\n\n{items}\n\nDas Backup ist für eine echte Wiederherstellung möglicherweise nicht brauchbar. Sehen Sie sich den Screenshot unter Backup → Läufe → Test-Wiederherstellungen an, beheben Sie die Ursache und testen Sie erneut.\n\nHyperCore Manager: {link}\n\nSie erhalten dies als nächstgelegener Administrator dieses Ordners.\n'),
        'test_overdue': ('HyperCore Manager: VMs in {folder} werden nicht testweise wiederhergestellt',
                         '{n} VM(s) in einem Testplan von {folder} hatten seit dem doppelten Intervall des Plans keine erfolgreiche Test-Wiederherstellung.\n\n{items}\n\nPrüfen Sie, ob der Plan aktiv ist und ob sein Zielcluster Platz und Zeit für die Tests hat.\n\nHyperCore Manager: {link}\n\nSie erhalten dies als nächstgelegener Administrator dieses Ordners.\n'),
        'test': ("HyperCore Manager: Testmail",
                 "Dies ist eine Testmail von HyperCore Manager, gesendet von {actor}.\n\n"
                 "Wenn Sie dies lesen können, erreicht Sie Mail von HyperCore Manager.\n"),
    },
    'fr': {
        'reset': ("Réinitialiser votre mot de passe HyperCore Manager",
                  "Quelqu'un a demandé la réinitialisation du mot de passe du compte HyperCore Manager de cette adresse.\n\n"
                  "Ouvrez ce lien pour choisir un nouveau mot de passe :\n\n{link}\n\n"
                  "Le lien fonctionne une fois, pendant {minutes} minutes. Si ce n'était pas vous, ignorez cet e-mail : rien ne change.\n"),
        'email_confirm': ("Confirmez votre nouvelle adresse pour HyperCore Manager",
                          "Votre compte HyperCore Manager se connectera désormais avec cette adresse : {new}\n\n"
                          "Ouvrez ce lien pour la confirmer :\n\n{link}\n\n"
                          "Le lien fonctionne une fois, pendant {minutes} minutes. Si vous ne l'avez pas demandé, ignorez cet e-mail.\n"),
        'email_notice': ("Votre adresse HyperCore Manager a été modifiée",
                         "Votre compte HyperCore Manager ne se connecte plus avec cette adresse ; il utilise maintenant {new}.\n\n"
                         "Si ce n'était pas vous, contactez immédiatement votre administrateur.\n"),
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
        'guest_request': ("HyperCore Manager : accès console demandé pour {vm}",
                          "Quelqu'un demande un accès console externe à une VM pour laquelle vous pouvez donner cet accès.\n\n"
                          "VM : {vm} (cluster {cluster})\nInvité : {guest}\n\n"
                          "Approuvez ou refusez la demande dans HyperCore Manager :\n\n{link}\n\n"
                          "Rien n'est accordé tant que personne ne l'a approuvée là-bas. Toute personne autorisée à donner cet accès peut décider ; "
                          "si un collègue l'a déjà fait, le lien l'indique.\n"),
        'verify_failed': ("HyperCore Manager : échec de la vérification des sauvegardes dans {folder}",
                          "{n} point(s) de restauration dans {folder} ne correspondent plus aux sommes de contrôle écrites lors de leur création. "
                          "Une restauration depuis ces points, ou depuis des points ultérieurs de la même chaîne, n'est pas fiable.\n\n"
                          "{items}\n\n"
                          "Contrôlez le partage de stockage, puis faites dès que "
                          "possible une nouvelle sauvegarde complète de ces VM (une nouvelle chaîne).\n\n"
                          "HyperCore Manager : {link}\n\n"
                          "Vous recevez ce message en tant qu'administrateur le plus proche de ce dossier.\n"),
        'verify_overdue': ("HyperCore Manager : les sauvegardes de {folder} ne sont pas vérifiées",
                           "{n} chaîne(s) de sauvegarde dans {folder} ont des points de restauration non vérifiés depuis deux fois l'intervalle du dossier.\n\n"
                           "{items}\n\n"
                           "En général les proxys ont trop peu de temps : élargissez les heures de vérification du dossier, marquez un cluster "
                           "passif comme PRA pour que ses proxys vérifient en continu, ou vérifiez que les proxys tournent.\n\n"
                           "HyperCore Manager : {link}\n\n"
                           "Vous recevez ce message en tant qu'administrateur le plus proche de ce dossier.\n"),
        'test_failed': ('HyperCore Manager : échec de restauration de test dans {folder}',
                        "{n} restauration(s) de test dans {folder} ont échoué : la sauvegarde la plus récente de ces VM a été restaurée comme VM de test et n'a pas démarré comme prévu.\n\n{items}\n\nLa sauvegarde n'est peut-être pas utilisable pour une vraie restauration. Consultez la capture d'écran dans Sauvegarde → Exécutions → Restaurations de test, corrigez la cause et relancez le test.\n\nHyperCore Manager : {link}\n\nVous recevez ce message en tant qu'administrateur le plus proche de ce dossier.\n"),
        'test_overdue': ('HyperCore Manager : les VM de {folder} ne sont pas testées par restauration',
                         "{n} VM d'un plan de test de {folder} n'ont eu aucune restauration de test réussie depuis deux fois l'intervalle du plan.\n\n{items}\n\nVérifiez que le plan est actif et que son cluster cible a la place et le temps pour les tests.\n\nHyperCore Manager : {link}\n\nVous recevez ce message en tant qu'administrateur le plus proche de ce dossier.\n"),
        'test': ("HyperCore Manager : e-mail de test",
                 "Ceci est un e-mail de test de HyperCore Manager, envoyé par {actor}.\n\n"
                 "Si vous pouvez lire ceci, les e-mails de HyperCore Manager vous parviennent.\n"),
    },
}
