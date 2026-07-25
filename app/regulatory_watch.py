"""
Veille réglementaire automatisée.

Surveille une liste de sources officielles (CNIL, service-public.fr,
Légifrance...) configurées par le superadmin dans /supadmin. Une fois par
jour, chaque source active est récupérée, son contenu texte est comparé au
dernier relevé connu (hash) ; en cas de changement, un résumé assisté par
IA (Gemini, même mécanisme et même clé que la Recherche IA — voir
app/ia_search.py) tente d'expliquer ce qui a changé et si ça peut concerner
ClickProspect.

Important, dans le même esprit que les autres automatisations de l'app
(app/automations.py, app/lifecycle.py) : cette tâche ne modifie JAMAIS rien
de son propre chef, que ce soit dans les documents légaux, le code ou la
configuration. Elle se contente de signaler (table regulatory_alerts) —
toute suite (mise à jour d'un document, d'une fonctionnalité...) reste une
décision et une action humaines, en session avec Claude ou avec le DPO.

L'IA est aussi utilisée en amont, pour son résumé : ce résumé peut se
tromper ou passer à côté d'une nuance, il ne remplace pas une lecture
humaine de la source elle-même (le lien est toujours conservé dans l'alerte).

Appelée depuis app/scheduler.py, mais ne fait le travail réel qu'une fois
par jour (throttle via app_settings), pas à chaque passage du planificateur
(toutes les 30s).
"""
import hashlib
import json
import re

import requests

from app.db import get_db
from app.app_logging import logger
from app.ia_search import GEMINI_API_KEY, get_current_model, INTERACTIONS_API_URL, API_REVISION

_LAST_RUN_KEY = "regulatory_watch_last_check_date"
FETCH_TIMEOUT = 20
FETCH_USER_AGENT = "ClickProspect-VeilleReglementaire/1.0 (+contact@clickprospect.fr)"
GEMINI_TIMEOUT = 45
# Coupe le contenu envoyé à l'IA pour rester raisonnable en taille de requête
# et en coût — largement suffisant pour une page d'actualité ou de fiche
# pratique, ce n'est pas fait pour ingérer un texte de loi complet.
MAX_CONTENT_CHARS = 15000

SUMMARY_PROMPT_TEMPLATE = """Tu assistes l'équipe de ClickProspect, un CRM web pour artisans et petites \
entreprises françaises (gestion de prospects, campagnes e-mail, recherche IA de prospects, \
enrichissement via les API SIRENE / Recherche d'Entreprises / BODACC).

Voici le contenu actuel d'une page officielle que nous surveillons pour rester informés des évolutions \
réglementaires ou légales (RGPD, e-commerce, obligations d'un éditeur de logiciel SaaS français) :

Source : {source_name} ({url})

---
{content}
---

Réponds UNIQUEMENT avec un objet JSON de cette forme, sans aucun texte autour :
{{"resume": "résumé factuel en 2-3 phrases, en français simple, de ce que dit la page", \
"pertinence_clickprospect": "1-2 phrases sur si/pourquoi ceci pourrait concerner ClickProspect ; \
si tu ne peux pas trancher avec certitude, dis-le explicitement plutôt que de deviner"}}
"""


class RegulatoryWatchError(Exception):
    pass


# --- Gestion des sources -----------------------------------------------

def list_sources():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, url, category, active, last_checked_at, last_check_error,
                       last_content_hash
                FROM regulatory_sources
                ORDER BY category NULLS LAST, name
                """
            )
            cols = [c.name for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def add_source(name, url, category=None):
    name = (name or "").strip()
    url = (url or "").strip()
    if not name or not url:
        raise RegulatoryWatchError("Le nom et l'URL de la source sont obligatoires.")
    if not (url.startswith("http://") or url.startswith("https://")):
        raise RegulatoryWatchError("L'URL doit commencer par http:// ou https://.")
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO regulatory_sources (name, url, category) VALUES (%s, %s, %s) RETURNING id",
                (name, url, (category or "").strip() or None),
            )
            new_id = cur.fetchone()[0]
        conn.commit()
        return new_id
    finally:
        conn.close()


def set_source_active(source_id, active):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE regulatory_sources SET active = %s WHERE id = %s",
                (bool(active), source_id),
            )
        conn.commit()
    finally:
        conn.close()


def delete_source(source_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM regulatory_sources WHERE id = %s", (source_id,))
        conn.commit()
    finally:
        conn.close()


# --- Gestion des alertes -------------------------------------------------

def list_alerts(status=None, limit=200):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            query = """
                SELECT a.id, a.source_id, s.name AS source_name, s.url AS source_url,
                       s.category, a.detected_at, a.resume, a.pertinence, a.status,
                       a.notes, a.reviewed_at
                FROM regulatory_alerts a
                JOIN regulatory_sources s ON s.id = a.source_id
            """
            params = []
            if status:
                query += " WHERE a.status = %s"
                params.append(status)
            query += " ORDER BY a.detected_at DESC LIMIT %s"
            params.append(limit)
            cur.execute(query, params)
            cols = [c.name for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def set_alert_status(alert_id, status, notes=None, reviewed_by=None):
    if status not in ("nouveau", "en_cours", "traite", "sans_suite"):
        raise RegulatoryWatchError(f"Statut inconnu : {status}")
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE regulatory_alerts
                SET status = %s, notes = COALESCE(%s, notes),
                    reviewed_at = now(), reviewed_by = %s
                WHERE id = %s
                """,
                (status, notes, reviewed_by, alert_id),
            )
        conn.commit()
    finally:
        conn.close()


