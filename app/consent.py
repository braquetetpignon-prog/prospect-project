"""
Consentement RGPD (Option 3, onglet RGPD).

Règles appliquées (actées dans la spec) :
- "avis" : l'intérêt légitime peut s'appliquer par défaut (client déjà servi),
  donc autorisé tant qu'aucun opt-out explicite n'a été enregistré.
- "publicitaire" / "newsletter" : opt-in explicite requis, refusé par défaut
  tant qu'aucun consentement n'a été donné.
- Dans tous les cas, un opt-out enregistré bloque tout envoi de ce type,
  quel que soit le fondement juridique invoqué.

Désinscription : lien signé (HMAC avec SECRET_KEY), sans authentification —
un destinataire externe doit pouvoir s'en servir depuis un e-mail sans
compte ClickProspect.
"""
import hashlib
import hmac
import os

from app.db import get_db

SECRET_KEY = os.environ.get("SECRET_KEY", "")
APP_BASE_URL = os.environ.get("APP_BASE_URL", "https://preproduction.clickprospect.fr")

# Durée de conservation des entrées de consentement OBSOLÈTES (remplacées par un statut
# plus récent). Valeur provisoire à 40 jours, en attendant confirmation par un juriste —
# les lignes directrices du CEPD (5/2020) ne fixent pas de durée précise : elles indiquent
# seulement que la preuve doit être gardée tant que le traitement se poursuit, et au-delà,
# pas plus longtemps que nécessaire pour une obligation légale ou l'exercice d'un droit en
# justice (prescription civile française de droit commun : 5 ans). Cette purge ne touche
# JAMAIS le dernier statut enregistré pour un prospect/type donné — c'est l'état actif dont
# l'application a besoin pour fonctionner, indépendamment de son ancienneté.
CONSENT_HISTORY_RETENTION_DAYS = int(os.environ.get("CONSENT_HISTORY_RETENTION_DAYS", "40"))

CONSENT_TYPES = ("avis", "publicitaire", "newsletter", "relance")
OPT_IN_REQUIRED_TYPES = ("publicitaire", "newsletter")


def record_consent(prospect_id, type_, statut, source=None):
    if type_ not in CONSENT_TYPES:
        raise ValueError(f"Type de consentement invalide : {type_}")
    if statut not in ("opt_in", "opt_out", "interet_legitime"):
        raise ValueError(f"Statut de consentement invalide : {statut}")

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO consents (prospect_id, type, statut, source) VALUES (%s, %s, %s, %s)",
                (prospect_id, type_, statut, source),
            )
        conn.commit()
    finally:
        conn.close()


def get_consent_status(prospect_id, type_):
    """Renvoie le dernier statut enregistré pour ce type, ou None si aucun."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT statut, source, created_at FROM consents
                WHERE prospect_id = %s AND type = %s
                ORDER BY created_at DESC LIMIT 1
                """,
                (prospect_id, type_),
            )
            row = cur.fetchone()
        if not row:
            return None
        return {"statut": row[0], "source": row[1], "created_at": row[2]}
    finally:
        conn.close()


def get_all_consent_status(prospect_id):
    return {t: get_consent_status(prospect_id, t) for t in CONSENT_TYPES}


def prospect_workspace_id(prospect_id):
    """Retourne le workspace_id du prospect, ou None s'il n'existe pas.
    Utilisé pour vérifier qu'un utilisateur n'accède qu'aux prospects de son
    propre espace de travail."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT workspace_id FROM prospects WHERE id = %s", (prospect_id,))
            row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def can_send(prospect_id, type_):
    """Vérifie si un envoi de ce type est autorisé pour ce prospect."""
    if type_ not in CONSENT_TYPES:
        return False, f"Type de campagne invalide : {type_}"

    current = get_consent_status(prospect_id, type_)

    if current and current["statut"] == "opt_out":
        return False, "Le prospect s'est désinscrit de ce type de communication."

    if type_ in OPT_IN_REQUIRED_TYPES:
        if not current or current["statut"] != "opt_in":
            return False, "Opt-in requis et absent pour ce type de campagne."
        return True, "Opt-in explicite."

    # type "avis" : intérêt légitime par défaut, tant qu'il n'y a pas d'opt-out
    return True, current["statut"] if current else "Intérêt légitime (par défaut)."


# --- Désinscription (lien signé, sans authentification) -------------------

def _signature(prospect_id, type_):
    message = f"{prospect_id}:{type_}".encode()
    return hmac.new(SECRET_KEY.encode(), message, hashlib.sha256).hexdigest()[:32]


def build_unsubscribe_url(prospect_id, type_, base_url=None):
    sig = _signature(prospect_id, type_)
    base = (base_url or APP_BASE_URL).rstrip("/")
    return f"{base}/unsubscribe?prospect_id={prospect_id}&type={type_}&sig={sig}"


def verify_and_unsubscribe(prospect_id, type_, sig):
    if type_ not in CONSENT_TYPES:
        return False, "Type invalide."
    expected = _signature(prospect_id, type_)
    if not hmac.compare_digest(expected, sig):
        return False, "Lien de désinscription invalide."

    record_consent(prospect_id, type_, "opt_out", source="lien_desinscription")
    return True, "Désinscription enregistrée."


# --- Purge de l'historique de consentement obsolète ------------------------

def purge_old_consent_history(retention_days=None):
    """Supprime les entrées de consentement remplacées par un statut plus récent et
    plus vieilles que retention_days. Le dernier enregistrement de chaque couple
    (prospect_id, type) — le statut actif — n'est jamais supprimé par cette fonction,
    quelle que soit son ancienneté."""
    retention_days = retention_days or CONSENT_HISTORY_RETENTION_DAYS
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM consents c
                WHERE c.created_at < now() - (%s || ' days')::interval
                  AND c.id NOT IN (
                      SELECT DISTINCT ON (prospect_id, type) id
                      FROM consents
                      ORDER BY prospect_id, type, created_at DESC
                  )
                """,
                (retention_days,),
            )
            deleted = cur.rowcount
        conn.commit()
        return deleted
    finally:
        conn.close()
