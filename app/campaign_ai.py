"""
Amélioration du contenu d'une campagne via l'IA (Option 3, onglet Configuration).

Contrairement à la Recherche IA (app/ia_search.py), pas besoin de recherche
web ici : on demande simplement à Gemini de reformuler un brouillon existant.
Réutilise la même API Interactions et le même modèle configuré (Paramètres),
sans l'outil google_search ni de schéma JSON — juste du texte.

Comme partout ailleurs dans l'app : l'IA ne fait que proposer, l'utilisateur
relit et valide manuellement avant d'enregistrer (aucune écriture automatique
en base depuis ce module).
"""
import requests

from app import ia_search

REQUEST_TIMEOUT = 30

CAMPAIGN_TYPE_LABELS = {
    "avis": "une demande d'avis Google à un client déjà servi",
    "publicitaire": "un e-mail publicitaire annonçant une offre ou une actualité",
    "newsletter": "une newsletter d'actualités pour des prospects/clients",
}

PROMPT_TEMPLATE = """Tu aides un artisan ou une petite entreprise française à améliorer un e-mail \
professionnel qu'il compte envoyer à ses prospects/clients. Le message est {type_label}.

Voici son brouillon actuel :
---
{draft}
---

Réécris ce message pour le rendre plus clair, chaleureux et professionnel, en gardant un \
ton simple et sincère (pas de style trop commercial ou too much). Conserve impérativement \
tous les marqueurs entre accolades tels quels s'ils sont présents dans le brouillon (par \
exemple {{prenom}}, {{nom_entreprise}}, {{entreprise_prospect}}, {{lien_avis_google}}, {{lien_desinscription}}, {{image}}) \
— ce sont des variables techniques remplacées automatiquement à l'envoi, ne les traduis pas \
et ne les supprime pas. Réponds uniquement avec le texte du message amélioré, sans commentaire \
ni introduction, sans guillemets autour."""


class ImprovementError(Exception):
    pass


def improve_content(draft, campaign_type):
    if not draft or not draft.strip():
        raise ImprovementError("Rien à améliorer : le message est vide.")

    type_label = CAMPAIGN_TYPE_LABELS.get(campaign_type, "un e-mail professionnel")
    prompt = PROMPT_TEMPLATE.format(type_label=type_label, draft=draft.strip())

    if not ia_search.GEMINI_API_KEY:
        raise ImprovementError("GEMINI_API_KEY n'est pas configurée sur le serveur.")

    model = ia_search.get_current_model()
    body = {"model": model, "input": prompt}

    try:
        resp = requests.post(
            ia_search.INTERACTIONS_API_URL,
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": ia_search.GEMINI_API_KEY,
                "Api-Revision": ia_search.API_REVISION,
            },
            json=body,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise ImprovementError(f"Erreur réseau vers Gemini : {exc}") from exc

    if resp.status_code == 429:
        raise ImprovementError("Quota Gemini atteint (palier gratuit global). Réessayez plus tard.")
    if resp.status_code >= 400:
        raise ImprovementError(f"Erreur Gemini ({resp.status_code}) : {resp.text[:300]}")

    data = resp.json()
    text = ia_search._extract_model_output_text(data)
    if not text:
        raise ImprovementError("Réponse Gemini vide ou dans un format inattendu.")

    return text.strip()
