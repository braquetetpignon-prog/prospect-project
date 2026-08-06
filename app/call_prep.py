"""
Aide à la préparation du premier appel prospect ("Préparer mon appel").

Génère un support d'appel (accroche, pitch express, questions ouvertes,
traitement d'objections) adapté à 2 axes croisés : le secteur d'activité du
prospect (naf_code -> libellé lisible via naf_codes, cf. naf_search.py) et le
type de contact (premier_contact / rappel / proposition_particuliere).

Choix volontaire : pas de nouveau champ sur la fiche prospect. Le seul champ
"métier" disponible aujourd'hui est naf_code (code officiel INSEE, déjà
présent sur prospects) — on le résout en libellé humain via la table
naf_codes existante plutôt que d'ajouter un champ dédié.

Cache par workspace : un texte déjà généré pour (secteur, type_contact) est
réutilisé par tous les utilisateurs du même espace de travail — seule une
génération réelle (cache miss) consomme le quota Gemini de l'utilisateur.
Quota et modèle Gemini réutilisent exactement le pattern de ia_search.py
(même API Interactions, même gestion d'erreurs) mais avec leur propre
comptage : DAILY_QUOTA ici est INDÉPENDANT de celui de ia_search.py, compté
PAR UTILISATEUR (et non par workspace comme ia_search) — cohérent avec
document_send.py (plafond d'envoi de fichier, lui aussi par utilisateur).

Sécurité :
- Les seules données envoyées au prompt sont des champs déjà structurés en
  base (nom_entreprise, libellé secteur) — jamais un champ libre non
  sanitizé du prospect (notes, etc.), pour limiter le risque d'injection de
  prompt via une fiche prospect malveillante.
- Double disclaimer non masquable côté UI (responsabilité utilisateur +
  non-usable tel quel pour un e-mail) — ce module ne fait que produire le
  texte, l'affichage du bandeau est de la responsabilité du template.
"""
import json
import os

import requests

from app.db import get_db

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
DEFAULT_GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
INTERACTIONS_API_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
API_REVISION = "2026-05-20"
REQUEST_TIMEOUT = 30  # pas de grounding web ici (contrairement à ia_search), donc plus rapide

DAILY_QUOTA = 3  # par utilisateur — compteur indépendant de ia_search.DAILY_QUOTA
DISCLAIMER_VERSION = 1  # incrémenter si le texte du bandeau/consentement change

TYPES_CONTACT = ("premier_contact", "rappel", "proposition_particuliere")
TYPE_CONTACT_LABELS = {
    "premier_contact": "premier contact",
    "rappel": "rappel",
    "proposition_particuliere": "proposition particulière",
}


class CallPrepError(Exception):
    """Erreur métier — message destiné à être renvoyé tel quel à l'utilisateur."""


class QuotaExceeded(CallPrepError):
    pass


class GeminiError(CallPrepError):
    pass


class ConsentRequired(CallPrepError):
    pass


RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "accroche": {"type": "string"},
        "pitch": {"type": "string"},
        "questions": {"type": "array", "items": {"type": "string"}},
        "objections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "objection": {"type": "string"},
                    "reponse": {"type": "string"},
                },
                "required": ["objection", "reponse"],
            },
        },
    },
    "required": ["accroche", "pitch", "questions", "objections"],
}

PROMPT_TEMPLATE = """Tu aides un commercial français à préparer un appel de prospection téléphonique.

Contexte :
- Entreprise appelée : {nom_entreprise}
- Secteur d'activité : {secteur_label}
- Type d'appel : {type_contact_label}

Produis un support d'appel court et concret, structuré en 4 parties :
1. Une accroche (1 à 2 phrases) pour les 10 premières secondes de l'appel.
2. Un pitch express (2 à 3 phrases) présentant la valeur ajoutée, adapté au secteur ci-dessus.
3. 2 à 3 questions ouvertes pour faire parler le prospect de ses besoins.
4. 2 objections courantes pour ce type d'appel, chacune avec une réponse courte et non agressive.

Ce texte est un support générique par secteur et type d'appel — il sera relu et adapté par le \\
commercial pour chaque prospect précis avant l'appel, jamais utilisé tel quel. Reste général \\
(pas de nom de personne, pas de détail inventé sur cette entreprise précise), professionnel, \\
et concis."""


def get_naf_label(naf_code):
    if not naf_code:
        return None
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT label FROM naf_codes WHERE code = %s", (naf_code,))
            row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _normalize_secteur_key(naf_code, secteur_label):
    """Clé de cache stable : le code NAF si connu, sinon un fallback textuel
    normalisé — jamais None (sinon deux prospects "sans secteur" partageraient
    par erreur toutes les clés NULL en SQL, qui ne s'égalent jamais entre elles)."""
    return (naf_code or "").strip() or "non_renseigne"


# --- Consentement (case à cocher, une fois par utilisateur) ---------------

def has_consented(user_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM call_prep_consent WHERE user_id = %s AND disclaimer_version = %s",
                (user_id, DISCLAIMER_VERSION),
            )
            return cur.fetchone() is not None
    finally:
        conn.close()


