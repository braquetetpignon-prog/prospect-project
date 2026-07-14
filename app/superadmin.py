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
    for wid, name, created_at, plan, trial_ends_at, paid_until, admin_email, member_count in rows:
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
