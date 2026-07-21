"""
Superadmin : gestion de l'ensemble des espaces de travail (clients), de leur
abonnement, et réinitialisation de mot de passe en cas de besoin. Totalement
séparé des comptes utilisateurs normaux (table dédiée `superadmins`, session
distincte) — accessible uniquement via /supadmin, jamais lié depuis l'app.

Deux rôles : 'administrateur' (accès complet) et 'technicien' (support —
consultation, réinitialisation de mot de passe d'un admin d'espace de
travail, mais jamais login-as, jamais changement d'abonnement/suppression).
Voir login_required (n'importe quel rôle) vs admin_required (administrateur
uniquement) — la distinction est toujours vérifiée côté serveur.

Bootstrap : le premier compte superadmin (toujours 'administrateur') est créé
automatiquement au démarrage à partir des variables d'environnement
SUPERADMIN_EMAIL / SUPERADMIN_PASSWORD, définies directement sur Coolify —
jamais saisies ni vues côté code applicatif au-delà de cette création initiale.
Si ces variables ne sont pas définies, ou si un compte existe déjà, rien ne
se passe (idempotent, sûr à appeler à chaque démarrage de chaque worker).
D'autres comptes (administrateur ou technicien) se créent ensuite depuis
/supadmin lui-même, réservé aux administrateurs (create_superadmin).
"""
import os
import secrets
import time
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
                "SELECT id, password_hash, email, role, is_active FROM superadmins WHERE email = %s",
                (email.lower().strip(),),
            )
            row = cur.fetchone()
    finally:
        conn.close()

    if not row or not check_password_hash(row[1], password):
        raise SuperadminError("Adresse e-mail ou mot de passe incorrect.")
    if not row[4]:
        raise SuperadminError("Ce compte a été désactivé.")

    session.clear()
    session["superadmin_id"] = row[0]
    session["superadmin_email"] = row[2]
    session["superadmin_role"] = row[3]
    session.permanent = True


def logout():
    session.clear()


# Mode maintenance : coupe l'accès public au site pendant un déploiement
# sensible, activé/désactivé manuellement depuis /supadmin (jamais
# automatique). Stocké dans app_settings comme les autres réglages globaux.
# Un petit cache en mémoire (quelques secondes) évite une requête DB à
# chaque page vue de chaque visiteur — voir app/main.py::_maintenance_gate.
_MAINTENANCE_KEY = "maintenance_mode"
_maintenance_cache = {"value": False, "checked_at": 0.0}
_MAINTENANCE_CACHE_TTL_SECONDS = 5


def is_maintenance_mode():
    now = time.time()
    if now - _maintenance_cache["checked_at"] < _MAINTENANCE_CACHE_TTL_SECONDS:
        return _maintenance_cache["value"]

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM app_settings WHERE key = %s", (_MAINTENANCE_KEY,))
            row = cur.fetchone()
    finally:
        conn.close()

    value = bool(row and row[0] == "on")
    _maintenance_cache["value"] = value
    _maintenance_cache["checked_at"] = now
    return value