def _insert_alert(source_id, resume, pertinence):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO regulatory_alerts (source_id, resume, pertinence)
                VALUES (%s, %s, %s)
                """,
                (source_id, resume, pertinence),
            )
        conn.commit()
    finally:
        conn.close()


# --- Détection de changement ----------------------------------------------

def _fetch_page_text(url):
    resp = requests.get(url, headers={"User-Agent": FETCH_USER_AGENT}, timeout=FETCH_TIMEOUT)
    resp.raise_for_status()
    html = resp.text
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;|&amp;|&#160;", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _content_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _summarize_change(source_name, url, content):
    """Ne lève jamais d'exception : en cas de souci IA, retourne un résumé de
    repli neutre plutôt que de faire échouer toute la vérification (le
    changement reste signalé, juste sans résumé automatique)."""
    if not GEMINI_API_KEY:
        return (
            "Résumé IA indisponible (clé Gemini non configurée sur le serveur) — "
            "contenu modifié, à relire directement sur la source.",
            "Non évalué automatiquement.",
        )
    prompt = SUMMARY_PROMPT_TEMPLATE.format(
        source_name=source_name, url=url, content=content[:MAX_CONTENT_CHARS]
    )
    body = {
        "model": get_current_model(),
        "input": prompt,
        "response_format": {
            "type": "text",
            "mime_type": "application/json",
            "schema": {
                "type": "object",
                "properties": {
                    "resume": {"type": "string"},
                    "pertinence_clickprospect": {"type": "string"},
                },
                "required": ["resume", "pertinence_clickprospect"],
            },
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
            timeout=GEMINI_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        text = ""
        for step in data.get("steps") or []:
            if step.get("type") != "model_output":
                continue
            for block in step.get("content") or []:
                if block.get("type") == "text" and block.get("text"):
                    text += block["text"]
        parsed = json.loads(text)
        return (
            parsed.get("resume") or "Contenu modifié — résumé IA vide.",
            parsed.get("pertinence_clickprospect") or "Non évalué.",
        )
    except Exception as exc:  # réseau, quota Gemini, JSON invalide...
        logger.warning("Résumé IA de veille réglementaire indisponible : %s", exc)
        return (
            "Résumé IA indisponible (erreur technique) — contenu modifié, à relire directement sur la source.",
            "Non évalué automatiquement.",
        )


def check_source(source):
    """Vérifie une source, insère une alerte si le contenu a changé depuis le
    dernier relevé. Ne lève pas d'exception vers l'appelant — toute erreur est
    consignée sur la source elle-même (last_check_error) pour rester visible
    dans /supadmin sans jamais casser la boucle du planificateur."""
    conn = get_db()
    try:
        try:
            text = _fetch_page_text(source["url"])
        except Exception as exc:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE regulatory_sources SET last_checked_at = now(), last_check_error = %s WHERE id = %s",
                    (str(exc)[:500], source["id"]),
                )
            conn.commit()
            return

        new_hash = _content_hash(text)
        changed = new_hash != source.get("last_content_hash")

        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE regulatory_sources
                SET last_checked_at = now(), last_check_error = NULL, last_content_hash = %s
                WHERE id = %s
                """,
                (new_hash, source["id"]),
            )
        conn.commit()

        # Premier relevé d'une source neuve : on enregistre le hash de
        # référence mais on ne crée pas d'alerte, il n'y a rien eu de
        # "changé" par rapport à un état précédent inexistant.
        if source.get("last_content_hash") is None:
            return

        if changed:
            resume, pertinence = _summarize_change(source["name"], source["url"], text)
            _insert_alert(source["id"], resume, pertinence)
    finally:
        conn.close()


def _already_ran_today():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM app_settings WHERE key = %s", (_LAST_RUN_KEY,))
            row = cur.fetchone()
        from datetime import datetime, timezone
        return bool(row and row[0] == datetime.now(timezone.utc).date().isoformat())
    finally:
        conn.close()


def _mark_ran_today():
    conn = get_db()
    try:
        from datetime import datetime, timezone
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app_settings (key, value, updated_at) VALUES (%s, %s, now())
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
                """,
                (_LAST_RUN_KEY, datetime.now(timezone.utc).date().isoformat()),
            )
        conn.commit()
    finally:
        conn.close()


def run_due_checks(force=False):
    """Point d'entrée appelé par le planificateur. `force=True` permet aussi
    un déclenchement manuel immédiat depuis /supadmin (bouton "Vérifier
    maintenant"), en dehors du rythme quotidien normal."""
    if not force and _already_ran_today():
        return
    for source in list_sources():
        if not source["active"]:
            continue
        try:
            check_source(source)
        except Exception:
            logger.exception("Échec de check_source() pour la source %s", source.get("name"))
    if not force:
        _mark_ran_today()
