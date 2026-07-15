"""
Événements de sécurité "self-service" (réinitialisation de mot de passe via
PIN, blocages anti brute-force) : journalisation consultable par le
superadmin + notification par e-mail de la personne concernée et des autres
administrateurs de son espace de travail.

Distinct de superadmin_audit_log, qui trace les actions DU superadmin — ici
ce sont des actions de l'utilisateur lui-même sur son propre compte.

Les e-mails utilisent le SMTP système (jamais celui du client, qui peut ne
pas être configuré/vérifié) — c'est ClickProspect qui informe d'un
événement de sécurité, pas une communication commerciale du client.
"""
from app.db import get_db
from app import system_mail
from app.app_logging import logger


def log_event(workspace_id, workspace_name, user_email, event_type, details, ip_address):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO security_events (workspace_id, workspace_name, user_email, event_type, details, ip_address)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (workspace_id, workspace_name, user_email, event_type, details, ip_address),
            )
        conn.commit()
    finally:
        conn.close()


def list_recent_events(limit=100):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT workspace_id, workspace_name, user_email, event_type, details, ip_address, created_at
                FROM security_events ORDER BY created_at DESC LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
        cols = ["workspace_id", "workspace_name", "user_email", "event_type", "details", "ip_address", "created_at"]
        return [dict(zip(cols, r)) for r in rows]
    finally:
        conn.close()


def notify_password_reset_via_pin(workspace_id, user_email, ip_address):
    """Prévient l'utilisateur concerné (« si ce n'est pas toi... ») et les
    autres administrateurs de son espace. N'interrompt jamais le flux de
    réinitialisation lui-même si l'envoi échoue — c'est une notification,
    pas une condition de succès."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM workspaces WHERE id = %s", (workspace_id,))
            row = cur.fetchone()
            workspace_name = row[0] if row else ""
            cur.execute(
                "SELECT email FROM users WHERE workspace_id = %s AND role = 'admin' AND is_active",
                (workspace_id,),
            )
            admin_emails = [r[0] for r in cur.fetchall()]
    finally:
        conn.close()

    log_event(
        workspace_id, workspace_name, user_email, "password_reset_pin",
        "Mot de passe réinitialisé via code PIN (auto-service, sans e-mail).", ip_address,
    )

    if not system_mail.is_configured():
        return

    try:
        system_mail.send_system_email(
            user_email,
            "Ton mot de passe ClickProspect a été modifié",
            "Bonjour,\n\n"
            "Ton mot de passe vient d'être réinitialisé à l'aide de ton code PIN personnel.\n\n"
            "Si c'est bien toi, tu n'as rien d'autre à faire.\n"
            "Si ce n'est pas toi, contacte immédiatement un administrateur de ton espace de "
            "travail pour sécuriser ton compte.\n\n"
            "— L'équipe ClickProspect",
        )
    except system_mail.SystemMailError:
        logger.exception("Échec de la notification de réinitialisation à %s", user_email)

    for admin_email in admin_emails:
        if admin_email.lower() == user_email.lower():
            continue  # déjà notifié ci-dessus en tant qu'utilisateur concerné
        try:
            system_mail.send_system_email(
                admin_email,
                f"Mot de passe réinitialisé — {user_email}",
                f"Bonjour,\n\n"
                f"Le mot de passe de {user_email} dans « {workspace_name} » vient d'être "
                f"réinitialisé via son code PIN personnel (auto-service, sans intervention de ta part).\n\n"
                f"Si cela te semble anormal, contacte la personne concernée.\n\n"
                f"— L'équipe ClickProspect",
            )
        except system_mail.SystemMailError:
            logger.exception("Échec de la notification admin à %s", admin_email)


def notify_pin_rate_limited(email, ip_address):
    """Événement purement journalisé (pas d'e-mail) — sert à repérer une
    activité suspecte a posteriori depuis /supadmin, sans bruit pour les
    utilisateurs à chaque tentative ratée."""
    log_event(None, None, email, "pin_rate_limited", "Trop de tentatives de réinitialisation par PIN.", ip_address)
