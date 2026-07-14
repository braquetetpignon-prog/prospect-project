"""
Assistant d'aide en ligne, intégré à l'app pour tous les utilisateurs connectés
(pas seulement les admins). Répond aux questions d'usage de ClickProspect à
partir d'un prompt système qui décrit les fonctionnalités — pas de recherche
web, pas de lien avec les données du client (aucune fiche prospect n'est
transmise au modèle). Permet aussi d'envoyer une suggestion/idée d'amélioration,
stockée séparément et visible côté superadmin (/supadmin).

Quota : DAILY_QUOTA messages par jour et par espace de travail (table
assistant_chat_log, même principe que ia_search_log pour la Recherche IA).
"""
import os

import requests

from app.db import get_db

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
DEFAULT_MODEL = "gemini-3.5-flash"
DAILY_QUOTA = 20
REQUEST_TIMEOUT = 30
GENERATE_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Limite la conversation envoyée à chaque appel (évite un prompt qui grossit
# indéfiniment si quelqu'un discute longtemps) — les tours les plus anciens
# sont simplement oubliés du contexte, la conversation reste utilisable.
MAX_HISTORY_TURNS = 10

SYSTEM_PROMPT = """Tu es l'assistant d'aide intégré à ClickProspect, un CRM léger pour \
artisans et petites équipes commerciales. Tu aides les utilisateurs à comprendre et \
utiliser l'application — jamais autre chose.

Fonctionnalités de ClickProspect que tu peux expliquer :
- Prospects : liste, recherche, filtres par statut et type, fiche complète (SIRET \
vérifiable, contact, statut, prochaine action, notes), prise de rendez-vous depuis la \
fiche, sélection multiple et suppression groupée, export CSV (réservé aux espaces en \
essai ou payants).
- Campagnes : création avec modèles préremplis (avis, publicitaire, newsletter), envoi \
par sélection manuelle ou par type de statut, historique des envois, limite de 10 \
campagnes actives (réservé aux espaces en essai ou payants).
- Prospection (onglet "Prospection") avec 4 sous-onglets :
  · Fichier : import CSV avec mapping automatique des colonnes.
  · Recherche IA : suggestions d'entreprises par IA avec recherche web réelle, limitée à \
quelques lancements par jour, jamais enregistré automatiquement, possibilité de \
planifier une relance quotidienne.
  · Recherche automatique : recherche via le registre officiel des entreprises \
(SIRET/adresse fiables, jamais de téléphone/email inventé).
  · Coller une réponse IA : on peut coller le texte d'une réponse obtenue sur Gemini/\
ChatGPT ailleurs, l'app en extrait automatiquement les fiches.
- Calendrier : rendez-vous partagés par toute l'équipe, vues jour/semaine/mois, export \
.ics.
- Paramètres (admin uniquement) : SMTP, membres de l'équipe, types de statut \
personnalisables, modèle IA utilisé pour la Recherche IA.
- Chaque espace de travail commence par un essai gratuit de 7 jours avec accès complet, \
puis bascule en version gratuite avec export CSV et envoi de campagnes désactivés si \
personne ne passe en payant.

Consignes :
- Réponds en français, de façon concise et concrète (quelques phrases, pas un roman).
- Si tu ne sais pas ou que la question sort du cadre de ClickProspect (question \
personnelle, actualité, autre sujet), dis-le simplement et recentre sur ce que tu peux \
faire.
- Tu n'as accès à aucune donnée du compte de la personne (ni ses prospects, ni ses \
campagnes) — si elle décrit un problème précis sur SES données, invite-la à contacter \
le support plutôt que de deviner.
- Tu peux suggérer d'utiliser le bouton "Envoyer une suggestion" du widget si la personne \
exprime une idée d'amélioration plutôt qu'une question."""


class AssistantError(Exception):
    pass


def get_quota_status(workspace_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) FROM assistant_chat_log
                WHERE workspace_id = %s AND created_at::date = CURRENT_DATE
                """,
                (workspace_id,),
            )
            used = cur.fetchone()[0]
        return {"used": used, "limit": DAILY_QUOTA, "remaining": max(0, DAILY_QUOTA - used)}
    finally:
        conn.close()


def _log_message(workspace_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO assistant_chat_log (workspace_id) VALUES (%s)", (workspace_id,))
        conn.commit()
    finally:
        conn.close()


def get_current_model():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM app_settings WHERE key = 'assistant_model'")
            row = cur.fetchone()
        return (row[0] if row and row[0] else None) or DEFAULT_MODEL
    finally:
        conn.close()


def send_message(workspace_id, history, message):
    """history : liste de {"role": "user"|"assistant", "text": "..."} — les tours
    précédents de LA conversation en cours (gardés côté client, pas en base).
    Retourne le texte de la réponse. Compte dans le quota quotidien qu'il y ait
    échec ou non côté Gemini (le quota protège le coût de l'appel lui-même)."""
    quota = get_quota_status(workspace_id)
    if quota["remaining"] <= 0:
        raise AssistantError(f"Quota quotidien atteint ({quota['used']}/{quota['limit']} messages aujourd'hui).")

    if not GEMINI_API_KEY:
        raise AssistantError("GEMINI_API_KEY n'est pas configurée sur le serveur.")

    message = (message or "").strip()
    if not message:
        raise AssistantError("Message vide.")

    contents = []
    for turn in (history or [])[-MAX_HISTORY_TURNS:]:
        role = "model" if turn.get("role") == "assistant" else "user"
        text = (turn.get("text") or "").strip()
        if text:
            contents.append({"role": role, "parts": [{"text": text}]})
    contents.append({"role": "user", "parts": [{"text": message}]})

    model = get_current_model()
    body = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": contents,
    }

    _log_message(workspace_id)  # compte dans le quota même en cas d'échec ci-dessous

    try:
        resp = requests.post(
            GENERATE_URL_TEMPLATE.format(model=model),
            headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY},
            json=body,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise AssistantError(f"Erreur réseau vers l'assistant : {exc}") from exc

    if resp.status_code == 429:
        raise AssistantError("Quota atteint au niveau du compte Google (palier gratuit global). Réessayez plus tard.")
    if resp.status_code == 404:
        raise AssistantError(
            f"Le modèle configuré ({model}) n'est plus disponible chez Google. "
            f"Un administrateur peut le changer depuis Paramètres."
        )
    if resp.status_code >= 400:
        raise AssistantError(f"Erreur assistant ({resp.status_code}) : {resp.text[:300]}")

    data = resp.json()
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        raise AssistantError("Réponse de l'assistant vide ou inattendue.")

    return text.strip()


def submit_feedback(workspace_id, workspace_name, user_email, message):
    message = (message or "").strip()
    if not message:
        raise AssistantError("Message vide.")
    if len(message) > 4000:
        raise AssistantError("Message trop long (4000 caractères maximum).")

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO admin_feedback (workspace_id, workspace_name, user_email, message)
                VALUES (%s, %s, %s, %s)
                """,
                (workspace_id, workspace_name, user_email, message),
            )
        conn.commit()
    finally:
        conn.close()
