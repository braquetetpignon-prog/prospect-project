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
import json

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


def create_workspace_with_admin(workspace_name, admin_email, admin_password, consent_ip=None):
    """Inscription d'un nouvel artisan : crée l'espace de travail et son premier
    utilisateur, administrateur de celui-ci. L'acceptation des CGV et du
    traitement RGPD est vérifiée en amont (voir app/main.py::auth_signup) —
    cette fonction se contente d'horodater les deux consentements, toujours
    ensemble puisqu'ils sont cochés dans le même envoi de formulaire."""
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
                INSERT INTO users (workspace_id, email, password_hash, role,
                                    cgv_accepted_at, rgpd_accepted_at, consent_ip)
                VALUES (%s, %s, %s, 'admin', now(), now(), %s)
                RETURNING id
                """,
                (workspace_id, admin_email.lower().strip(), hash_password(admin_password), consent_ip),
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


def update_user(workspace_id, user_id, fields):
    """Modifie l'e-mail et/ou le rôle d'un membre (permet notamment de
    'changer d'administrateur' — promouvoir un collègue en admin, ou
    rétrograder l'actuel). Protège toujours contre un espace de travail qui
    se retrouverait sans aucun administrateur actif."""
    updates = {}
    if "email" in fields and fields["email"]:
        updates["email"] = fields["email"].strip().lower()
    if "role" in fields and fields["role"]:
        if fields["role"] not in ROLES:
            raise AuthError(f"Rôle invalide. Rôles possibles : {', '.join(ROLES)}")
        updates["role"] = fields["role"]

    if not updates:
        raise AuthError("Aucune modification valide fournie.")

    conn = get_db()
    try:
        with conn.cursor() as cur:
            if "role" in updates and updates["role"] != "admin":
                cur.execute(
                    "SELECT count(*) FROM users WHERE workspace_id = %s AND role = 'admin' AND is_active AND id != %s",
                    (workspace_id, user_id),
                )
                if cur.fetchone()[0] == 0:
                    raise AuthError("Impossible : ce membre est le dernier administrateur actif de l'espace de travail.")

            if "email" in updates:
                cur.execute(
                    "SELECT id FROM users WHERE workspace_id = %s AND email = %s AND id != %s",
                    (workspace_id, updates["email"], user_id),
                )
                if cur.fetchone():
                    raise AuthError("Cet e-mail est déjà utilisé par un autre membre de l'équipe.")

            set_clause = ", ".join(f"{k} = %s" for k in updates)
            cur.execute(
                f"UPDATE users SET {set_clause} WHERE id = %s AND workspace_id = %s RETURNING id",
                list(updates.values()) + [user_id, workspace_id],
            )
            updated = cur.fetchone()
        conn.commit()
        if not updated:
            raise AuthError("Membre introuvable dans cet espace de travail.")
    finally:
        conn.close()


def delete_user(workspace_id, user_id):
    """Suppression définitive d'un membre. Protections :
    - impossible de supprimer le dernier administrateur actif ;
    - impossible si le membre a des rendez-vous à venir (la suppression les
      effacerait en cascade — l'admin doit d'abord les réassigner ou les
      annuler depuis le calendrier, pour ne jamais perdre un rendez-vous
      sans s'en rendre compte)."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT role FROM users WHERE id = %s AND workspace_id = %s", (user_id, workspace_id))
            row = cur.fetchone()
            if not row:
                raise AuthError("Membre introuvable dans cet espace de travail.")
            role = row[0]

            if role == "admin":
                cur.execute(
                    "SELECT count(*) FROM users WHERE workspace_id = %s AND role = 'admin' AND is_active AND id != %s",
                    (workspace_id, user_id),
                )
                if cur.fetchone()[0] == 0:
                    raise AuthError("Impossible : ce membre est le dernier administrateur actif de l'espace de travail.")

            cur.execute("SELECT count(*) FROM rendez_vous WHERE user_id = %s AND date_heure > now()", (user_id,))
            upcoming = cur.fetchone()[0]
            if upcoming:
                raise AuthError(
                    f"Ce membre a {upcoming} rendez-vous à venir dans le calendrier — "
                    f"réassigne-les ou annule-les avant de le supprimer."
                )

            cur.execute("DELETE FROM users WHERE id = %s AND workspace_id = %s", (user_id, workspace_id))
        conn.commit()
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
    session.pop("impersonation_superadmin_id", None)
    session["user_id"] = row[0]
    session["workspace_id"] = row[1]
    session["role"] = row[3]
    session["must_change_password"] = row[5]
    session.permanent = True

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE workspaces SET last_active_at = now(), deletion_requested_at = NULL WHERE id = %s",
                (row[1],),
            )
        conn.commit()
    finally:
        conn.close()


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


PIN_MIN_LENGTH = 6


def _validate_pin(pin):
    if not pin or not pin.isdigit() or len(pin) < PIN_MIN_LENGTH:
        raise AuthError(f"Le code PIN doit contenir au moins {PIN_MIN_LENGTH} chiffres (uniquement des chiffres).")
    if len(set(pin)) == 1:
        raise AuthError("Le code PIN ne doit pas être une répétition du même chiffre (ex : 111111).")
    digits = [int(c) for c in pin]
    ascending = all(b - a == 1 for a, b in zip(digits, digits[1:]))
    descending = all(a - b == 1 for a, b in zip(digits, digits[1:]))
    if ascending or descending:
        raise AuthError("Le code PIN ne doit pas être une suite de chiffres consécutifs (ex : 123456, 654321).")


def set_pin(user_id, current_password, pin):
    """Définit ou change le code PIN de récupération — utilisé ensuite pour
    réinitialiser le mot de passe sans e-mail. Exige le mot de passe actuel,
    comme pour tout changement d'identifiant de sécurité (on ne modifie pas
    un moyen de récupération sans prouver qu'on a déjà accès au compte)."""
    _validate_pin(pin)
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT password_hash FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
            if not row or not check_password_hash(row[0], current_password):
                raise AuthError("Mot de passe actuel incorrect.")
            cur.execute(
                "UPDATE users SET pin_hash = %s, pin_set_at = now() WHERE id = %s",
                (generate_password_hash(pin), user_id),
            )
        conn.commit()
    finally:
        conn.close()


def has_pin(user_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pin_hash IS NOT NULL FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
        return bool(row and row[0])
    finally:
        conn.close()


def reset_password_with_pin(email, pin, new_password):
    """Réinitialise le mot de passe via le code PIN — auto-service, sans
    passer par un e-mail. Message d'erreur volontairement générique dans
    tous les cas d'échec (e-mail inconnu, PIN jamais défini, ou PIN
    incorrect) pour ne jamais révéler si un compte existe."""
    if len(new_password) < 8:
        raise AuthError("Le nouveau mot de passe doit contenir au moins 8 caractères.")

    generic_error = "E-mail ou code PIN incorrect."
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, pin_hash, workspace_id FROM users WHERE email = %s AND is_active",
                (email.strip().lower(),),
            )
            row = cur.fetchone()
            if not row or not row[1] or not check_password_hash(row[1], pin):
                raise AuthError(generic_error)
            user_id, _, workspace_id = row
            cur.execute(
                "UPDATE users SET password_hash = %s, must_change_password = FALSE WHERE id = %s",
                (hash_password(new_password), user_id),
            )
        conn.commit()
        return workspace_id
    finally:
        conn.close()


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


DASHBOARD_WIDGETS = ("rdv", "prospects", "activity")


def get_dashboard_layout(user_id):
    """Renvoie l'ordre des widgets du tableau de bord pour cet utilisateur —
    préférence strictement personnelle, jamais partagée avec le reste de
    l'équipe. Ordre par défaut si jamais réglé, et toujours filtré/complété
    pour ne renvoyer que des identifiants de widgets valides (au cas où la
    liste des widgets disponibles évoluerait après coup)."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT dashboard_layout FROM user_preferences WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
    finally:
        conn.close()

    if not row or not row[0]:
        return list(DASHBOARD_WIDGETS)

    saved = [w for w in row[0] if w in DASHBOARD_WIDGETS]
    missing = [w for w in DASHBOARD_WIDGETS if w not in saved]
    return saved + missing  # widgets jamais vus (nouveauté future) ajoutés à la fin


def set_dashboard_layout(user_id, layout):
    if not isinstance(layout, list) or not all(w in DASHBOARD_WIDGETS for w in layout):
        raise AuthError(f"Disposition invalide — widgets autorisés : {', '.join(DASHBOARD_WIDGETS)}")
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO user_preferences (user_id, dashboard_layout, updated_at)
                VALUES (%s, %s, now())
                ON CONFLICT (user_id) DO UPDATE SET dashboard_layout = EXCLUDED.dashboard_layout, updated_at = now()
                """,
                (user_id, json.dumps(layout)),
            )
        conn.commit()
    finally:
        conn.close()
