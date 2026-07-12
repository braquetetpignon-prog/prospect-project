"""
Gestion individuelle des prospects (hors import CSV en masse) : création
manuelle depuis l'interface, depuis un résultat de recherche IA, consultation,
mise à jour de statut.

Réutilise la validation et la protection anti-injection CSV déjà construites
dans csv_import.py, pour un comportement cohérent quelle que soit la source
d'une fiche prospect.
"""
from app.db import get_db
from app import csv_import

STATUTS = ("nouveau", "qualifie", "client", "recale")


class ProspectError(Exception):
    pass


def create_prospect(workspace_id, fields, source="manuel"):
    """fields : dict parmi csv_import.PROSPECT_FIELDS (ex: nom_entreprise, email, ville...)."""
    cleaned = {}
    for key, value in (fields or {}).items():
        if key not in csv_import.PROSPECT_FIELDS or value in (None, ""):
            continue
        cleaned[key] = csv_import.sanitize_cell(str(value))

    is_blocking, messages = csv_import.validate_row(dict(cleaned))
    if is_blocking:
        raise ProspectError("; ".join(messages) or "Fiche invalide.")

    conn = get_db()
    try:
        columns = list(cleaned.keys()) + ["workspace_id", "source"]
        placeholders = ", ".join(["%s"] * len(columns))
        values = list(cleaned.values()) + [workspace_id, source]
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO prospects ({', '.join(columns)}) VALUES ({placeholders}) RETURNING id",
                values,
            )
            prospect_id = cur.fetchone()[0]
        conn.commit()
        return prospect_id, messages  # messages = avertissements non bloquants éventuels
    finally:
        conn.close()


def get_prospect(prospect_id, workspace_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, nom_entreprise, contact_prenom, contact_nom, siren, siret,
                       naf_code, adresse, code_postal, ville, telephone, email, site_web,
                       statut, source, motif_recalage, created_at
                FROM prospects WHERE id = %s AND workspace_id = %s
                """,
                (prospect_id, workspace_id),
            )
            row = cur.fetchone()
        if not row:
            return None
        cols = ["id", "nom_entreprise", "contact_prenom", "contact_nom", "siren", "siret",
                "naf_code", "adresse", "code_postal", "ville", "telephone", "email", "site_web",
                "statut", "source", "motif_recalage", "created_at"]
        return dict(zip(cols, row))
    finally:
        conn.close()


def update_statut(prospect_id, workspace_id, statut, motif=None):
    if statut not in STATUTS:
        raise ProspectError(f"Statut invalide : {statut}")

    conn = get_db()
    try:
        with conn.cursor() as cur:
            if statut == "recale":
                cur.execute(
                    """
                    UPDATE prospects SET statut = %s, motif_recalage = %s, recale_at = now(), updated_at = now()
                    WHERE id = %s AND workspace_id = %s RETURNING id
                    """,
                    (statut, motif, prospect_id, workspace_id),
                )
            else:
                cur.execute(
                    """
                    UPDATE prospects SET statut = %s, updated_at = now()
                    WHERE id = %s AND workspace_id = %s RETURNING id
                    """,
                    (statut, prospect_id, workspace_id),
                )
            updated = cur.fetchone()
        conn.commit()
        if not updated:
            raise ProspectError("Prospect introuvable dans cet espace de travail.")
    finally:
        conn.close()
