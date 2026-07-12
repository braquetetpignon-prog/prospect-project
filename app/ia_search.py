"""
Recherche IA intégrée (Option 2) — fournisseur Gemini, palier gratuit.

Flux : l'utilisateur personnalise seulement le lieu et le type d'entreprise
(+ un critère optionnel). Le prompt est pré-construit côté serveur, envoyé à
Gemini, la réponse structurée est retournée pour relecture. Rien n'est
enregistré automatiquement dans une fiche prospect — l'insertion reste une
action manuelle et volontaire de l'utilisateur, faite séparément.

Quota : 3 lancements par jour par espace de travail (table ia_search_log).
"""
import json
import os

import requests

from app.db import get_db

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

DAILY_QUOTA = 3
NOMBRE_RESULTATS = 8
REQUEST_TIMEOUT = 30

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "prospects": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "nom_entreprise": {"type": "STRING"},
                    "adresse": {"type": "STRING"},
                    "ville": {"type": "STRING"},
                    "telephone": {"type": "STRING"},
                    "site_web": {"type": "STRING"},
                    "description": {"type": "STRING"},
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

Propose jusqu'à {nombre_resultats} entreprises plausibles correspondant à ces critères.
Pour chaque entreprise, ne renseigne un champ (adresse, téléphone, site web) que si tu es \
raisonnablement certain de son exactitude. Laisse-le vide plutôt que d'inventer une \
information. Cette liste sera systématiquement relue et vérifiée par un humain avant tout \
usage — elle ne doit donc contenir aucune donnée present\u00e9e comme certaine si elle ne l'est pas."""


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
        return {"used": used, "limit": DAILY_QUOTA, "remaining": max(0, DAILY_QUOTA - used)}
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

    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "response_mime_type": "application/json",
            "response_schema": RESPONSE_SCHEMA,
        },
    }
    try:
        resp = requests.post(
            GEMINI_URL,
            headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY},
            json=body,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise GeminiError(f"Erreur réseau vers Gemini : {exc}") from exc

    if resp.status_code == 429:
        raise GeminiError(
            "Quota Gemini atteint au niveau du compte Google (palier gratuit global). Réessayez plus tard."
        )
    if resp.status_code >= 400:
        raise GeminiError(f"Erreur Gemini ({resp.status_code}) : {resp.text[:300]}")

    data = resp.json()
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(text)
    except (KeyError, IndexError, json.JSONDecodeError) as exc:
        raise GeminiError(f"Réponse Gemini inattendue : {exc}") from exc

    return parsed.get("prospects", [])


def perform_search(workspace_id, lieu, type_entreprise, criteres_additionnels=None):
    quota = get_quota_status(workspace_id)
    if quota["remaining"] <= 0:
        raise QuotaExceeded(
            f"Quota quotidien atteint ({quota['used']}/{quota['limit']} lancements aujourd'hui)."
        )

    prompt = build_prompt(lieu, type_entreprise, criteres_additionnels)
    prospects = call_gemini(prompt)

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
