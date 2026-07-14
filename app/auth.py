"""
Authentification et rôles (session 3 des travaux Option 3+).

Rôles :
- admin        : accès complet, y compris gestion des membres et des paramètres
                 (SMTP, Google Business Profile).
- commercial   : travail quotidien — prospects, recherche IA, campagnes, envoi.
- lecture_seule: consultation uniquement, aucune action de création/modification/envoi.

Le premier utilisateur d'un espace de travail (créé en même temps que celui-ci)
est automatiquement administrateur — c'est l'inscription d'un nouvel artisan.
Les collègues sont ensuite créés directement par l'administrateur (pas de lien
d'invitation à gérer).

Sessions Flask standard (cookie signé avec SECRET_KEY, déjà configurée).
Mots de passe hachés avec werkzeug (inclus avec Flask, aucune nouvelle
dépendance nécessaire).
"""
from functools import wraps

from flask import session, jsonify, request
from werkzeug.security import generate_password_hash, check_password_hash

from app.db import get_db
from app import prospect_types
from app import subscriptions

ROLES = ("admin", "commercial", "lecture_seule")
WRITE_ROLES = ("admin", "commercial")  # rôles autorisés à créer/modifier/envoyer


class AuthError(Exception):
    pass


def hash_password(password):
    return generate_password_hash(password)


def create_workspace_with_admin(workspace_name, admin_email, admin_password):
    """Inscription d'un nouvel artisan : crée l'espace de travail et son premier
    utilisateur, administrateur de celui-ci."""
    if len(admin_password) < 8:
        raise AuthError("Le mot de passe doit contenir au moins 8 caractères.")

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO workspaces (name, plan, trial_ends_at) VALUES (%s, 'trial', %s) RETURNING id",
                (workspace_name, subscriptions.trial_end_date()),
            )
            workspace_id = cur.fetchone()[0]
            cur.execute(
                """
                INSERT INTO users (workspace_id, email, password_hash, role)
                VALUES (%s, %s, %s, 'admin')
                RETURNING id
                """,
                (workspace_id, admin_email.lower().strip(), hash_password(admin_password)),
            )
            user_id = cur.fetchone()[0]
        prospect_types.seed_default_types(workspace_id, conn=conn)
        conn.commit()
        return workspace_id, user_id
    except Exception as exc:
        conn.rollback()
        if "users_email_key" in str(exc):
            raise AuthError("Cette adresse e-mail est déjà utilisée.") from exc
        raise
    finally:
        conn.close()


def create_user(workspace_id, email, password, role):
    if role not in ROLES:
        raise AuthError(f"Rôle invalide : {role}")
    if len(password) < 8:
        raise AuthError("Le mot de passe doit contenir au moins 8 caractères.")

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (workspace_id, email, password_hash, role)
                VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                (workspace_id, email.lower().strip(), hash_password(password), role),
            )
            user_id = cur.fetchone()[0]
        conn.commit()
        return user_id
    except Exception as exc:
        conn.rollback()
        if "users_email_key" in str(exc):
            raise AuthError("Cette adresse e-mail est déjà utilisée.") from exc
        raise
    finally:
        conn.close()


def list_users(workspace_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, email, role, is_active, created_at FROM users WHERE workspace_id = %s ORDER BY created_at",
                (workspace_id,),
            )
            rows = cur.fetchall()
        cols = ["id", "email", "role", "is_active", "created_at"]
        return [dict(zip(cols, r)) for r in rows]
    finally:
        conn.close()


def set_user_active(workspace_id, user_id, is_active):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET is_active = %s WHERE id = %s AND workspace_id = %s RETURNING id",
                (is_active, user_id, workspace_id),
            )
            updated = cur.fetchone()
        conn.commit()
        if not updated:
            raise AuthError("Utilisateur introuvable dans cet espace de travail.")
    finally:
        conn.close()


def login(email, password):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, workspace_id, password_hash, role, is_active, must_change_password
                FROM users WHERE email = %s
                """,
                (email.lower().strip(),),
            )
            row = cur.fetchone()
    finally:
        conn.close()

    if not row or not check_password_hash(row[2], password):
        raise AuthError("Adresse e-mail ou mot de passe incorrect.")
    if not row[4]:
        raise AuthError("Ce compte a été désactivé.")

    session.pop("superadmin_id", None)
    session["user_id"] = row[0]
    session["workspace_id"] = row[1]
    session["role"] = row[3]
    session["must_change_password"] = row[5]
    session.permanent = True


def change_own_password(user_id, current_password, new_password):
    """Changement de mot de passe par l'utilisateur lui-même (depuis Paramètres,
    ou après une réinitialisation par le superadmin)."""
    if len(new_password) < 8:
        raise AuthError("Le nouveau mot de passe doit contenir au moins 8 caractères.")

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT password_hash FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
            if not row or not check_password_hash(row[0], current_password):
                raise AuthError("Mot de passe actuel incorrect.")
            cur.execute(
                "UPDATE users SET password_hash = %s, must_change_password = FALSE WHERE id = %s",
                (hash_password(new_password), user_id),
            )
        conn.commit()
    finally:
        conn.close()
    session["must_change_password"] = False


def logout():
    session.clear()


def current_user():
    if "user_id" not in session:
        return None
    return {
        "user_id": session["user_id"],
        "workspace_id": session["workspace_id"],
        "role": session["role"],
        "must_change_password": session.get("must_change_password", False),
    }


# --- Décorateurs de protection des routes ----------------------------------

def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return jsonify(error="Authentification requise."), 401
        return f(*args, **kwargs)
    return wrapper


def _requested_workspace_id(kwargs):
    if "workspace_id" in kwargs:
        return kwargs["workspace_id"]
    val = request.args.get("workspace_id", type=int)
    if val:
        return val
    val = request.form.get("workspace_id", type=int)
    if val:
        return val
    body = request.get_json(silent=True) or {}
    return body.get("workspace_id")


def require_own_workspace(f):
    """Vérifie que le workspace_id demandé (dans l'URL, la query string ou le
    corps JSON) correspond bien à celui de l'utilisateur connecté."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        requested = _requested_workspace_id(kwargs)
        if requested is not None and int(requested) != session.get("workspace_id"):
            return jsonify(error="Accès refusé à cet espace de travail."), 403
        return f(*args, **kwargs)
    return wrapper


def require_role(*allowed_roles):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if session.get("role") not in allowed_roles:
                return jsonify(error="Permission insuffisante pour cette action."), 403
            return f(*args, **kwargs)
        return wrapper
    return decorator
