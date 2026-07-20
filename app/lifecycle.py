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
from app import rate_limit
from app import subscriptions
from app import system_mail
from app import mollie_billing

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

WEEKLY_SUMMARY_SUBJECT = "Votre résumé de la semaine — {workspace_name}"
WEEKLY_SUMMARY_TEMPLATE = """Bonjour,

Voici le résumé de la semaine pour « {workspace_name} » :

- Nouveaux prospects ajoutés : {nouveaux_prospects}
- Rendez-vous à venir (7 prochains jours) : {rdv_a_venir}
- Envois de campagnes en attente : {campagnes_en_attente}
- Actions de suivi en retard : {actions_en_retard}

Retrouvez le détail sur https://clickprospect.fr/dashboard

— L'équipe ClickProspect
"""

TRIAL_ENDING_SUBJECT = "Votre essai ClickProspect se termine dans 2 jours"
TRIAL_ENDING_BODY_TEMPLATE = """Bonjour,

Votre essai gratuit de ClickProspect pour « {workspace_name} » se termine
dans 2 jours, le {trial_end_date}.

Passé ce délai, votre espace basculera automatiquement en version gratuite
restreinte (le Pipeline et le Rapport d'équipe, entre autres, ne seront
plus accessibles).

Pour continuer sans interruption avec toutes les fonctionnalités, choisissez
une formule ici :
{app_base_url}/tarifs

— L'équipe ClickProspect
"""

RENEWAL_REMINDER_SUBJECT = "Votre abonnement ClickProspect se renouvelle bientôt"
RENEWAL_REMINDER_BODY_TEMPLATE = """Bonjour,

Votre abonnement annuel ClickProspect pour « {workspace_name} » sera
automatiquement renouvelé le {renewal_date} ({amount} {currency} prélevés
sur la carte enregistrée).

Aucune action n'est nécessaire de votre part si vous souhaitez continuer.
Pour modifier ou annuler votre abonnement :
{app_base_url}/parametres

— L'équipe ClickProspect
"""

FREE_DOWNGRADE_FOLLOWUP_SUBJECT = "Toujours partant pour aller plus loin avec ClickProspect ?"
FREE_DOWNGRADE_FOLLOWUP_BODY_TEMPLATE = """Bonjour,

Votre essai ClickProspect pour « {workspace_name} » est terminé depuis une
semaine, et votre espace est maintenant en version gratuite restreinte (le
Pipeline et le Rapport d'équipe, entre autres, ne sont plus accessibles).

Si vous souhaitez retrouver l'accès complet, les formules sont ici :
{app_base_url}/tarifs

Une question, un frein particulier ? Répondez simplement à cet e-mail.

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


def send_weekly_summaries():
    """Envoie un résumé hebdomadaire à l'admin de chaque espace de travail
    actif, jamais plus d'une fois tous les 7 jours par espace (contrôlé par
    weekly_summary_last_sent_at, indépendamment de la porte quotidienne
    globale de run_daily_maintenance — chaque espace a son propre rythme).
    Utilise le SMTP système (ClickProspect qui informe), pas celui du client."""
    if not system_mail.is_configured():
        return

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name FROM workspaces
                WHERE weekly_summary_last_sent_at IS NULL
                   OR weekly_summary_last_sent_at < now() - interval '7 days'
                """
            )
            due = cur.fetchall()
    finally:
        conn.close()

    for workspace_id, name in due:
        _send_one_weekly_summary(workspace_id, name)


