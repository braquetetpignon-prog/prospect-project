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
import secrets
from datetime import datetime, timedelta, timezone

from flask import session, jsonify, request
from werkzeug.security import generate_password_hash, check_password_hash

from app.db import get_db
from app import prospect_types
from app import subscriptions

ROLES = ("admin", "commercial", "lecture_seule")
WRITE_ROLES = ("admin", "commercial")  # rôles autorisés à créer/modifier/envoyer

EMAIL_VERIFICATION_TOKEN_VALIDITY_HOURS = 24


class AuthError(Exception):
    pass


def hash_password(password):
    return generate_password_hash(password)


def create_workspace_with_admin(workspace_name, admin_email, admin_password, consent_ip=None):
    """Inscription d'un nouvel artisan : crée l'espace de travail et son premier
    utilisateur, administrateur de celui-ci. L'acceptation des CGV et du
    traitement RGPD est vérifiée en amont (voir app/main.py::auth_signup) —
    cette fonction se contente d'horodater les deux consentements, toujours
    ensemble puisqu'ils sont cochés dans le même envoi de formulaire.

    Renvoie aussi un jeton de vérification d'e-mail EN CLAIR (uniquement à cet
    instant — seul son hachage est conservé en base, voir generate_password_hash
    ci-dessous) : c'est à l'appelant (app/main.py::auth_signup) de l'envoyer
    par e-mail, cette fonction ne s'occupe jamais elle-même de l'envoi."""
    if len(admin_password) < 8:
        raise AuthError("Le mot de passe doit contenir au moins 8 caractères.")

    verification_token = secrets.token_urlsafe(32)

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
                                    cgv_accepted_at, rgpd_accepted_at, consent_ip,
                                    email_verification_token_hash, email_verification_sent_at)
                VALUES (%s, %s, %s, 'admin', now(), now(), %s, %s, now())
                RETURNING id
                """,
                (
                    workspace_id, admin_email.lower().strip(), hash_password(admin_password), consent_ip,
                    generate_password_hash(verification_token),
                ),
            )
            user_id = cur.fetchone()[0]
        prospect_types.seed_default_types(workspace_id, conn=conn)
        conn.commit()
        return workspace_id, user_id, verification_token
    except Exception as exc:
        conn.rollback()
        if "users_email_key" in str(exc):
            raise AuthError("Cette adresse e-mail est déjà utilisée.") from exc
        raise
    finally:
        conn.close()


def generate_email_verification_token(email):
    """(Ré)génère un jeton de vérification pour un compte pas encore confirmé
    — utilisé au renvoi depuis /verifier-email (voir app/main.py). Renvoie le
    jeton EN CLAIR (à envoyer par e-mail immédiatement, jamais stocké tel
    quel) ou None si l'e-mail est inconnu, inactif, ou déjà vérifié (dans ce
    dernier cas, rien à renvoyer — message générique côté appelant pour ne
    jamais confirmer/infirmer l'existence d'un compte à un tiers)."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM users WHERE email = %s AND is_active AND email_verified_at IS NULL",
                (email.lower().strip(),),
            )
            row = cur.fetchone()
            if not row:
                return None
            token = secrets.token_urlsafe(32)
            cur.execute(
                "UPDATE users SET email_verification_token_hash = %s, email_verification_sent_at = now() WHERE id = %s",
                (generate_password_hash(token), row[0]),
            )
        conn.commit()
        return token
    finally:
        conn.close()


def verify_email_token(email, token):
    """Confirme l'adresse e-mail si le jeton correspond et n'a pas expiré.
    Renvoie l'id utilisateur en cas de succès. Idempotent : si le compte est
    déjà vérifié, renvoie aussi son id sans erreur (un clic répété sur le
    même lien, ou deux onglets ouverts, ne doit jamais afficher d'échec)."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, email_verified_at, email_verification_token_hash, email_verification_sent_at
                FROM users WHERE email = %s AND is_active
                """,
                (email.lower().strip(),),
            )
            row = cur.fetchone()
            if not row:
                raise AuthError("Lien de vérification invalide.")
            user_id, verified_at, token_hash, sent_at = row
            if verified_at is not None:
                return user_id
            if not token_hash or not sent_at:
                raise AuthError("Lien de vérification invalide.")
            expired = datetime.now(timezone.utc) > sent_at + timedelta(hours=EMAIL_VERIFICATION_TOKEN_VALIDITY_HOURS)
            if expired:
                raise AuthError("Ce lien a expiré — demande un nouvel e-mail de confirmation.")
            if not check_password_hash(token_hash, token):
                raise AuthError("Lien de vérification invalide.")
            cur.execute(
                "UPDATE users SET email_verified_at = now(), email_verification_token_hash = NULL WHERE id = %s",
                (user_id,),
            )
        conn.commit()
        if session.get("user_id") == user_id:
            session["email_verified"] = True
        return user_id
    finally:
        conn.close()


