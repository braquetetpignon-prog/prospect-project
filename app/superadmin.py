"""
Superadmin : gestion de l'ensemble des espaces de travail (clients), de leur
abonnement, et réinitialisation de mot de passe en cas de besoin. Totalement
séparé des comptes utilisateurs normaux (table dédiée `superadmins`, session
distincte) — accessible uniquement via /supadmin, jamais lié depuis l'app.

Bootstrap : le premier (et normalement unique) compte superadmin est créé
automatiquement au démarrage à partir des variables d'environnement
SUPERADMIN_EMAIL / SUPERADMIN_PASSWORD, définies directement sur Coolify —
jamais saisies ni vues côté code applicatif au-delà de cette création initiale.
Si ces variables ne sont pas définies, ou si un compte existe déjà, rien ne
se passe (idempotent, sûr à appeler à chaque démarrage de chaque worker).
"""
import os
import secrets
from functools import wraps

from flask import session, jsonify, request
from werkzeug.security import generate_password_hash, check_password_hash

from app.db import get_db
from app import subscriptions


class SuperadminError(Exception):
    pass


def ensure_bootstrap_superadmin():
    email = os.environ.get("SUPERADMIN_EMAIL")
    password = os.environ.get("SUPERADMIN_PASSWORD")
    if not email or not password:
        return

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO superadmins (email, password_hash) VALUES (%s, %s) ON CONFLICT (email) DO NOTHING",
                (email.lower().strip(), generate_password_hash(password)),
            )
        conn.commit()
    finally:
        conn.close()


def login(email, password):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, password_hash FROM superadmins WHERE email = %s",
                (email.lower().strip(),),
            )
            row = cur.fetchone()
    finally:
        conn.close()

    if not row or not check_password_hash(row[1], password):
        raise SuperadminError("Adresse e-mail ou mot de passe incorrect.")

    session.clear()
    session["superadmin_id"] = row[0]
    session.permanent = True


def logout():
    session.clear()


def current_superadmin_id():
    return session.get("superadmin_id")


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "superadmin_id" not in session:
            return jsonify(error="Authentification superadmin requise."), 401
        return f(*args, **kwargs)
    return wrapper


# --- Gestion des espaces de travail ----------------------------------------

