"""Bandeaux d'aide contextuelle effaçables par l'utilisateur (voir
schema.sql::user_dismissed_hints). Générique : n'importe quelle page peut
définir une hint_key et l'afficher tant que l'utilisateur ne l'a pas
masquée — pas besoin de nouvelle table/migration à chaque nouveau rappel.

Usage type (voir campagnes.html pour un exemple concret) :
    - GET  /api/hints/dismissed         -> liste des hint_key déjà masquées
                                            par l'utilisateur courant
    - POST /api/hints/<hint_key>/dismiss -> marque cette hint_key comme
                                            masquée pour l'utilisateur courant

Volontairement pas de mécanisme pour "réafficher" un bandeau une fois
masqué (pas demandé, et une hint effacée par erreur n'est pas une
situation à risque — l'information reste consultable ailleurs, ex. la
fiche prospect elle-même pour le rappel consentement)."""

from app.db import get_db


def get_dismissed_hints(user_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT hint_key FROM user_dismissed_hints WHERE user_id = %s",
                (user_id,),
            )
            return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def dismiss_hint(user_id, hint_key):
    hint_key = (hint_key or "").strip()
    if not hint_key or len(hint_key) > 100:
        raise ValueError("Identifiant de bandeau invalide.")
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO user_dismissed_hints (user_id, hint_key)
                VALUES (%s, %s)
                ON CONFLICT (user_id, hint_key) DO NOTHING
                """,
                (user_id, hint_key),
            )
        conn.commit()
    finally:
        conn.close()
