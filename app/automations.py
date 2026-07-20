"""
Automatisations conditionnelles légères (chantier 5 de la feuille de route
CRM) : des règles simples, définies par l'admin d'un espace, qui déclenchent
une notification interne à l'équipe — jamais un e-mail au prospect.

Deux déclencheurs :
  - statut_stagnant : un prospect reste dans le même statut depuis plus de
    N jours (basé sur updated_at, ou created_at si jamais modifié)
  - rappel_depasse : la date de rappel (prospects.prochaine_action_date)
    est dépassée depuis plus de N jours (N=0 : dès le lendemain de la date)

Appelée depuis app/scheduler.py, qui tourne déjà en tâche de fond — mais
cette fonction ne fait réellement le travail qu'une fois par heure (porte
via app_settings, même mécanisme que app/lifecycle.py pour la maintenance
quotidienne), pas à chaque passage du planificateur (toutes les 30s).
"""
from datetime import datetime, timedelta, timezone

from app.db import get_db

TRIGGER_TYPES = ("statut_stagnant", "rappel_depasse")
CHECK_INTERVAL = timedelta(hours=1)
_LAST_RUN_KEY = "automations_last_run_at"


class AutomationError(Exception):
    pass


# --- Gestion des règles (admin) -----------------------------------------

def list_rules(workspace_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, trigger_type, statut, seuil_jours, actif, created_at
                FROM automation_rules WHERE workspace_id = %s ORDER BY created_at DESC
                """,
                (workspace_id,),
            )
            cols = ["id", "trigger_type", "statut", "seuil_jours", "actif", "created_at"]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def create_rule(workspace_id, trigger_type, statut, seuil_jours):
    if trigger_type not in TRIGGER_TYPES:
        raise AutomationError("Type de déclencheur invalide.")
    if trigger_type == "statut_stagnant" and not statut:
        raise AutomationError("Un statut est requis pour ce type de règle.")
    if trigger_type == "rappel_depasse":
        statut = None
    try:
        seuil_jours = int(seuil_jours)
    except (TypeError, ValueError):
        raise AutomationError("Le seuil doit être un nombre de jours.")
    if seuil_jours < 0 or seuil_jours > 365:
        raise AutomationError("Le seuil doit être compris entre 0 et 365 jours.")

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO automation_rules (workspace_id, trigger_type, statut, seuil_jours)
                VALUES (%s, %s, %s, %s) RETURNING id
                """,
                (workspace_id, trigger_type, statut, seuil_jours),
            )
            rule_id = cur.fetchone()[0]
        conn.commit()
        return rule_id
    finally:
        conn.close()


def set_rule_active(rule_id, workspace_id, actif):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE automation_rules SET actif = %s WHERE id = %s AND workspace_id = %s RETURNING id",
                (bool(actif), rule_id, workspace_id),
            )
            updated = cur.fetchone()
        conn.commit()
        if not updated:
            raise AutomationError("Règle introuvable dans cet espace de travail.")
    finally:
        conn.close()


def delete_rule(rule_id, workspace_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM automation_rules WHERE id = %s AND workspace_id = %s RETURNING id",
                (rule_id, workspace_id),
            )
            deleted = cur.fetchone()
        conn.commit()
        if not deleted:
            raise AutomationError("Règle introuvable dans cet espace de travail.")
    finally:
        conn.close()


# --- Notifications (toute l'équipe) -------------------------------------

