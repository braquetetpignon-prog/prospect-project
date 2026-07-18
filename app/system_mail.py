"""
E-mails envoyés PAR ClickProspect lui-même (alertes de compte, nouveautés) —
à ne pas confondre avec les campagnes envoyées par un client via son propre
SMTP (app/sending.py, app/workspace_settings.py). Utilise un compte SMTP
dédié, configuré uniquement par variables d'environnement (jamais saisi ni
vu côté code, même règle que pour toutes les autres clés/mots de passe) :

    SYSTEM_SMTP_HOST
    SYSTEM_SMTP_PORT
    SYSTEM_SMTP_USERNAME
    SYSTEM_SMTP_PASSWORD
    SYSTEM_SMTP_FROM_EMAIL

Si ces variables ne sont pas définies, l'envoi est silencieusement ignoré
(retourne False) plutôt que de faire planter le job qui l'appelle — utile
tant qu'alexis n'a pas encore configuré ce compte.
"""
import os
import smtplib
from email.mime.text import MIMEText


class SystemMailError(Exception):
    pass


def _get_config():
    host = os.environ.get("SYSTEM_SMTP_HOST")
    port_raw = os.environ.get("SYSTEM_SMTP_PORT")
    username = os.environ.get("SYSTEM_SMTP_USERNAME")
    password = os.environ.get("SYSTEM_SMTP_PASSWORD")
    from_email = os.environ.get("SYSTEM_SMTP_FROM_EMAIL")
    if not all([host, port_raw, username, password, from_email]):
        return None
    try:
        port = int(str(port_raw).strip())
    except ValueError:
        raise SystemMailError(
            f"SYSTEM_SMTP_PORT doit être un nombre (ex: 587) — valeur actuelle : {port_raw!r}"
        )
    return {
        "host": host.strip(),
        "port": port,
        "username": username.strip(),
        "password": password,
        "from_email": from_email.strip(),
    }


def is_configured():
    return all([
        os.environ.get("SYSTEM_SMTP_HOST"),
        os.environ.get("SYSTEM_SMTP_PORT"),
        os.environ.get("SYSTEM_SMTP_USERNAME"),
        os.environ.get("SYSTEM_SMTP_PASSWORD"),
        os.environ.get("SYSTEM_SMTP_FROM_EMAIL"),
    ])


def send_system_email(to_email, subject, body, reply_to=None):
    """Retourne True si envoyé, False si le SMTP système n'est pas configuré.
    Lève SystemMailError en cas d'échec d'envoi (erreur SMTP).
    reply_to (optionnel) : permet par exemple de répondre directement à un
    visiteur ayant utilisé le formulaire de contact, sans exposer son adresse
    comme expéditeur réel (qui reste toujours SYSTEM_SMTP_FROM_EMAIL)."""
    config = _get_config()
    if not config:
        return False

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = config["from_email"]
    msg["To"] = to_email
    if reply_to:
        msg["Reply-To"] = reply_to

    port = config["port"]
    try:
        if port == 465:
            server = smtplib.SMTP_SSL(config["host"], port, timeout=20)
        else:
            server = smtplib.SMTP(config["host"], port, timeout=20)
            server.ehlo()
            if server.has_extn("STARTTLS"):
                server.starttls()
                server.ehlo()
        try:
            server.login(config["username"], config["password"])
            server.sendmail(config["from_email"], [to_email], msg.as_string())
        finally:
            server.quit()
    except Exception as exc:
        raise SystemMailError(f"Échec d'envoi de l'e-mail système : {exc}") from exc

    return True