def record_consent(user_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO call_prep_consent (user_id, disclaimer_version)
                VALUES (%s, %s)
                ON CONFLICT (user_id, disclaimer_version) DO NOTHING
                """,
                (user_id, DISCLAIMER_VERSION),
            )
        conn.commit()
    finally:
        conn.close()


# --- Quota (par utilisateur, indépendant de ia_search) ---------------------

def get_quota_status(user_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) FROM call_prep_generation_log
                WHERE user_id = %s AND created_at::date = CURRENT_DATE
                """,
                (user_id,),
            )
            used = cur.fetchone()[0]
        return {"used": used, "limit": DAILY_QUOTA, "remaining": max(0, DAILY_QUOTA - used)}
    finally:
        conn.close()


def _log_generation(user_id, workspace_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO call_prep_generation_log (user_id, workspace_id) VALUES (%s, %s)",
                (user_id, workspace_id),
            )
        conn.commit()
    finally:
        conn.close()


# --- Cache partagé par workspace (secteur, type_contact) -------------------

def _get_cached(workspace_id, secteur_key, type_contact):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, texte_genere FROM call_prep_cache
                WHERE workspace_id = %s AND secteur_key = %s AND type_contact = %s
                """,
                (workspace_id, secteur_key, type_contact),
            )
            row = cur.fetchone()
        if not row:
            return None
        cache_id, texte = row
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE call_prep_cache SET usage_count = usage_count + 1 WHERE id = %s",
                (cache_id,),
            )
        conn.commit()
        return texte
    finally:
        conn.close()


def _store_cache(workspace_id, secteur_key, type_contact, texte_genere, user_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO call_prep_cache (workspace_id, secteur_key, type_contact, texte_genere, created_by)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (workspace_id, secteur_key, type_contact)
                DO UPDATE SET texte_genere = EXCLUDED.texte_genere, usage_count = call_prep_cache.usage_count + 1
                """,
                (workspace_id, secteur_key, type_contact, json.dumps(texte_genere), user_id),
            )
        conn.commit()
    finally:
        conn.close()


# --- Appel Gemini (même pattern que ia_search.call_gemini, sans grounding) -

def call_gemini(prompt):
    if not GEMINI_API_KEY:
        raise GeminiError("GEMINI_API_KEY n'est pas configurée sur le serveur.")

    body = {
        "model": DEFAULT_GEMINI_MODEL,
        "input": prompt,
        "response_format": {
            "type": "text",
            "mime_type": "application/json",
            "schema": RESPONSE_SCHEMA,
        },
    }
    try:
        resp = requests.post(
            INTERACTIONS_API_URL,
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": GEMINI_API_KEY,
                "Api-Revision": API_REVISION,
            },
            json=body,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise GeminiError(f"Erreur réseau vers Gemini : {exc}") from exc

    if resp.status_code == 429:
        raise GeminiError("Quota Gemini atteint au niveau du compte Google. Réessayez plus tard.")
    if resp.status_code >= 400:
        raise GeminiError(f"Erreur Gemini ({resp.status_code}) : {resp.text[:300]}")

    data = resp.json()
    text = _extract_model_output_text(data)
    if not text:
        raise GeminiError("Réponse Gemini vide ou dans un format inattendu.")

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise GeminiError(f"Réponse Gemini non exploitable (JSON invalide) : {exc}") from exc

    for key in ("accroche", "pitch", "questions", "objections"):
        if key not in parsed:
            raise GeminiError(f"Réponse Gemini incomplète (champ '{key}' manquant).")
    return parsed


def _extract_model_output_text(data):
    steps = data.get("steps") or []
    chunks = []
    for step in steps:
        if step.get("type") != "model_output":
            continue
        for block in step.get("content") or []:
            if block.get("type") == "text" and block.get("text"):
                chunks.append(block["text"])
    return "\n".join(chunks).strip()


# --- Orchestration -----------------------------------------------------

def prepare_call(workspace_id, user_id, prospect, type_contact):
    """`prospect` : dict déjà chargé par l'appelant (voir prospects.get_prospect),
    déjà vérifié appartenir au bon workspace — cette fonction ne recharge pas
    le prospect elle-même (même convention que document_send.send_document)."""
    if type_contact not in TYPES_CONTACT:
        raise CallPrepError(f"Type de contact invalide : {type_contact}")

    if not has_consented(user_id):
        raise ConsentRequired(
            "Vous devez d'abord valider l'avertissement d'usage avant de générer un support d'appel."
        )

    naf_code = prospect.get("naf_code")
    secteur_label = get_naf_label(naf_code) or "secteur non renseigné"
    secteur_key = _normalize_secteur_key(naf_code, secteur_label)

    cached = _get_cached(workspace_id, secteur_key, type_contact)
    if cached is not None:
        return {"texte": json.loads(cached) if isinstance(cached, str) else cached, "source": "cache"}

    quota = get_quota_status(user_id)
    if quota["remaining"] <= 0:
        raise QuotaExceeded(
            f"Quota quotidien atteint ({quota['used']}/{quota['limit']} générations aujourd'hui). "
            f"Un texte existe peut-être déjà pour un autre secteur/type de contact."
        )

    prompt = PROMPT_TEMPLATE.format(
        nom_entreprise=prospect.get("nom_entreprise") or "cette entreprise",
        secteur_label=secteur_label,
        type_contact_label=TYPE_CONTACT_LABELS[type_contact],
    )
    texte = call_gemini(prompt)

    _log_generation(user_id, workspace_id)
    _store_cache(workspace_id, secteur_key, type_contact, texte, user_id)

    return {"texte": texte, "source": "generation"}
