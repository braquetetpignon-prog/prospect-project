"""
Recherche IA intégrée (Option 2) — fournisseur Gemini, palier gratuit.

Flux : l'utilisateur personnalise seulement le lieu et le type d'entreprise
(+ un critère optionnel). Le prompt est pré-construit côté serveur, envoyé à
Gemini avec la recherche Google activée (grounding réel, pas juste une
estimation du modèle), la réponse structurée est retournée pour relecture.
Rien n'est enregistré automatiquement dans une fiche prospect — l'insertion
reste une action manuelle et volontaire de l'utilisateur, faite séparément.

Quota : 3 lancements par jour par espace de travail (table ia_search_log).
"""
import json
import os

import requests

from app.db import get_db
from app import official_search

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
# Le grounding (recherche Google) combiné à une sortie JSON structurée n'est
# disponible que sur les modèles de la série Gemini 3 (ex: gemini-3.5-flash).
# Modifiable sans redéploiement depuis Paramètres si Google fait évoluer l'offre.
DEFAULT_GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")

DAILY_QUOTA = 3
NOMBRE_RESULTATS = 8
# Le grounding peut déclencher plusieurs recherches web avant de répondre :
# plus lent qu'un simple appel texte, d'où un délai plus généreux.
REQUEST_TIMEOUT = 45
INTERACTIONS_API_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
# Fige le format de réponse ("steps") de l'API Interactions, encore en évolution.
API_REVISION = "2026-05-20"


def get_current_model():
    """Modèle Gemini actif : réglage en base s'il existe (modifiable depuis les
    Paramètres sans redéploiement), sinon la variable d'environnement GEMINI_MODEL,
    sinon la valeur par défaut codée en dur."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM app_settings WHERE key = 'gemini_model'")
            row = cur.fetchone()
        return (row[0] if row and row[0] else None) or DEFAULT_GEMINI_MODEL
    finally:
        conn.close()


def set_current_model(model_name):
    model_name = (model_name or "").strip()
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app_settings (key, value, updated_at) VALUES ('gemini_model', %s, now())
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
                """,
                (model_name or None,),
            )
        conn.commit()
    finally:
        conn.close()

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "prospects": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "nom_entreprise": {"type": "string"},
                    "adresse": {"type": "string"},
                    "ville": {"type": "string"},
                    "telephone": {"type": "string"},
                    "email": {"type": "string"},
                    "site_web": {"type": "string"},
                    "dirigeant": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["nom_entreprise"],
            },
        }
    },
    "required": ["prospects"],
}

PROMPT_TEMPLATE = """Tu aides un artisan français à repérer des prospects professionnels potentiels.

Critères de recherche :
- Type d'entreprise recherché : {type_entreprise}
- Lieu : {lieu}{criteres_line}

Utilise la recherche web pour trouver jusqu'à {nombre_resultats} entreprises réelles \
correspondant à ces critères (annuaires professionnels, pages jaunes, sites d'entreprise, \
registres légaux). Pour chaque entreprise, ne renseigne un champ (adresse, téléphone, email, \
site web, dirigeant) que si tu es raisonnablement certain de son exactitude d'après ce que tu \
as trouvé. Laisse-le vide plutôt que d'inventer une information. Cette liste sera \
systématiquement relue et vérifiée par un humain avant tout usage — elle ne doit donc contenir \
aucune donnée présentée comme certaine si elle ne l'est pas."""


class QuotaExceeded(Exception):
    pass


class GeminiError(Exception):
    pass