def _send_one_weekly_summary(workspace_id, workspace_name):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT email FROM users WHERE workspace_id = %s AND role = 'admin' AND is_active",
                (workspace_id,),
            )
            admin_emails = [r[0] for r in cur.fetchall()]

            cur.execute(
                "SELECT count(*) FROM prospects WHERE workspace_id = %s AND created_at > now() - interval '7 days'",
                (workspace_id,),
            )
            nouveaux_prospects = cur.fetchone()[0]

            cur.execute(
                """
                SELECT count(*) FROM rendez_vous
                WHERE workspace_id = %s AND date_heure BETWEEN now() AND now() + interval '7 days'
                """,
                (workspace_id,),
            )
            rdv_a_venir = cur.fetchone()[0]

            cur.execute(
                "SELECT count(*) FROM campaign_sends cs JOIN campaigns c ON c.id = cs.campaign_id "
                "WHERE c.workspace_id = %s AND cs.statut = 'planifie'",
                (workspace_id,),
            )
            campagnes_en_attente = cur.fetchone()[0]
    finally:
        conn.close()

    from app import prospects as prospects_module
    actions_en_retard = prospects_module.count_overdue_actions(workspace_id)

    body = WEEKLY_SUMMARY_TEMPLATE.format(
        workspace_name=workspace_name,
        nouveaux_prospects=nouveaux_prospects,
        rdv_a_venir=rdv_a_venir,
        campagnes_en_attente=campagnes_en_attente,
        actions_en_retard=actions_en_retard,
    )
    subject = WEEKLY_SUMMARY_SUBJECT.format(workspace_name=workspace_name)

    for email in admin_emails:
        try:
            system_mail.send_system_email(email, subject, body)
        except system_mail.SystemMailError:
            pass  # ne bloque jamais les autres espaces pour un souci d'envoi ponctuel

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE workspaces SET weekly_summary_last_sent_at = now() WHERE id = %s",
                (workspace_id,),
            )
        conn.commit()
    finally:
        conn.close()


def send_trial_ending_reminders():
    """J-2 avant fin d'essai. Réutilise trial_ending_reminder_sent_at, une
    colonne déjà présente dans schema.sql depuis une préparation antérieure
    mais jamais exploitée jusqu'ici (voir contexte v5, chantier e-mails
    automatiques) — un seul envoi par espace, jamais de doublon même si la
    tâche de fond repasse plusieurs fois pendant la fenêtre des 2 jours."""
    if not system_mail.is_configured():
        return

    now = datetime.now(timezone.utc)
    window_end = now + timedelta(days=2)

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, trial_ends_at FROM workspaces
                WHERE plan = 'trial'
                  AND trial_ending_reminder_sent_at IS NULL
                  AND trial_ends_at IS NOT NULL
                  AND trial_ends_at BETWEEN %s AND %s
                """,
                (now, window_end),
            )
            due = cur.fetchall()
    finally:
        conn.close()

    for workspace_id, name, trial_ends_at in due:
        _send_one_trial_ending_reminder(workspace_id, name, trial_ends_at)


def _send_one_trial_ending_reminder(workspace_id, workspace_name, trial_ends_at):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT email FROM users WHERE workspace_id = %s AND role = 'admin' AND is_active",
                (workspace_id,),
            )
            admin_emails = [r[0] for r in cur.fetchall()]
    finally:
        conn.close()

    body = TRIAL_ENDING_BODY_TEMPLATE.format(
        workspace_name=workspace_name,
        trial_end_date=trial_ends_at.strftime("%d/%m/%Y"),
        app_base_url=mollie_billing._app_base_url(),
    )
    for email in admin_emails:
        try:
            system_mail.send_system_email(email, TRIAL_ENDING_SUBJECT, body)
        except system_mail.SystemMailError:
            pass  # ne bloque jamais les autres espaces pour un souci d'envoi ponctuel

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE workspaces SET trial_ending_reminder_sent_at = now() WHERE id = %s",
                (workspace_id,),
            )
        conn.commit()
    finally:
        conn.close()


def send_annual_renewal_reminders():
    """J-2 avant renouvellement annuel. Dédoublonnée en comparant
    renewal_reminder_sent_for à paid_until (pas un simple NULL/non-NULL) :
    se réarme automatiquement chaque année sans tâche de nettoyage. Ne
    concerne que la formule annuelle — un rappel mensuel reviendrait trop
    souvent pour avoir du sens."""
    if not system_mail.is_configured():
        return

    now = datetime.now(timezone.utc)
    window_end = now + timedelta(days=2)

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, paid_until FROM workspaces
                WHERE plan = 'paid'
                  AND billing_interval = 'annual'
                  AND mollie_subscription_status = 'active'
                  AND paid_until IS NOT NULL
                  AND paid_until BETWEEN %s AND %s
                  AND (renewal_reminder_sent_for IS NULL OR renewal_reminder_sent_for <> paid_until)
                """,
                (now, window_end),
            )
            due = cur.fetchall()
    finally:
        conn.close()

    for workspace_id, name, paid_until in due:
        _send_one_renewal_reminder(workspace_id, name, paid_until)


