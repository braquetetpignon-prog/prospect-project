"""
Gestion individuelle des prospects (hors import CSV en masse) : création
manuelle, édition complète, recherche, export CSV, vérification SIRET.

Réutilise la validation et la protection anti-injection CSV déjà construites
dans csv_import.py, pour un comportement cohérent quelle que soit la source
d'une fiche prospect.
"""
import csv
import io

import requests

from app.db import get_db
from app import csv_import
from app import activity

STATUTS = ("nouveau", "qualifie", "client", "recale")

EDITABLE_FIELDS = [
    "nom_entreprise", "contact_prenom", "contact_nom", "siren", "siret", "naf_code",
    "adresse", "batiment", "etage", "code_postal", "ville", "telephone", "email", "site_web",
    "prospect_type_id", "prochaine_action", "prochaine_action_date", "notes",
    "potentiel", "valeur_estimee",
]

SIRENE_SEARCH_URL = "https://recherche-entreprises.api.gouv.fr/search"


class ProspectError(Exception):
    pass


def create_prospect(workspace_id, fields, source="manuel", user_id=None):
    """fields : dict parmi csv_import.PROSPECT_FIELDS (ex: nom_entreprise, email, ville...).
    user_id : utilisateur à l'origine de la création, pour le rapport d'équipe
    (voir app/activity.py) — None pour une création automatisée."""
    cleaned = {}
    for key, value in (fields or {}).items():
        if key not in csv_import.PROSPECT_FIELDS or value in (None, ""):
            continue
        cleaned[key] = csv_import.sanitize_cell(str(value))

    is_blocking, messages = csv_import.validate_row(cleaned)
    if is_blocking:
        raise ProspectError("; ".join(messages) or "Fiche invalide.")
    # validate_row peut avoir normalisé certaines valeurs (ex: montant reformaté)
    # ou en avoir écarté d'autres (mises à None si invalides, ex: potentiel hors
    # bornes) — on répercute ces corrections avant l'insertion, sinon la valeur
    # d'origine non validée serait tout de même enregistrée.
    cleaned = {k: v for k, v in cleaned.items() if v is not None}

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
    finally:
        conn.close()

    activity.log_event(prospect_id, workspace_id, "cree", f"Fiche créée (source : {source}).", user_id=user_id)
    return prospect_id, messages  # messages = avertissements non bloquants éventuels


def get_prospect(prospect_id, workspace_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, nom_entreprise, contact_prenom, contact_nom, siren, siret,
                       naf_code, adresse, batiment, etage, code_postal, ville, telephone, email, site_web,
                       statut, source, motif_recalage, prospect_type_id, prochaine_action,
                       prochaine_action_date, notes, potentiel, valeur_estimee, created_at
                FROM prospects WHERE id = %s AND workspace_id = %s
                """,
                (prospect_id, workspace_id),
            )
            row = cur.fetchone()
        if not row:
            return None
        cols = ["id", "nom_entreprise", "contact_prenom", "contact_nom", "siren", "siret",
                "naf_code", "adresse", "batiment", "etage", "code_postal", "ville", "telephone", "email", "site_web",
                "statut", "source", "motif_recalage", "prospect_type_id", "prochaine_action",
                "prochaine_action_date", "notes", "potentiel", "valeur_estimee", "created_at"]
        return dict(zip(cols, row))
    finally:
        conn.close()


def update_prospect(prospect_id, workspace_id, fields):
    """Édition complète de la fiche (formulaire de la page Prospects)."""
    cleaned = {}
    for key, value in (fields or {}).items():
        if key not in EDITABLE_FIELDS:
            continue
        if key == "prospect_type_id":
            cleaned[key] = value or None
        else:
            cleaned[key] = csv_import.sanitize_cell(str(value)) if value else None

    if "nom_entreprise" in cleaned and not cleaned["nom_entreprise"]:
        raise ProspectError("nom_entreprise manquant (obligatoire)")

    # Validation ciblée (et non via csv_import.validate_row : cette fonction
    # exige nom_entreprise dans le dict fourni, ce qui casserait toute mise
    # à jour partielle ne touchant pas ce champ).
    if cleaned.get("potentiel") is not None:
        normalized, warning = csv_import._validate_potentiel(cleaned["potentiel"])
        if warning:
            raise ProspectError(warning)
        cleaned["potentiel"] = normalized
    if cleaned.get("valeur_estimee") is not None:
        normalized, warning = csv_import._validate_valeur_estimee(cleaned["valeur_estimee"])
        if warning:
            raise ProspectError(warning)
        cleaned["valeur_estimee"] = normalized

    if not cleaned:
        raise ProspectError("Aucun champ à mettre à jour.")

    set_clause = ", ".join(f"{k} = %s" for k in cleaned) + ", updated_at = now()"
    values = list(cleaned.values()) + [prospect_id, workspace_id]

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE prospects SET {set_clause} WHERE id = %s AND workspace_id = %s RETURNING id",
                values,
            )
            updated = cur.fetchone()
        conn.commit()
        if not updated:
            raise ProspectError("Prospect introuvable dans cet espace de travail.")
    finally:
        conn.close()


def update_statut(prospect_id, workspace_id, statut, motif=None, user_id=None):
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

    description = f"Statut changé en « {statut} »"
    if motif:
        description += f" (motif : {motif})"
    activity.log_event(prospect_id, workspace_id, "statut_change", description + ".", user_id=user_id)


def search_prospects(workspace_id, query=None, statut=None, prospect_type_id=None, limit=500):
    conditions = ["p.workspace_id = %s"]
    params = [workspace_id]

    if query:
        conditions.append("(p.nom_entreprise ILIKE %s OR p.contact_prenom ILIKE %s OR p.contact_nom ILIKE %s OR p.email ILIKE %s)")
        like = f"%{query}%"
        params += [like, like, like, like]
    if statut:
        if isinstance(statut, (list, tuple, set)):
            conditions.append("p.statut = ANY(%s)")
            params.append(list(statut))
        else:
            conditions.append("p.statut = %s")
            params.append(statut)
    if prospect_type_id:
        conditions.append("p.prospect_type_id = %s")
        params.append(prospect_type_id)

    params.append(limit)

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT p.id, p.nom_entreprise, p.contact_prenom, p.contact_nom, p.ville, p.email,
                       p.telephone, p.statut, p.source, pt.nom AS type_nom, p.created_at,
                       p.prochaine_action, p.prochaine_action_date, p.potentiel, p.valeur_estimee,
                       p.adresse, p.code_postal, p.batiment, p.etage, p.notes,
                       EXISTS (
                           SELECT 1 FROM rendez_vous rv
                           WHERE rv.prospect_id = p.id AND rv.date_heure > now()
                       ) AS has_upcoming_rdv
                FROM prospects p
                LEFT JOIN prospect_types pt ON pt.id = p.prospect_type_id
                WHERE {' AND '.join(conditions)}
                ORDER BY p.created_at DESC LIMIT %s
                """,
                params,
            )
            rows = cur.fetchall()
        cols = ["id", "nom_entreprise", "contact_prenom", "contact_nom", "ville", "email",
                "telephone", "statut", "source", "type_nom", "created_at",
                "prochaine_action", "prochaine_action_date", "potentiel", "valeur_estimee",
                "adresse", "code_postal", "batiment", "etage", "notes", "has_upcoming_rdv"]
        return [dict(zip(cols, r)) for r in rows]
    finally:
        conn.close()