def get_quota_status(workspace_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) FROM ia_search_log
                WHERE workspace_id = %s AND created_at::date = CURRENT_DATE
                """,
                (workspace_id,),
            )
            used = cur.fetchone()[0]
            cur.execute(
                "SELECT ia_search_quota_override FROM workspaces WHERE id = %s",
                (workspace_id,),
            )
            row = cur.fetchone()
        # NULL = pas de réglage particulier pour ce client -> quota global par
        # défaut. Réglable par le superadmin pour un client précis (idée produit).
        limit_ = row[0] if row and row[0] is not None else DAILY_QUOTA
        return {"used": used, "limit": limit_, "remaining": max(0, limit_ - used)}
    finally:
        conn.close()


def _log_search(workspace_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO ia_search_log (workspace_id) VALUES (%s)",
                (workspace_id,),
            )
        conn.commit()
    finally:
        conn.close()


def build_prompt(lieu, type_entreprise, criteres_additionnels=None):
    criteres_line = f"\n- Autres critères : {criteres_additionnels}" if criteres_additionnels else ""
    return PROMPT_TEMPLATE.format(
        type_entreprise=type_entreprise,
        lieu=lieu,
        criteres_line=criteres_line,
        nombre_resultats=NOMBRE_RESULTATS,
    )


def call_gemini(prompt):
    if not GEMINI_API_KEY:
        raise GeminiError("GEMINI_API_KEY n'est pas configurée sur le serveur.")

    model = get_current_model()

    body = {
        "model": model,
        "input": prompt,
        # Grounding réel : le modèle exécute lui-même des recherches Google et
        # base sa réponse dessus, au lieu de deviner à partir de sa mémoire.
        "tools": [{"type": "google_search"}],
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
        raise GeminiError(
            "Quota Gemini atteint au niveau du compte Google (palier gratuit global). Réessayez plus tard."
        )
    if resp.status_code == 404:
        raise GeminiError(
            f"Le modèle IA configuré ({model}) n'est plus disponible chez Google — "
            f"c'est fréquent, ces modèles sont retirés régulièrement. Un administrateur peut le "
            f"changer directement depuis Paramètres, sans intervention technique "
            f"(voir la liste à jour sur ai.google.dev/gemini-api/docs/models)."
        )
    if resp.status_code == 400:
        raise GeminiError(
            f"Gemini a refusé la requête (modèle {model}) — la recherche web combinée à une "
            f"réponse structurée n'est disponible que sur certains modèles récents (ex: "
            f"gemini-3.5-flash). Vérifiez le modèle configuré dans Paramètres. Détail : "
            f"{resp.text[:300]}"
        )
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

    prospects = parsed.get("prospects") if isinstance(parsed, dict) else None
    if not isinstance(prospects, list):
        raise GeminiError("Format de réponse Gemini inattendu (champ 'prospects' manquant).")

    return prospects[:NOMBRE_RESULTATS]


def _extract_model_output_text(data):
    """Concatène le texte des blocs du dernier step 'model_output' (schéma
    'steps' de l'API Interactions — les autres steps sont les appels/résultats
    de recherche Google, qu'on ignore ici car seul le texte final nous intéresse).
    """
    steps = data.get("steps") or []
    chunks = []
    for step in steps:
        if step.get("type") != "model_output":
            continue
        for block in step.get("content") or []:
            if block.get("type") == "text" and block.get("text"):
                chunks.append(block["text"])
    return "\n".join(chunks).strip()


def _enrich_with_official_registry(prospects):
    """Pour chaque proposition de Gemini, tente de retrouver la fiche
    officielle correspondante (SIRET, SIREN, code NAF, adresse) via le
    registre Recherche d'Entreprises (data.gouv.fr), déjà utilisé ailleurs
    dans l'app (onglet Recherche automatique, vérification SIRET manuelle).

    Le grounding (recherche Google) peut désormais aussi remonter téléphone,
    email et dirigeant directement depuis le web (annuaires, sites d'entreprise)
    — ces champs-là ne viennent jamais du registre officiel, qui ne les contient
    pas, et restent donc « à vérifier » côté humain plutôt que garantis.

    Purement additif et jamais bloquant pour le SIRET/l'adresse : si aucune
    correspondance fiable n'est trouvée ou si le registre est indisponible,
    la proposition de Gemini reste inchangée — on ne comble jamais un champ
    par une donnée inventée ou incertaine.
    """
    enriched = []
    for prospect in prospects:
        match = official_search.enrich_by_name(prospect.get("nom_entreprise"), prospect.get("ville"))
        if match:
            prospect["siret"] = match.get("siret")
            prospect["siren"] = match.get("siren")
            prospect["naf_code"] = match.get("naf_code")
            # L'adresse/ville officielle remplace celle de Gemini quand elle est
            # connue : plus fiable qu'une estimation du modèle.
            if match.get("adresse"):
                prospect["adresse"] = match["adresse"]
            if match.get("code_postal"):
                prospect["code_postal"] = match["code_postal"]
            if match.get("ville"):
                prospect["ville"] = match["ville"]
            prospect["verifie_registre_officiel"] = True
        else:
            prospect["verifie_registre_officiel"] = False
        enriched.append(prospect)
    return enriched


def perform_search(workspace_id, lieu, type_entreprise, criteres_additionnels=None):
    quota = get_quota_status(workspace_id)
    if quota["remaining"] <= 0:
        raise QuotaExceeded(
            f"Quota quotidien atteint ({quota['used']}/{quota['limit']} lancements aujourd'hui)."
        )

    prompt = build_prompt(lieu, type_entreprise, criteres_additionnels)
    prospects = call_gemini(prompt)
    prospects = _enrich_with_official_registry(prospects)

    # Le lancement compte dans le quota même si l'utilisateur ne retient aucun résultat ensuite.
    _log_search(workspace_id)

    return {
        "prospects": prospects,
        "quota": get_quota_status(workspace_id),
    }


# --- Recherches planifiées (relance automatique quotidienne, fiable côté serveur) ---

def create_scheduled_search(workspace_id, lieu, type_entreprise, criteres_additionnels, heure):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO scheduled_searches (workspace_id, lieu, type_entreprise, criteres_additionnels, heure)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (workspace_id, lieu, type_entreprise, criteres_additionnels, heure),
            )
            search_id = cur.fetchone()[0]
        conn.commit()
        return search_id
    finally:
        conn.close()


def list_scheduled_searches(workspace_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, lieu, type_entreprise, criteres_additionnels, heure, actif, derniere_execution
                FROM scheduled_searches WHERE workspace_id = %s ORDER BY created_at DESC
                """,
                (workspace_id,),
            )
            rows = cur.fetchall()
        cols = ["id", "lieu", "type_entreprise", "criteres_additionnels", "heure", "actif", "derniere_execution"]
        results = [dict(zip(cols, r)) for r in rows]
        for r in results:
            if r["heure"] is not None:
                r["heure"] = r["heure"].strftime("%H:%M")
            if r["derniere_execution"] is not None:
                r["derniere_execution"] = r["derniere_execution"].isoformat()
        return results
    finally:
        conn.close()


def set_scheduled_search_active(workspace_id, search_id, actif):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE scheduled_searches SET actif = %s WHERE id = %s AND workspace_id = %s RETURNING id",
                (actif, search_id, workspace_id),
            )
            updated = cur.fetchone()
        conn.commit()
        if not updated:
            raise GeminiError("Recherche planifiée introuvable.")
    finally:
        conn.close()


