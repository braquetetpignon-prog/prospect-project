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
                SELECT host, port, username, from_email, updated_at, verified, verified_at
                FROM smtp_configs WHERE workspace_id = %s
                """,
                (workspace_id,),
            )
            row = cur.fetchone()
        if not row:
            return {"configured": False, "verified": False}
        return {
            "configured": True,
            "host": row[0],
            "port": row[1],
            "username": row[2],
            "from_email": row[3],
            "updated_at": row[4],
            "verified": row[5],
            "verified_at": row[6],
        }
    finally:
        conn.close()


def set_smtp_config(workspace_id, host, port, username, password, from_email):
    """Toute modification des identifiants remet la config à 'non vérifiée' —
    un test d'envoi réussi (app/sending.py::send_smtp_test) est nécessaire
    avant de pouvoir l'utiliser dans une campagne (voir sending._process_one_send)."""
    password_encrypted = crypto_utils.encrypt(password)
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO smtp_configs (workspace_id, host, port, username, password_encrypted, from_email, updated_at, verified, verified_at)
                VALUES (%s, %s, %s, %s, %s, %s, now(), FALSE, NULL)
                ON CONFLICT (workspace_id) DO UPDATE SET
                    host = EXCLUDED.host,
                    port = EXCLUDED.port,
                    username = EXCLUDED.username,
                    password_encrypted = EXCLUDED.password_encrypted,
                    from_email = EXCLUDED.from_email,
                    updated_at = now(),
                    verified = FALSE,
                    verified_at = NULL
                """,
                (workspace_id, host, port, username, password_encrypted, from_email),
            )
        conn.commit()
    finally:
        conn.close()


def mark_smtp_verified(workspace_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE smtp_configs SET verified = TRUE, verified_at = now() WHERE workspace_id = %s",
                (workspace_id,),
            )
        conn.commit()
    finally:
        conn.close()


def get_smtp_credentials_for_sending(workspace_id, require_verified=False):
    """Usage interne uniquement (module d'envoi) — déchiffre le mot de passe.
    Ne jamais exposer le résultat de cette fonction via une route API.
    Si require_verified=True, renvoie None tant que le test d'envoi (bouton
    Paramètres) n'a pas réussi — utilisé pour bloquer l'envoi de campagnes
    tant que la config n'a pas été validée."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT host, port, username, password_encrypted, from_email, verified
                FROM smtp_configs WHERE workspace_id = %s
                """,
                (workspace_id,),
            )
            row = cur.fetchone()
        if not row:
            return None
        host, port, username, password_encrypted, from_email, verified = row
        if require_verified and not verified:
            return None
        return {
            "host": host,
            "port": port,
            "username": username,
            "password": crypto_utils.decrypt(password_encrypted),
            "from_email": from_email,
            "verified": verified,
        }
    finally:
        conn.close()