def set_maintenance_mode(enabled):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app_settings (key, value, updated_at) VALUES (%s, %s, now())
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
                """,
                (_MAINTENANCE_KEY, "on" if enabled else "off"),
            )
        conn.commit()
    finally:
        conn.close()

    # Invalide le cache pour que le nouvel état soit pris en compte
    # immédiatement, sans attendre l'expiration du TTL.
    _maintenance_cache["checked_at"] = 0.0


def current_superadmin_id():
    return session.get("superadmin_id")


def current_superadmin_role():
    return session.get("superadmin_role")


def login_required(f):
    """N'importe quel compte superadmin actif — administrateur ou technicien.
    Utilisé pour les actions de consultation et de support (voir Article
    des rôles en tête de fichier)."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "superadmin_id" not in session:
            return jsonify(error="Authentification superadmin requise."), 401
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    """Réservé au rôle 'administrateur' — actions destructives, financières,
    ou touchant à la confidentialité d'un client (login-as). Toujours
    vérifié côté serveur, jamais seulement masqué côté interface."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "superadmin_id" not in session:
            return jsonify(error="Authentification superadmin requise."), 401
        if session.get("superadmin_role") != "administrateur":
            return jsonify(error="Action réservée au rôle administrateur."), 403
        return f(*args, **kwargs)
    return wrapper


def _log_action(action, workspace_id=None, workspace_name=None, details=None,
                 superadmin_id=None, superadmin_email=None):
    """Trace toute action sensible effectuée depuis /supadmin. Volontairement
    tolérant : un souci d'écriture du journal ne doit jamais faire échouer
    l'action elle-même (mieux vaut une action réussie sans trace qu'une
    action refusée à cause du journal).

    superadmin_id/superadmin_email peuvent être passés explicitement quand
    l'appelant a déjà modifié la session (ex: login_as, qui bascule sur une
    session utilisateur normale avant de journaliser) — sinon, lus depuis la
    session courante."""
    try:
        if superadmin_id is None:
            superadmin_id = current_superadmin_id()
        if superadmin_email is None:
            superadmin_email = session.get("superadmin_email", "?")
        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO superadmin_audit_log
                        (superadmin_id, superadmin_email, action, workspace_id, workspace_name, details)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (superadmin_id, superadmin_email, action, workspace_id, workspace_name, details),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def list_audit_log(limit=200):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT superadmin_email, action, workspace_id, workspace_name, details, created_at
                FROM superadmin_audit_log
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    return [
        {
            "superadmin_email": r[0],
            "action": r[1],
            "workspace_id": r[2],
            "workspace_name": r[3],
            "details": r[4],
            "created_at": r[5],
        }
        for r in rows
    ]


# --- Gestion des espaces de travail ----------------------------------------

def list_workspaces():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT w.id, w.name, w.created_at, w.plan, w.trial_ends_at, w.paid_until,
                       w.last_active_at, w.deletion_requested_at, w.ia_search_quota_override,
                       w.billing_interval, w.mollie_subscription_status,
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
         deletion_requested_at, ia_search_quota_override, billing_interval,
         mollie_subscription_status, admin_email, member_count) in rows:
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
            "billing_interval": billing_interval,
            "mollie_subscription_status": mollie_subscription_status,
            "last_active_at": last_active_at,
            "deletion_requested_at": deletion_requested_at,
            "ia_search_quota_override": ia_search_quota_override,
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
                    "UPDATE workspaces SET plan = 'trial', trial_ends_at = %s, paid_until = NULL WHERE id = %s RETURNING id, name",
                    (subscriptions.trial_end_date(), workspace_id),
                )
            elif plan == "paid":
                cur.execute(
                    "UPDATE workspaces SET plan = 'paid', paid_until = %s WHERE id = %s RETURNING id, name",
                    (paid_until, workspace_id),
                )
            else:  # free
                cur.execute(
                    "UPDATE workspaces SET plan = 'free', paid_until = NULL WHERE id = %s RETURNING id, name",
                    (workspace_id,),
                )
            updated = cur.fetchone()
        conn.commit()
        if not updated:
            raise SuperadminError("Espace de travail introuvable.")
    finally:
        conn.close()

    _log_action(
        "set_plan",
        workspace_id=workspace_id,
        workspace_name=updated[1],
        details=f"Nouveau statut : {plan}" + (f" jusqu'au {paid_until}" if plan == "paid" else ""),
    )


def set_ia_search_quota_override(workspace_id, quota_override):
    """quota_override : entier >= 1, ou None pour revenir au quota global par défaut."""
    if quota_override is not None and (not isinstance(quota_override, int) or quota_override < 1):
        raise SuperadminError("Le quota doit être un entier positif, ou vide pour revenir au défaut.")

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE workspaces SET ia_search_quota_override = %s WHERE id = %s RETURNING id, name",
                (quota_override, workspace_id),
            )
            updated = cur.fetchone()
        conn.commit()
        if not updated:
            raise SuperadminError("Espace de travail introuvable.")
    finally:
        conn.close()

    _log_action(
        "set_ia_search_quota_override",
        workspace_id=workspace_id,
        workspace_name=updated[1],
        details=f"Quota Recherche IA : {quota_override if quota_override is not None else 'défaut global'}",
    )


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
            cur.execute("SELECT name FROM workspaces WHERE id = %s", (workspace_id,))
            name_row = cur.fetchone()
        conn.commit()
        if not updated:
            raise SuperadminError("Aucun administrateur trouvé pour cet espace de travail.")
    finally:
        conn.close()

    emails = [r[0] for r in updated]
    _log_action(
        "reset_admin_password",
        workspace_id=workspace_id,
        workspace_name=name_row[0] if name_row else None,
        details=f"Mot de passe réinitialisé pour : {', '.join(emails)}",
    )
    return {"emails": emails, "temporary_password": temp_password}


def delete_workspace(workspace_id):
    """Suppression DÉFINITIVE et immédiate (sur demande explicite par mail, ou
    après validation manuelle d'une demande automatique pour inactivité). Toutes
    les données liées (prospects, campagnes, rendez-vous...) partent avec, via
    les contraintes ON DELETE CASCADE du schéma — il n'y a pas de retour arrière."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM workspaces WHERE id = %s RETURNING id, name", (workspace_id,))
            deleted = cur.fetchone()
        conn.commit()
        if not deleted:
            raise SuperadminError("Espace de travail introuvable.")
    finally:
        conn.close()

    _log_action("delete_workspace", workspace_id=workspace_id, workspace_name=deleted[1])


def dismiss_deletion_request(workspace_id):
    """Annule un signalement automatique (faux positif) sans rien supprimer."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE workspaces SET deletion_requested_at = NULL WHERE id = %s RETURNING id, name",
                (workspace_id,),
            )
            updated = cur.fetchone()
        conn.commit()
        if not updated:
            raise SuperadminError("Espace de travail introuvable.")
    finally:
        conn.close()

    _log_action("dismiss_deletion_request", workspace_id=workspace_id, workspace_name=updated[1])


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
        count = len(deleted)
        _log_action("purge_scheduled_search_results", details=f"{count} entrée(s) supprimée(s)")
        return count
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
        count = len(deleted)
        _log_action("purge_import_jobs", details=f"{count} entrée(s) supprimée(s)")
        return count
    finally:
        conn.close()


# --- Dépannage : se connecter en tant qu'administrateur d'un espace --------

def login_as(workspace_id):
    """Ouvre une session utilisateur normale sur le compte admin de cet espace,
    pour dépanner un client sans jamais connaître ni changer son mot de passe.
    Systématiquement journalisé — c'est l'action la plus sensible de la console.
    La session garde une marque discrète (impersonation_superadmin_id) pour que
    l'app affiche un bandeau "vue superadmin" pendant toute la durée de la visite.
    """
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT u.id, u.role, u.email, w.name
                FROM users u JOIN workspaces w ON w.id = u.workspace_id
                WHERE u.workspace_id = %s AND u.role = 'admin'
                ORDER BY u.created_at LIMIT 1
                """,
                (workspace_id,),
            )
            row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        raise SuperadminError("Aucun administrateur trouvé pour cet espace de travail.")

    user_id, role, email, workspace_name = row
    impersonator_id = current_superadmin_id()
    impersonator_email = session.get("superadmin_email", "?")

    session.clear()
    session["user_id"] = user_id
    session["workspace_id"] = workspace_id
    session["role"] = role
    session["must_change_password"] = False
    session["impersonation_superadmin_id"] = impersonator_id
    session.permanent = True

    _log_action(
        "login_as",
        workspace_id=workspace_id,
        workspace_name=workspace_name,
        details=f"Connecté en tant que {email}",
        superadmin_id=impersonator_id,
        superadmin_email=impersonator_email,
    )