def _send_one_renewal_reminder(workspace_id, workspace_name, paid_until):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT email FROM users WHERE workspace_id = %s AND role = 'admin' AND is_active",
                (workspace_id,),
            )
            admin_emails = [r[0] for r in cur.fetchall()]
    finally:
        conn.close()

    plan_info = mollie_billing.PLAN_AMOUNTS["annual"]
    body = RENEWAL_REMINDER_BODY_TEMPLATE.format(
        workspace_name=workspace_name,
        renewal_date=paid_until.strftime("%d/%m/%Y"),
        amount=plan_info["value"],
        currency=plan_info["currency"],
        app_base_url=mollie_billing._app_base_url(),
    )
    for email in admin_emails:
        try:
            system_mail.send_system_email(email, RENEWAL_REMINDER_SUBJECT, body)
        except system_mail.SystemMailError:
            pass  # ne bloque jamais les autres espaces pour un souci d'envoi ponctuel

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE workspaces SET renewal_reminder_sent_for = %s WHERE id = %s",
                (paid_until, workspace_id),
            )
        conn.commit()
    finally:
        conn.close()


def send_free_downgrade_followups():
    """Relance à J+7 après la fin d'un essai non converti en abonnement
    payant (voir contexte v5, section 4, point 2 : "bascule en gratuit sans
    conversion"). Couvre volontairement ce seul cas (essai -> gratuit) : un
    abonnement payant qui ne se renouvelle pas est déjà couvert par la
    notification de paiement en échec (mollie_billing._notify_payment_failed),
    qui intervient plus tôt et pour une raison différente (échec de
    prélèvement, pas fin d'essai)."""
    if not system_mail.is_configured():
        return

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=8)
    window_end = now - timedelta(days=7)

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name FROM workspaces
                WHERE plan = 'trial'
                  AND free_downgrade_followup_sent_at IS NULL
                  AND trial_ends_at IS NOT NULL
                  AND trial_ends_at BETWEEN %s AND %s
                """,
                (window_start, window_end),
            )
            due = cur.fetchall()
    finally:
        conn.close()

    for workspace_id, name in due:
        _send_one_free_downgrade_followup(workspace_id, name)


def _send_one_free_downgrade_followup(workspace_id, workspace_name):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT email FROM users WHERE workspace_id = %s AND role = 'admin' AND is_active",
                (workspace_id,),
            )
            admin_emails = [r[0] for r in cur.fetchall()]
    finally:
        conn.close()

    body = FREE_DOWNGRADE_FOLLOWUP_BODY_TEMPLATE.format(
        workspace_name=workspace_name,
        app_base_url=mollie_billing._app_base_url(),
    )
    for email in admin_emails:
        try:
            system_mail.send_system_email(email, FREE_DOWNGRADE_FOLLOWUP_SUBJECT, body)
        except system_mail.SystemMailError:
            pass  # ne bloque jamais les autres espaces pour un souci d'envoi ponctuel

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE workspaces SET free_downgrade_followup_sent_at = now() WHERE id = %s",
                (workspace_id,),
            )
        conn.commit()
    finally:
        conn.close()


def run_daily_maintenance():
    if _already_ran_today():
        return
    flag_inactive_free_workspaces()
    rate_limit.purge_old_attempts()
    send_weekly_summaries()
    send_trial_ending_reminders()
    send_annual_renewal_reminders()
    send_free_downgrade_followups()
    mollie_billing.send_card_expiring_reminders()
    _mark_ran_today()