def export_csv(workspace_id, statut=None):
    prospects = search_prospects(workspace_id, statut=statut, limit=100000)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "Entreprise", "Prénom contact", "Nom contact", "Ville", "Adresse", "Code postal",
        "Bâtiment", "Étage", "Email", "Téléphone", "Statut", "Type", "Source",
        "Potentiel", "Valeur estimée", "Notes",
    ])
    for p in prospects:
        writer.writerow([
            p["nom_entreprise"], p["contact_prenom"] or "", p["contact_nom"] or "", p["ville"] or "",
            p["adresse"] or "", p["code_postal"] or "", p["batiment"] or "", p["etage"] or "",
            p["email"] or "", p["telephone"] or "", p["statut"], p["type_nom"] or "", p["source"] or "",
            p["potentiel"] if p["potentiel"] is not None else "",
            p["valeur_estimee"] if p["valeur_estimee"] is not None else "",
            p["notes"] or "",
        ])
    return buf.getvalue()


def verify_siret(siret):
    """Interroge l'API officielle Recherche d'Entreprises (data.gouv.fr, gratuite)
    pour confirmer qu'un SIRET existe et récupérer les infos publiques associées."""
    siret = (siret or "").strip().replace(" ", "")
    if not siret or not siret.isdigit() or len(siret) != 14:
        raise ProspectError("Le SIRET doit contenir exactement 14 chiffres.")

    try:
        resp = requests.get(SIRENE_SEARCH_URL, params={"q": siret}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        raise ProspectError(f"Impossible de contacter le registre officiel : {exc}") from exc

    for result in data.get("results", []):
        candidates = [result.get("siege", {})] + result.get("matching_etablissements", [])
        for etab in candidates:
            if etab.get("siret") == siret:
                actif = etab.get("etat_administratif") == "A"
                return {
                    "found": True,
                    "actif": actif,
                    "nom_entreprise": result.get("nom_complet"),
                    "adresse": etab.get("adresse"),
                    "code_postal": etab.get("code_postal"),
                    "ville": etab.get("libelle_commune"),
                    "naf_code": etab.get("activite_principale"),
                }

    return {"found": False}


def count_overdue_actions(workspace_id):
    """Prospects qualifiés/en attente avec une prochaine_action_date dépassée
    et non recalés/déjà clients — sert au badge liste + au résumé hebdomadaire."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) FROM prospects
                WHERE workspace_id = %s AND statut IN ('nouveau', 'qualifie')
                  AND prochaine_action_date IS NOT NULL AND prochaine_action_date < CURRENT_DATE
                """,
                (workspace_id,),
            )
            return cur.fetchone()[0]
    finally:
        conn.close()


def delete_prospect(prospect_id, workspace_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM prospects WHERE id = %s AND workspace_id = %s RETURNING id",
                (prospect_id, workspace_id),
            )
            deleted = cur.fetchone()
        conn.commit()
        if not deleted:
            raise ProspectError("Prospect introuvable dans cet espace de travail.")
    finally:
        conn.close()


def delete_prospects_bulk(prospect_ids, workspace_id):
    """Suppression groupée — protège toujours les clients actifs (statut
    'client') d'un effacement accidentel via une sélection large ('tout
    sélectionner' puis supprimer) : ils sont silencieusement exclus, jamais
    supprimés par cette voie. Pour supprimer un client précis, il faut le
    faire depuis sa fiche (Gestion Client), un geste volontaire et unitaire."""
    if not prospect_ids:
        raise ProspectError("Aucun prospect sélectionné.")
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM prospects WHERE workspace_id = %s AND id = ANY(%s) AND statut != 'client' RETURNING id",
                (workspace_id, prospect_ids),
            )
            deleted_ids = [r[0] for r in cur.fetchall()]
            cur.execute(
                "SELECT count(*) FROM prospects WHERE workspace_id = %s AND id = ANY(%s) AND statut = 'client'",
                (workspace_id, prospect_ids),
            )
            protected_count = cur.fetchone()[0]
        conn.commit()
        return deleted_ids, protected_count
    finally:
        conn.close()
