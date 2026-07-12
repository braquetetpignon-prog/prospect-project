"""
Gestion des campagnes (Option 3, onglet Configuration) : avis, publicitaire,
newsletter. Templates préremplis modifiables, limite de 10 campagnes actives
par espace de travail.
"""
from app.db import get_db

MAX_ACTIVE_CAMPAIGNS = 10

CAMPAIGN_TYPES = ("avis", "publicitaire", "newsletter")

DEFAULT_TEMPLATES = {
    "avis": {
        "sujet": "Votre avis compte pour nous !",
        "contenu": (
            "Bonjour {prenom},\n\n"
            "Merci d'avoir fait appel à {nom_entreprise} récemment. Votre satisfaction est "
            "importante pour nous : auriez-vous un instant pour laisser un avis sur notre "
            "fiche Google ?\n\n"
            "{lien_avis_google}\n\n"
            "Merci beaucoup,\n"
            "L'équipe {nom_entreprise}\n\n"
            "{lien_desinscription}"
        ),
    },
    "publicitaire": {
        "sujet": "Une offre chez {nom_entreprise}",
        "contenu": (
            "Bonjour {prenom},\n\n"
            "[Décrivez ici votre offre ou actualité]\n\n"
            "N'hésitez pas à nous contacter pour en savoir plus.\n\n"
            "L'équipe {nom_entreprise}\n\n"
            "{lien_desinscription}"
        ),
    },
    "newsletter": {
        "sujet": "Les actualités de {nom_entreprise}",
        "contenu": (
            "Bonjour {prenom},\n\n"
            "[Vos actualités du mois]\n\n"
            "À bientôt,\n"
            "L'équipe {nom_entreprise}\n\n"
            "{lien_desinscription}"
        ),
    },
}


def _count_active(workspace_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM campaigns WHERE workspace_id = %s AND statut = 'active'",
                (workspace_id,),
            )
            return cur.fetchone()[0]
    finally:
        conn.close()


def list_campaigns(workspace_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, type, nom, sujet, contenu, quota_par_jour, statut, created_at
                FROM campaigns WHERE workspace_id = %s ORDER BY created_at DESC
                """,
                (workspace_id,),
            )
            rows = cur.fetchall()
        cols = ["id", "type", "nom", "sujet", "contenu", "quota_par_jour", "statut", "created_at"]
        return [dict(zip(cols, r)) for r in rows]
    finally:
        conn.close()


def create_campaign(workspace_id, type_, nom, sujet=None, contenu=None, quota_par_jour=100):
    if type_ not in CAMPAIGN_TYPES:
        raise ValueError(f"Type de campagne invalide : {type_} (attendu : {', '.join(CAMPAIGN_TYPES)})")

    if _count_active(workspace_id) >= MAX_ACTIVE_CAMPAIGNS:
        raise ValueError(
            f"Limite de {MAX_ACTIVE_CAMPAIGNS} campagnes actives atteinte pour cet espace de travail."
        )

    template = DEFAULT_TEMPLATES[type_]
    sujet = sujet or template["sujet"]
    contenu = contenu or template["contenu"]

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO campaigns (workspace_id, type, nom, sujet, contenu, quota_par_jour, statut)
                VALUES (%s, %s, %s, %s, %s, %s, 'active')
                RETURNING id
                """,
                (workspace_id, type_, nom, sujet, contenu, quota_par_jour),
            )
            campaign_id = cur.fetchone()[0]
        conn.commit()
        return campaign_id
    finally:
        conn.close()


def update_campaign(workspace_id, campaign_id, **fields):
    allowed = {"nom", "sujet", "contenu", "quota_par_jour", "statut"}
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        raise ValueError("Aucun champ valide à mettre à jour.")

    if updates.get("statut") == "active":
        if _count_active(workspace_id) >= MAX_ACTIVE_CAMPAIGNS:
            raise ValueError(
                f"Limite de {MAX_ACTIVE_CAMPAIGNS} campagnes actives atteinte pour cet espace de travail."
            )

    set_clause = ", ".join(f"{k} = %s" for k in updates)
    values = list(updates.values()) + [workspace_id, campaign_id]

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE campaigns SET {set_clause} WHERE workspace_id = %s AND id = %s RETURNING id",
                values,
            )
            updated = cur.fetchone()
        conn.commit()
        if not updated:
            raise ValueError("Campagne introuvable pour cet espace de travail.")
    finally:
        conn.close()
