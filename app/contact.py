"""
Formulaire de contact public (/contact) — visiteurs non connectés, donc pas
de workspace_id disponible (distinct de admin_feedback, réservé aux clients
déjà inscrits). Le message est toujours enregistré en base, même si la
notification par e-mail à Alexis échoue, pour ne jamais perdre un message.
"""
from app.db import get_db

MAX_PER_IP_PER_HOUR = 5


def is_rate_limited(ip_address):
    """Anti-spam simple : au-delà de MAX_PER_IP_PER_HOUR messages depuis la
    même IP en une heure, on bloque. Pas besoin d'un système aussi strict que
    rate_limit.py (anti brute-force de connexion) : ici il s'agit juste
    d'éviter qu'un script n'inonde la boîte mail de contact."""
    if not ip_address:
        return False
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) FROM contact_messages
                WHERE ip_address = %s AND created_at > now() - interval '1 hour'
                """,
                (ip_address,),
            )
            count = cur.fetchone()[0]
        return count >= MAX_PER_IP_PER_HOUR
    finally:
        conn.close()


def create_message(name, email, message, ip_address):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO contact_messages (name, email, message, ip_address)
                VALUES (%s, %s, %s, %s) RETURNING id
                """,
                (name, email, message, ip_address),
            )
            new_id = cur.fetchone()[0]
        conn.commit()
        return new_id
    finally:
        conn.close()


def mark_notified(message_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE contact_messages SET notified = TRUE WHERE id = %s",
                (message_id,),
            )
        conn.commit()
    finally:
        conn.close()