# --- Suggestions remontées par les utilisateurs via l'assistant ------------

def get_workspace_detail(workspace_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT email, role, is_active, created_at FROM users WHERE workspace_id = %s ORDER BY created_at",
                (workspace_id,),
            )
            members = [
                {"email": r[0], "role": r[1], "is_active": r[2], "created_at": r[3]}
                for r in cur.fetchall()
            ]

            cur.execute("SELECT count(*) FROM prospects WHERE workspace_id = %s", (workspace_id,))
            prospect_count = cur.fetchone()[0]

            cur.execute(
                "SELECT count(*) FROM prospects WHERE workspace_id = %s AND statut = 'client'",
                (workspace_id,),
            )
            client_count = cur.fetchone()[0]

            cur.execute(
                "SELECT count(*) FROM campaigns WHERE workspace_id = %s AND statut = 'active'",
                (workspace_id,),
            )
            active_campaign_count = cur.fetchone()[0]

            cur.execute(
                """
                SELECT event_type, details, created_at FROM mollie_events
                WHERE workspace_id = %s ORDER BY created_at DESC LIMIT 10
                """,
                (workspace_id,),
            )
            billing_events = [
                {"event_type": r[0], "details": r[1], "created_at": r[2]}
                for r in cur.fetchall()
            ]
    finally:
        conn.close()

    return {
        "members": members,
        "prospect_count": prospect_count,
        "client_count": client_count,
        "active_campaign_count": active_campaign_count,
        "billing_events": billing_events,
    }