def delete_scheduled_search(workspace_id, search_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM scheduled_searches WHERE id = %s AND workspace_id = %s RETURNING id",
                (search_id, workspace_id),
            )
            deleted = cur.fetchone()
        conn.commit()
        if not deleted:
            raise GeminiError("Recherche planifiée introuvable.")
    finally:
        conn.close()


def run_due_scheduled_searches():
    """Exécute les recherches planifiées dont l'heure est passée aujourd'hui et qui n'ont
    pas encore tourné aujourd'hui. Les résultats sont stockés pour vérification manuelle,
    jamais insérés directement en base — même principe que la recherche interactive."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, workspace_id, lieu, type_entreprise, criteres_additionnels
                FROM scheduled_searches
                WHERE actif = TRUE
                  AND heure <= CURRENT_TIME
                  AND (derniere_execution IS NULL OR derniere_execution < CURRENT_DATE)
                """
            )
            due = cur.fetchall()
    finally:
        conn.close()

    executed = 0
    for search_id, workspace_id, lieu, type_entreprise, criteres in due:
        try:
            result = perform_search(workspace_id, lieu, type_entreprise, criteres)
        except (QuotaExceeded, GeminiError):
            # Quota atteint ou erreur Gemini : on retentera au prochain cycle du planificateur
            # tant que derniere_execution n'est pas mise à jour.
            continue

        conn = get_db()
        try:
            with conn.cursor() as cur:
                for prospect in result["prospects"]:
                    cur.execute(
                        """
                        INSERT INTO scheduled_search_results (scheduled_search_id, workspace_id, fields)
                        VALUES (%s, %s, %s)
                        """,
                        (search_id, workspace_id, json.dumps(prospect)),
                    )
                cur.execute(
                    "UPDATE scheduled_searches SET derniere_execution = CURRENT_DATE WHERE id = %s",
                    (search_id,),
                )
            conn.commit()
            executed += 1
        finally:
            conn.close()

    return executed


def list_pending_scheduled_results(workspace_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, fields, created_at FROM scheduled_search_results
                WHERE workspace_id = %s AND statut = 'a_verifier'
                ORDER BY created_at DESC
                """,
                (workspace_id,),
            )
            rows = cur.fetchall()
        return [{"id": r[0], **r[1], "found_at": r[2]} for r in rows]
    finally:
        conn.close()


def dismiss_scheduled_result(workspace_id, result_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE scheduled_search_results SET statut = 'traite' WHERE id = %s AND workspace_id = %s RETURNING id",
                (result_id, workspace_id),
            )
            updated = cur.fetchone()
        conn.commit()
        if not updated:
            raise GeminiError("Résultat introuvable.")
    finally:
        conn.close()
