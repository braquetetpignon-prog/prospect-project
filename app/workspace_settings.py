"""
Paramètres de configuration par espace de travail : fiche Google Business
Profile et compte SMTP sortant (Option 3, onglet Configuration).
"""
from app.db import get_db
from app import crypto_utils


# --- Google Business Profile --------------------------------------------

def get_google_business_profile(workspace_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT profile_url FROM google_business_profiles WHERE workspace_id = %s",
                (workspace_id,),
            )
            row = cur.fetchone()
        return {"profile_url": row[0] if row else None}
    finally:
        conn.close()


def set_google_business_profile(workspace_id, profile_url):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO google_business_profiles (workspace_id, profile_url)
                VALUES (%s, %s)
                ON CONFLICT (workspace_id) DO UPDATE SET profile_url = EXCLUDED.profile_url
                """,
                (workspace_id, profile_url),
            )
        conn.commit()
    finally:
        conn.close()


# --- Configuration SMTP ---------------------------------------------------

def get_smtp_config(workspace_id):
    """Ne renvoie jamais le mot de passe, même chiffré — seulement le statut."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT host, port, username, from_email, updated_at
                FROM smtp_configs WHERE workspace_id = %s
                """,
                (workspace_id,),
            )
            row = cur.fetchone()
        if not row:
            return {"configured": False}
        return {
            "configured": True,
            "host": row[0],
            "port": row[1],
            "username": row[2],
            "from_email": row[3],
            "updated_at": row[4],
        }
    finally:
        conn.close()


def set_smtp_config(workspace_id, host, port, username, password, from_email):
    password_encrypted = crypto_utils.encrypt(password)
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO smtp_configs (workspace_id, host, port, username, password_encrypted, from_email, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (workspace_id) DO UPDATE SET
                    host = EXCLUDED.host,
                    port = EXCLUDED.port,
                    username = EXCLUDED.username,
                    password_encrypted = EXCLUDED.password_encrypted,
                    from_email = EXCLUDED.from_email,
                    updated_at = now()
                """,
                (workspace_id, host, port, username, password_encrypted, from_email),
            )
        conn.commit()
    finally:
        conn.close()


def get_smtp_credentials_for_sending(workspace_id):
    """Usage interne uniquement (module d'envoi) — déchiffre le mot de passe.
    Ne jamais exposer le résultat de cette fonction via une route API."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT host, port, username, password_encrypted, from_email
                FROM smtp_configs WHERE workspace_id = %s
                """,
                (workspace_id,),
            )
            row = cur.fetchone()
        if not row:
            return None
        host, port, username, password_encrypted, from_email = row
        return {
            "host": host,
            "port": port,
            "username": username,
            "password": crypto_utils.decrypt(password_encrypted),
            "from_email": from_email,
        }
    finally:
        conn.close()