def list_superadmins():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, email, role, is_active, created_at FROM superadmins ORDER BY created_at"
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    return [
        {"id": r[0], "email": r[1], "role": r[2], "is_active": r[3], "created_at": r[4]}
        for r in rows
    ]


def create_superadmin(email, password, role):
    if role not in ("administrateur", "technicien"):
        raise SuperadminError("Rôle invalide.")
    if not password or len(password) < 8:
        raise SuperadminError("Le mot de passe doit contenir au moins 8 caractères.")

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM superadmins WHERE email = %s", (email.lower().strip(),))
            if cur.fetchone():
                raise SuperadminError("Un compte superadmin existe déjà avec cet e-mail.")
            cur.execute(
                "INSERT INTO superadmins (email, password_hash, role) VALUES (%s, %s, %s) RETURNING id",
                (email.lower().strip(), generate_password_hash(password), role),
            )
            new_id = cur.fetchone()[0]
        conn.commit()
    finally:
        conn.close()

    _log_action("create_superadmin", details=f"{email} ({role})")
    return new_id


def set_superadmin_active(superadmin_id, is_active):
    """Désactive ou réactive un compte superadmin — jamais de suppression
    définitive, pour garder l'historique du journal d'audit lisible
    (superadmin_id y référence toujours un compte existant tant qu'on ne le
    supprime pas). On ne peut jamais se désactiver soi-même, pour éviter de
    se retrouver bloqué dehors par erreur."""
    if superadmin_id == current_superadmin_id() and not is_active:
        raise SuperadminError("Impossible de désactiver votre propre compte.")

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE superadmins SET is_active = %s WHERE id = %s RETURNING email",
                (is_active, superadmin_id),
            )
            row = cur.fetchone()
            if not row:
                raise SuperadminError("Compte superadmin introuvable.")
        conn.commit()
    finally:
        conn.close()

    _log_action("deactivate_superadmin" if not is_active else "reactivate_superadmin", details=row[0])