def list_notifications(workspace_id, limit=30):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT n.id, n.prospect_id, p.nom_entreprise, n.message, n.read_at, n.created_at
                FROM team_notifications n
                LEFT JOIN prospects p ON p.id = n.prospect_id
                WHERE n.workspace_id = %s
                ORDER BY n.created_at DESC LIMIT %s
                """,
                (workspace_id, limit),
            )
            cols = ["id", "prospect_id", "prospect_nom", "message", "read_at", "created_at"]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def count_unread(workspace_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM team_notifications WHERE workspace_id = %s AND read_at IS NULL",
                (workspace_id,),
            )
            return cur.fetchone()[0]
    finally:
        conn.close()


def mark_read(notification_id, workspace_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE team_notifications SET read_at = now() WHERE id = %s AND workspace_id = %s AND read_at IS NULL",
                (notification_id, workspace_id),
            )
        conn.commit()
    finally:
        conn.close()


def mark_all_read(workspace_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE team_notifications SET read_at = now() WHERE workspace_id = %s AND read_at IS NULL",
                (workspace_id,),
            )
        conn.commit()
    finally:
        conn.close()


# --- Vérification périodique (planificateur) ----------------------------

def _should_run():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM app_settings WHERE key = %s", (_LAST_RUN_KEY,))
            row = cur.fetchone()
        if not row or not row[0]:
            return True
        last_run = datetime.fromisoformat(row[0])
        return datetime.now(timezone.utc) - last_run >= CHECK_INTERVAL
    finally:
        conn.close()


def _mark_ran():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app_settings (key, value, updated_at) VALUES (%s, %s, now())
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
                """,
                (_LAST_RUN_KEY, datetime.now(timezone.utc).isoformat()),
            )
        conn.commit()
    finally:
        conn.close()


def _check_statut_stagnant(cur, rule):
    cur.execute(
        """
        SELECT id, nom_entreprise FROM prospects
        WHERE workspace_id = %s AND statut = %s
          AND COALESCE(updated_at, created_at) < now() - (%s || ' days')::interval
        """,
        (rule["workspace_id"], rule["statut"], rule["seuil_jours"]),
    )
    return cur.fetchall()


def _check_rappel_depasse(cur, rule):
    cur.execute(
        """
        SELECT id, nom_entreprise FROM prospects
        WHERE workspace_id = %s AND prochaine_action_date IS NOT NULL
          AND prochaine_action_date + (%s || ' days')::interval < now()
        """,
        (rule["workspace_id"], rule["seuil_jours"]),
    )
    return cur.fetchall()


def run_due_automations():
    """Point d'entrée appelé par le planificateur. Ne fait le travail réel
    qu'une fois par heure (voir _should_run), quel que soit le rythme
    d'appel — évite de scanner tous les espaces toutes les 30 secondes."""
    if not _should_run():
        return
    _mark_ran()

    # is_restricted n'est volontairement pas vérifié ici : si un espace est
    # repassé en Gratuit après avoir créé des règles, on arrête simplement
    # de les évaluer côté API (list_rules reste accessible en lecture), pas
    # besoin de dupliquer cette logique métier ici — les règles orphelines
    # ne coûtent qu'une requête vide.
    from app import subscriptions

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, workspace_id, trigger_type, statut, seuil_jours FROM automation_rules WHERE actif = TRUE")
            rule_cols = ["id", "workspace_id", "trigger_type", "statut", "seuil_jours"]
            rules = [dict(zip(rule_cols, r)) for r in cur.fetchall()]

            for rule in rules:
                if subscriptions.is_restricted(rule["workspace_id"]):
                    continue

                if rule["trigger_type"] == "statut_stagnant":
                    matches = _check_statut_stagnant(cur, rule)
                    verb = f"reste au statut « {rule['statut']} » depuis plus de {rule['seuil_jours']} jour(s)"
                else:
                    matches = _check_rappel_depasse(cur, rule)
                    verb = "a une date de rappel dépassée"

                for prospect_id, nom in matches:
                    message = f"{nom} {verb}."
                    cur.execute(
                        """
                        INSERT INTO team_notifications (workspace_id, prospect_id, rule_id, message)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (workspace_id, prospect_id, rule_id) WHERE read_at IS NULL DO NOTHING
                        """,
                        (rule["workspace_id"], prospect_id, rule["id"], message),
                    )
        conn.commit()
    finally:
        conn.close()