def list_workspaces():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT w.id, w.name, w.created_at, w.plan, w.trial_ends_at, w.paid_until,
                       w.last_active_at, w.deletion_requested_at,
                       (SELECT email FROM users u WHERE u.workspace_id = w.id AND u.role = 'admin'
                        ORDER BY u.created_at LIMIT 1) AS admin_email,
                       (SELECT count(*) FROM users u WHERE u.workspace_id = w.id) AS member_count
                FROM workspaces w
                ORDER BY w.created_at DESC
                """
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    workspaces = []
    for (wid, name, created_at, plan, trial_ends_at, paid_until, last_active_at,
         deletion_requested_at, admin_email, member_count) in rows:
        effective = subscriptions.effective_plan(plan, trial_ends_at, paid_until)
        workspaces.append({
            "id": wid,
            "name": name,
            "created_at": created_at,
            "admin_email": admin_email,
            "member_count": member_count,
            "plan": plan,
            "plan_effective": effective,
            "trial_ends_at": trial_ends_at,
            "paid_until": paid_until,
            "last_active_at": last_active_at,
            "deletion_requested_at": deletion_requested_at,
        })
    return workspaces


def set_plan(workspace_id, plan, paid_until=None):
    if plan not in ("trial", "free", "paid"):
        raise SuperadminError(f"Statut d'abonnement invalide : {plan}")
    if plan == "paid" and not paid_until:
        raise SuperadminError("Une date de fin est requise pour un abonnement payant.")

    conn = get_db()
    try:
        with conn.cursor() as cur:
            if plan == "trial":
                cur.execute(
                    "UPDATE workspaces SET plan = 'trial', trial_ends_at = %s, paid_until = NULL WHERE id = %s RETURNING id",
                    (subscriptions.trial_end_date(), workspace_id),
                )
            elif plan == "paid":
                cur.execute(
                    "UPDATE workspaces SET plan = 'paid', paid_until = %s WHERE id = %s RETURNING id",
                    (paid_until, workspace_id),
                )
            else:  # free
                cur.execute(
                    "UPDATE workspaces SET plan = 'free', paid_until = NULL WHERE id = %s RETURNING id",
                    (workspace_id,),
                )
            updated = cur.fetchone()
        conn.commit()
        if not updated:
            raise SuperadminError("Espace de travail introuvable.")
    finally:
        conn.close()


def reset_workspace_admin_password(workspace_id):
    """Génère un mot de passe temporaire pour le(s) administrateur(s) de cet
    espace de travail, le renvoie EN CLAIR une seule fois (jamais stocké tel
    quel — seul son hash l'est), et force son changement à la prochaine connexion."""
    temp_password = secrets.token_urlsafe(9)  # ~12 caractères lisibles

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE users SET password_hash = %s, must_change_password = TRUE
                WHERE workspace_id = %s AND role = 'admin'
                RETURNING email
                """,
                (generate_password_hash(temp_password), workspace_id),
            )
            updated = cur.fetchall()
        conn.commit()
        if not updated:
            raise SuperadminError("Aucun administrateur trouvé pour cet espace de travail.")
        return {"emails": [r[0] for r in updated], "temporary_password": temp_password}
    finally:
        conn.close()


def delete_workspace(workspace_id):
    """Suppression DÉFINITIVE et immédiate (sur demande explicite par mail, ou
    après validation manuelle d'une demande automatique pour inactivité). Toutes
    les données liées (prospects, campagnes, rendez-vous...) partent avec, via
    les contraintes ON DELETE CASCADE du schéma — il n'y a pas de retour arrière."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM workspaces WHERE id = %s RETURNING id", (workspace_id,))
            deleted = cur.fetchone()
        conn.commit()
        if not deleted:
            raise SuperadminError("Espace de travail introuvable.")
    finally:
        conn.close()


def dismiss_deletion_request(workspace_id):
    """Annule un signalement automatique (faux positif) sans rien supprimer."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE workspaces SET deletion_requested_at = NULL WHERE id = %s RETURNING id",
                (workspace_id,),
            )
            updated = cur.fetchone()
        conn.commit()
        if not updated:
            raise SuperadminError("Espace de travail introuvable.")
    finally:
        conn.close()


# --- Base de données : état et purge (données obsolètes, jamais les prospects) ---

# Ces seuils déterminent ce qui est considéré "obsolète" pour chaque purge.
STALE_SCHEDULED_RESULTS_DAYS = 30   # résultats de recherche IA planifiée jamais vérifiés
ABANDONED_IMPORT_JOBS_HOURS = 24    # imports CSV commencés puis jamais confirmés


def get_db_stats():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM workspaces")
            workspace_count = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM prospects")
            prospect_count = cur.fetchone()[0]
            cur.execute(
                """
                SELECT count(*) FROM scheduled_search_results
                WHERE statut = 'a_verifier' AND created_at < now() - make_interval(days => %s)
                """,
                (STALE_SCHEDULED_RESULTS_DAYS,),
            )
            stale_scheduled_results = cur.fetchone()[0]
            cur.execute(
                """
                SELECT count(*) FROM import_jobs
                WHERE status IN ('mapping', 'pending', 'processing')
                  AND created_at < now() - make_interval(hours => %s)
                """,
                (ABANDONED_IMPORT_JOBS_HOURS,),
            )
            abandoned_import_jobs = cur.fetchone()[0]
    finally:
        conn.close()

    return {
        "workspace_count": workspace_count,
        "prospect_count": prospect_count,
        "stale_scheduled_results": stale_scheduled_results,
        "abandoned_import_jobs": abandoned_import_jobs,
    }


def purge_stale_scheduled_results():
    """Supprime les résultats de recherche IA planifiée jamais vérifiés depuis
    plus de STALE_SCHEDULED_RESULTS_DAYS jours. N'affecte jamais un prospect
    déjà créé — uniquement les suggestions jamais relues par l'utilisateur."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM scheduled_search_results
                WHERE statut = 'a_verifier' AND created_at < now() - make_interval(days => %s)
                RETURNING id
                """,
                (STALE_SCHEDULED_RESULTS_DAYS,),
            )
            deleted = cur.fetchall()
        conn.commit()
        return len(deleted)
    finally:
        conn.close()


def purge_abandoned_import_jobs():
    """Supprime les imports CSV commencés (fichier envoyé, mapping proposé)
    mais jamais confirmés ni annulés depuis plus de ABANDONED_IMPORT_JOBS_HOURS
    heures — plus personne ne va les reprendre. Les imports terminés ('done'/
    'failed') ne sont pas concernés : leur contenu brut est déjà vidé automatiquement
    dès la fin du traitement (voir app/csv_import.py), ils ne pèsent presque rien."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM import_jobs
                WHERE status IN ('mapping', 'pending', 'processing')
                  AND created_at < now() - make_interval(hours => %s)
                RETURNING id
                """,
                (ABANDONED_IMPORT_JOBS_HOURS,),
            )
            deleted = cur.fetchall()
        conn.commit()
        return len(deleted)
    finally:
        conn.close()
