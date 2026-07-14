"""
Maintenance automatique quotidienne : repère les espaces de travail en
version gratuite restreinte, inactifs depuis INACTIVITY_DAYS jours, envoie
une alerte e-mail à leur(s) administrateur(s), et les place dans la file de
"demande de suppression" du superadmin.

Important : cette tâche ne supprime JAMAIS rien elle-même. Elle se contente
de signaler (`deletion_requested_at`) — la suppression réelle est toujours
une action manuelle du superadmin, pour se prémunir d'un faux positif (ex:
bug de calcul, période d'inactivité légitime d'un client saisonnier...).
Si l'administrateur se reconnecte entre-temps, `auth.login()` efface
automatiquement le signalement (voir app/auth.py).

Appelée depuis app/scheduler.py, qui tourne déjà en tâche de fond — mais
cette fonction ne fait réellement le travail qu'une fois par jour (portes
via app_settings), pas à chaque passage du planificateur (toutes les 30s).
"""
from datetime import datetime, timedelta, timezone

from app.db import get_db
from app import subscriptions
from app import system_mail

INACTIVITY_DAYS = 30
_LAST_RUN_KEY = "lifecycle_last_inactivity_check_date"

WARNING_SUBJECT = "Votre espace ClickProspect va être supprimé prochainement"
WARNING_BODY_TEMPLATE = """Bonjour,

Votre espace de travail ClickProspect « {workspace_name} » est en version
gratuite et n'a montré aucune activité depuis {days} jours.

Sans connexion de votre part, cet espace sera examiné pour suppression par
notre équipe. Pour l'éviter, il suffit de vous reconnecter :
https://clickprospect.fr/login

Si vous pensez qu'il s'agit d'une erreur, ou pour toute question, répondez
simplement à cet e-mail.

— L'équipe ClickProspect
"""


def _already_ran_today():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM app_settings WHERE key = %s", (_LAST_RUN_KEY,))
            row = cur.fetchone()
        return bool(row and row[0] == datetime.now(timezone.utc).date().isoformat())
    finally:
        conn.close()


def _mark_ran_today():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app_settings (key, value, updated_at) VALUES (%s, %s, now())
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
                """,
                (_LAST_RUN_KEY, datetime.now(timezone.utc).date().isoformat()),
            )
        conn.commit()
    finally:
        conn.close()


def flag_inactive_free_workspaces():
    """Fait le travail réel (pas de porte anti-doublon ici — voir
    run_daily_maintenance pour ça). Retourne la liste des workspace_id signalés."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=INACTIVITY_DAYS)

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, plan, trial_ends_at, paid_until
                FROM workspaces
                WHERE deletion_requested_at IS NULL AND last_active_at < %s
                """,
                (cutoff,),
            )
            candidates = cur.fetchall()
    finally:
        conn.close()

    flagged = []
    for workspace_id, name, plan, trial_ends_at, paid_until in candidates:
        if subscriptions.effective_plan(plan, trial_ends_at, paid_until) != "free":
            continue  # essai ou payant : jamais concerné, quelle que soit l'inactivité

        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE workspaces SET deletion_requested_at = now() WHERE id = %s",
                    (workspace_id,),
                )
                cur.execute(
                    "SELECT email FROM users WHERE workspace_id = %s AND role = 'admin'",
                    (workspace_id,),
                )
                admin_emails = [r[0] for r in cur.fetchall()]
            conn.commit()
        finally:
            conn.close()

        for email in admin_emails:
            try:
                system_mail.send_system_email(
                    email,
                    WARNING_SUBJECT,
                    WARNING_BODY_TEMPLATE.format(workspace_name=name, days=INACTIVITY_DAYS),
                )
            except system_mail.SystemMailError:
                pass  # ne bloque jamais le signalement pour un souci d'envoi ponctuel

        flagged.append(workspace_id)

    return flagged


def run_daily_maintenance():
    if _already_ran_today():
        return
    flag_inactive_free_workspaces()
    _mark_ran_today()
