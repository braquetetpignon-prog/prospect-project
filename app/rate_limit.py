"""
Limitation des tentatives de connexion (anti brute-force), pour l'auth
utilisateur normale ET la console superadmin.

Approche : fenêtre glissante en base de données (pas de dépendance externe
type Redis, cohérent avec l'infra actuelle — un seul Postgres partagé entre
tous les workers gunicorn, donc le comptage reste correct même avec
plusieurs workers/instances).

Chaque tentative (réussie ou non) est journalisée. Les échecs récents sont
comptés par IDENTIFIANT (email visé — protège un compte précis contre un
bourrage ciblé) ET par IP (protège contre un attaquant qui teste beaucoup de
comptes différents depuis la même adresse) ; le blocage le plus strict des
deux s'applique.

Le compte superadmin est traité à part avec un seuil plus bas : c'est la
cible la plus sensible du site (accès à tous les espaces de travail), et il
n'y a qu'un seul compte, donc pas de risque de bloquer un client légitime.
"""
from app.db import get_db

WINDOW_MINUTES = 15
MAX_ATTEMPTS_PER_IDENTIFIER = 8
MAX_ATTEMPTS_PER_IP = 20
MAX_ATTEMPTS_SUPERADMIN_IDENTIFIER = 5
MAX_ATTEMPTS_SUPERADMIN_IP = 8


def record_attempt(identifier, ip_address, success):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO login_attempts (identifier, ip_address, success) VALUES (%s, %s, %s)",
                (identifier.lower().strip() if identifier else None, ip_address, success),
            )
        conn.commit()
    finally:
        conn.close()


def is_rate_limited(identifier, ip_address, max_per_identifier=MAX_ATTEMPTS_PER_IDENTIFIER, max_per_ip=MAX_ATTEMPTS_PER_IP):
    """Renvoie (limited: bool, retry_after_seconds: int)."""
    identifier = identifier.lower().strip() if identifier else None
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) FROM login_attempts
                WHERE identifier = %s AND success = FALSE
                  AND created_at > now() - (%s || ' minutes')::interval
                """,
                (identifier, WINDOW_MINUTES),
            )
            by_identifier = cur.fetchone()[0]

            cur.execute(
                """
                SELECT count(*) FROM login_attempts
                WHERE ip_address = %s AND success = FALSE
                  AND created_at > now() - (%s || ' minutes')::interval
                """,
                (ip_address, WINDOW_MINUTES),
            )
            by_ip = cur.fetchone()[0]
        limited = by_identifier >= max_per_identifier or by_ip >= max_per_ip
        return limited, WINDOW_MINUTES * 60
    finally:
        conn.close()


def purge_old_attempts(older_than_hours=24):
    """Appelé depuis la maintenance quotidienne (lifecycle.py) pour ne pas
    laisser grossir la table indéfiniment."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM login_attempts WHERE created_at < now() - (%s || ' hours')::interval",
                (older_than_hours,),
            )
            deleted = cur.rowcount
        conn.commit()
        return deleted
    finally:
        conn.close()
