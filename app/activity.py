"""
Historique d'activité par prospect (idée produit "timeline") — trace les
événements clés d'un dossier au fil de l'eau : création, changement de
statut, RDV planifié, campagne reçue. Jamais modifiable après coup (pas de
fonction update/delete) : c'est un journal, pas un champ éditable.

Alimenté depuis plusieurs modules (prospects.py, rendez_vous.py, sending.py)
pour éviter d'avoir à recouper plusieurs écrans quand on reprend un dossier.
"""
from app.db import get_db

EVENT_TYPES = ("cree", "statut_change", "rdv_planifie", "campagne_envoyee", "note")


def log_event(prospect_id, workspace_id, event_type, description, user_id=None):
    if event_type not in EVENT_TYPES:
        event_type = "note"
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO prospect_activity (prospect_id, workspace_id, event_type, description, user_id)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (prospect_id, workspace_id, event_type, description, user_id),
            )
        conn.commit()
    finally:
        conn.close()


def list_activity(prospect_id, workspace_id, limit=100):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT event_type, description, created_at
                FROM prospect_activity
                WHERE prospect_id = %s AND workspace_id = %s
                ORDER BY created_at DESC LIMIT %s
                """,
                (prospect_id, workspace_id, limit),
            )
            rows = cur.fetchall()
        return [{"event_type": r[0], "description": r[1], "created_at": r[2]} for r in rows]
    finally:
        conn.close()
