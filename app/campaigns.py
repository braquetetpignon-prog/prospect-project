"""
Gestion des campagnes (Option 3, onglet Configuration) : avis, publicitaire,
newsletter, relance. Templates préremplis modifiables, limite de 10 campagnes
actives par espace de travail (1 seule en version gratuite, réservée au type
"relance" — voir _plan_limits ci-dessous).
"""
from app.db import get_db
from app import subscriptions

MAX_ACTIVE_CAMPAIGNS = 10

# Version gratuite : une seule campagne active, exclusivement de type "relance"
# (les autres types restent créables/visibles mais pas envoyables — cf. la
# vérification faite dans main.py au moment de l'envoi, pas ici à la création,
# pour ne pas bloquer un essai qui redeviendrait payant entre-temps).
FREE_PLAN_MAX_ACTIVE = 1
FREE_PLAN_ALLOWED_TYPES = ("relance",)

CAMPAIGN_TYPES = ("avis", "publicitaire", "newsletter", "relance")

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
    "relance": {
        "sujet": "On reste à votre disposition, {nom_entreprise}",
        "contenu": (
            "Bonjour {prenom},\n\n"
            "Nous étions récemment en contact au sujet de {nom_entreprise} — je voulais "
            "prendre de vos nouvelles et voir si vous aviez des questions ou si le moment "
            "était mieux choisi pour en reparler.\n\n"
            "N'hésitez pas à me répondre directement, je me ferai un plaisir d'échanger avec vous.\n\n"
            "Belle journée,\n"
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


def campaign_workspace_id(campaign_id):
    """Retourne le workspace_id de la campagne, ou None si elle n'existe pas."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT workspace_id FROM campaigns WHERE id = %s", (campaign_id,))
            row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def list_campaigns(workspace_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, type, nom, sujet, contenu, quota_par_jour, statut, created_at,
                       (image_data IS NOT NULL) AS has_image
                FROM campaigns WHERE workspace_id = %s ORDER BY created_at DESC
                """,
                (workspace_id,),
            )
            rows = cur.fetchall()
        cols = ["id", "type", "nom", "sujet", "contenu", "quota_par_jour", "statut", "created_at", "has_image"]
        return [dict(zip(cols, r)) for r in rows]
    finally:
        conn.close()


# --- Image insérée dans le corps du message (ré-encodée, cf. campaign_image.py) ---

def set_campaign_image(workspace_id, campaign_id, image_bytes, mimetype):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE campaigns SET image_data = %s, image_mimetype = %s, image_updated_at = now()
                WHERE id = %s AND workspace_id = %s RETURNING id
                """,
                (image_bytes, mimetype, campaign_id, workspace_id),
            )
            updated = cur.fetchone()
        conn.commit()
        if not updated:
            raise ValueError("Campagne introuvable pour cet espace de travail.")
    finally:
        conn.close()


def remove_campaign_image(workspace_id, campaign_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE campaigns SET image_data = NULL, image_mimetype = NULL, image_updated_at = NULL
                WHERE id = %s AND workspace_id = %s RETURNING id
                """,
                (campaign_id, workspace_id),
            )
            updated = cur.fetchone()
        conn.commit()
        if not updated:
            raise ValueError("Campagne introuvable pour cet espace de travail.")
    finally:
        conn.close()


def get_campaign_image(workspace_id, campaign_id):
    """Renvoie {"data": bytes, "mimetype": str} ou None si aucune image.
    workspace_id=None autorise la lecture cross-workspace pour un usage
    interne uniquement (worker d'envoi, cf. app/sending.py) — jamais depuis
    une route API accessible à l'utilisateur."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            if workspace_id is None:
                cur.execute(
                    "SELECT image_data, image_mimetype FROM campaigns WHERE id = %s",
                    (campaign_id,),
                )
            else:
                cur.execute(
                    "SELECT image_data, image_mimetype FROM campaigns WHERE id = %s AND workspace_id = %s",
                    (campaign_id, workspace_id),
                )
            row = cur.fetchone()
        if not row or row[0] is None:
            return None
        return {"data": bytes(row[0]), "mimetype": row[1]}
    finally:
        conn.close()


def get_campaign(campaign_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, workspace_id, type, nom, sujet, contenu, quota_par_jour, statut FROM campaigns WHERE id = %s",
                (campaign_id,),
            )
            row = cur.fetchone()
        if not row:
            return None
        cols = ["id", "workspace_id", "type", "nom", "sujet", "contenu", "quota_par_jour", "statut"]
        return dict(zip(cols, row))
    finally:
        conn.close()


def _check_plan_limits(workspace_id, type_, is_activating):
    """Lève ValueError si la création/activation demandée dépasse ce que permet
    le plan actuel. Version gratuite : 1 seule campagne active, uniquement de
    type "relance". Essai et payant : jusqu'à MAX_ACTIVE_CAMPAIGNS, tout type."""
    sub = subscriptions.get_workspace_subscription(workspace_id)
    effective = sub["plan_effective"] if sub else "free"

    if effective == "free":
        if type_ not in FREE_PLAN_ALLOWED_TYPES:
            raise ValueError(
                "En version gratuite, seule la campagne de type « Relance » est disponible. "
                "Passe en Premium pour débloquer les autres types."
            )
        if is_activating and _count_active(workspace_id) >= FREE_PLAN_MAX_ACTIVE:
            raise ValueError(
                "Version gratuite limitée à 1 campagne active à la fois. "
                "Passe en Premium pour en activer davantage."
            )
    elif is_activating and _count_active(workspace_id) >= MAX_ACTIVE_CAMPAIGNS:
        raise ValueError(
            f"Limite de {MAX_ACTIVE_CAMPAIGNS} campagnes actives atteinte pour cet espace de travail."
        )


def create_campaign(workspace_id, type_, nom, sujet=None, contenu=None, quota_par_jour=100):
    if type_ not in CAMPAIGN_TYPES:
        raise ValueError(f"Type de campagne invalide : {type_} (attendu : {', '.join(CAMPAIGN_TYPES)})")

    _check_plan_limits(workspace_id, type_, is_activating=True)

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
        current = get_campaign(campaign_id)
        type_ = current["type"] if current else None
        _check_plan_limits(workspace_id, type_, is_activating=True)

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