def create_user(workspace_id, email, password, role):
    """Ajout d'un collègue par un administrateur — contrairement à
    create_workspace_with_admin (inscription publique), le compte est
    vérifié d'office : c'est l'admin, déjà authentifié, qui certifie
    l'adresse en l'ajoutant lui-même. Sans ça, ce compte resterait bloqué
    indéfiniment par _email_verification_gate (voir app/main.py) sans
    jamais recevoir de jeton, puisque ce parcours n'en envoie aucun."""
    if role not in ROLES:
        raise AuthError(f"Rôle invalide : {role}")
    if len(password) < 8:
        raise AuthError("Le mot de passe doit contenir au moins 8 caractères.")

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (workspace_id, email, password_hash, role, email_verified_at)
                VALUES (%s, %s, %s, %s, now())
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
                SELECT id, workspace_id, password_hash, role, is_active, must_change_password,
                       email_verified_at
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
    session["email_verified"] = row[6] is not None
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


PROFILE_PERSONAL_FIELDS = ("first_name", "last_name", "phone")
PROFILE_COMPANY_FIELDS = ("name", "siret", "adresse", "code_postal", "ville")


def get_profile(user_id, workspace_id):
    """Informations personnelles (users) + entreprise (workspaces) pour la
    page Mon compte. Les champs entreprise sont renvoyés pour tout le monde
    (affichage), mais seul un admin peut les modifier — voir update_profile."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT first_name, last_name, phone FROM users WHERE id = %s", (user_id,)
            )
            personal = cur.fetchone()
            cur.execute(
                "SELECT name, siret, adresse, code_postal, ville FROM workspaces WHERE id = %s",
                (workspace_id,),
            )
            company = cur.fetchone()
    finally:
        conn.close()
    return {
        "first_name": personal[0], "last_name": personal[1], "phone": personal[2],
        "company_name": company[0], "siret": company[1], "adresse": company[2],
        "code_postal": company[3], "ville": company[4],
    }


def update_profile(user_id, workspace_id, is_admin, fields):
    """Met à jour les informations facultatives de Mon compte. Les champs
    personnels (nom, prénom, téléphone) sont modifiables par n'importe quel
    utilisateur pour lui-même ; les champs entreprise (nom, SIRET, adresse)
    ne le sont que par un admin de l'espace de travail — un commercial ne
    doit pas pouvoir changer le SIRET de l'entreprise depuis son propre
    compte. Tous les champs sont facultatifs (aucun n'est requis) :
    minimisation des données, personne n'est forcé de les renseigner."""
    personal_updates = {
        k: (fields.get(k) or "").strip() or None
        for k in PROFILE_PERSONAL_FIELDS if k in fields
    }
    company_updates = {}
    if is_admin:
        company_updates = {
            k: (fields.get(k) or "").strip() or None
            for k in PROFILE_COMPANY_FIELDS if k in fields
        }
        # "name" du côté workspaces, mais la clé publique de l'API est
        # "company_name" pour rester explicite côté formulaire.
        if "company_name" in fields:
            company_updates["name"] = (fields.get("company_name") or "").strip() or None

    conn = get_db()
    try:
        with conn.cursor() as cur:
            if personal_updates:
                set_clause = ", ".join(f"{k} = %s" for k in personal_updates)
                cur.execute(
                    f"UPDATE users SET {set_clause} WHERE id = %s",
                    (*personal_updates.values(), user_id),
                )
            if company_updates:
                # "name" ne doit jamais être vidé (NOT NULL en base) : on
                # ignore une valeur vide pour cette colonne précise plutôt
                # que de risquer une erreur SQL qui ferait perdre aussi les
                # autres champs de la même requête.
                if "name" in company_updates and not company_updates["name"]:
                    company_updates.pop("name")
                if company_updates:
                    set_clause = ", ".join(f"{k} = %s" for k in company_updates)
                    cur.execute(
                        f"UPDATE workspaces SET {set_clause} WHERE id = %s",
                        (*company_updates.values(), workspace_id),
                    )
        conn.commit()
    finally:
        conn.close()

    if is_admin:
        from app import client_sync
        client_sync.sync_workspace_admin_to_crm(workspace_id)


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
        "email_verified": session.get("email_verified", True),
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
