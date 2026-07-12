"""
Types de statut personnalisables (Option 3+ : organisation des prospects par
forme juridique ou catégorie, pour cibler des campagnes par type).

Chaque nouvel espace de travail démarre avec 4 types par défaut (SARL, SASU,
Association, Autre), librement modifiables ensuite par l'administrateur.
"""
from app.db import get_db

DEFAULT_TYPES = ["SARL", "SASU", "Association", "Autre"]


class ProspectTypeError(Exception):
    pass


def seed_default_types(workspace_id, conn=None):
    """Appelée à la création d'un espace de travail. Si conn est fourni, réutilise
    la même transaction (utile lors de l'inscription, avant le commit final)."""
    owns_conn = conn is None
    conn = conn or get_db()
    try:
        with conn.cursor() as cur:
            for nom in DEFAULT_TYPES:
                cur.execute(
                    "INSERT INTO prospect_types (workspace_id, nom) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (workspace_id, nom),
                )
        if owns_conn:
            conn.commit()
    finally:
        if owns_conn:
            conn.close()


def list_types(workspace_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT pt.id, pt.nom, count(p.id) AS nb_prospects
                FROM prospect_types pt
                LEFT JOIN prospects p ON p.prospect_type_id = pt.id
                WHERE pt.workspace_id = %s
                GROUP BY pt.id, pt.nom
                ORDER BY pt.nom
                """,
                (workspace_id,),
            )
            rows = cur.fetchall()
        return [{"id": r[0], "nom": r[1], "nb_prospects": r[2]} for r in rows]
    finally:
        conn.close()


def create_type(workspace_id, nom):
    nom = (nom or "").strip()
    if not nom:
        raise ProspectTypeError("Le nom du type est requis.")
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO prospect_types (workspace_id, nom) VALUES (%s, %s) RETURNING id",
                (workspace_id, nom),
            )
            type_id = cur.fetchone()[0]
        conn.commit()
        return type_id
    except Exception as exc:
        conn.rollback()
        if "prospect_types_workspace_id_nom_key" in str(exc):
            raise ProspectTypeError("Ce type existe déjà.") from exc
        raise
    finally:
        conn.close()


def delete_type(workspace_id, type_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM prospect_types WHERE id = %s AND workspace_id = %s RETURNING id",
                (type_id, workspace_id),
            )
            deleted = cur.fetchone()
        conn.commit()
        if not deleted:
            raise ProspectTypeError("Type introuvable.")
    finally:
        conn.close()


def count_unclassified(workspace_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM prospects WHERE workspace_id = %s AND prospect_type_id IS NULL",
                (workspace_id,),
            )
            return cur.fetchone()[0]
    finally:
        conn.close()