def change_own_password(current_password, new_password, pin=None):
    superadmin_id = current_superadmin_id()
    if not new_password or len(new_password) < 8:
        raise SuperadminError("Le nouveau mot de passe doit contenir au moins 8 caractères.")

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT password_hash, pin_hash FROM superadmins WHERE id = %s", (superadmin_id,))
            row = cur.fetchone()
            if not row or not check_password_hash(row[0], current_password):
                raise SuperadminError("Mot de passe actuel incorrect.")
            # Si un PIN est configuré, il devient obligatoire pour tout
            # changement de mot de passe — c'est précisément ce qui protège
            # contre un tiers ayant récupéré la session (ex: ordinateur
            # laissé ouvert) mais ignorant le PIN.
            if row[1]:
                if not pin or not check_password_hash(row[1], pin):
                    raise SuperadminError("Code PIN incorrect.")
            cur.execute(
                "UPDATE superadmins SET password_hash = %s WHERE id = %s",
                (generate_password_hash(new_password), superadmin_id),
            )
        conn.commit()
    finally:
        conn.close()


PIN_MIN_LENGTH = 6


def _validate_pin(pin):
    """Mêmes règles que pour le PIN de récupération des utilisateurs
    classiques (voir auth.py::_validate_pin) — dupliqué volontairement ici
    pour garder ce module indépendant, l'usage étant différent (confirmation
    de changement de mot de passe, pas récupération)."""
    if not pin or not pin.isdigit() or len(pin) < PIN_MIN_LENGTH:
        raise SuperadminError(f"Le code PIN doit contenir au moins {PIN_MIN_LENGTH} chiffres (uniquement des chiffres).")
    if len(set(pin)) == 1:
        raise SuperadminError("Le code PIN ne doit pas être une répétition du même chiffre (ex : 111111).")
    digits = [int(c) for c in pin]
    ascending = all(digits[i] + 1 == digits[i + 1] for i in range(len(digits) - 1))
    descending = all(digits[i] - 1 == digits[i + 1] for i in range(len(digits) - 1))
    if ascending or descending:
        raise SuperadminError("Le code PIN ne doit pas être une suite de chiffres consécutifs (ex : 123456, 654321).")


def has_own_pin():
    superadmin_id = current_superadmin_id()
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pin_hash IS NOT NULL FROM superadmins WHERE id = %s", (superadmin_id,))
            row = cur.fetchone()
    finally:
        conn.close()
    return bool(row and row[0])


def set_own_pin(current_password, pin):
    """Définit ou change le code PIN — exige le mot de passe actuel, comme
    pour change_own_password : même niveau d'exigence pour configurer la
    protection que pour l'action qu'elle protège."""
    superadmin_id = current_superadmin_id()
    _validate_pin(pin)

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT password_hash FROM superadmins WHERE id = %s", (superadmin_id,))
            row = cur.fetchone()
            if not row or not check_password_hash(row[0], current_password):
                raise SuperadminError("Mot de passe actuel incorrect.")
            cur.execute(
                "UPDATE superadmins SET pin_hash = %s, pin_set_at = now() WHERE id = %s",
                (generate_password_hash(pin), superadmin_id),
            )
        conn.commit()
    finally:
        conn.close()


def list_feedback(limit=200):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, workspace_name, user_email, message, created_at, replied_at
                FROM admin_feedback
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    return [
        {"id": r[0], "workspace_name": r[1], "user_email": r[2], "message": r[3],
         "created_at": r[4], "replied_at": r[5]}
        for r in rows
    ]


def get_feedback(feedback_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, workspace_name, user_email, message FROM admin_feedback WHERE id = %s",
                (feedback_id,),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {"id": row[0], "workspace_name": row[1], "user_email": row[2], "message": row[3]}


def mark_feedback_replied(feedback_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE admin_feedback SET replied_at = now() WHERE id = %s RETURNING id",
                (feedback_id,),
            )
            updated = cur.fetchone()
        conn.commit()
    finally:
        conn.close()
    if not updated:
        raise SuperadminError("Suggestion introuvable.")
