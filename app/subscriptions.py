"""
Abonnement par espace de travail : essai gratuit de TRIAL_DAYS jours à
l'inscription, puis bascule automatique en version gratuite restreinte si
personne n'est passé en payant (fait manuellement par le superadmin pour
l'instant, aucune intégration de paiement).

Principe important : le statut stocké en base (colonne `plan`) n'est jamais
lu seul. Le statut EFFECTIF est toujours recalculé à la volée à partir de
`trial_ends_at` / `paid_until` (voir effective_plan ci-dessous) — ainsi un
essai ou un abonnement payant expiré retombe automatiquement en 'free' sans
dépendre d'une tâche planifiée qui pourrait ne pas s'être exécutée.
"""
from datetime import datetime, timedelta, timezone

from app.db import get_db

TRIAL_DAYS = 7

# Fonctionnalités désactivées en version gratuite ('free' uniquement — jamais
# pendant l'essai ni en payant).
RESTRICTED_PLAN = "free"


def trial_end_date():
    return datetime.now(timezone.utc) + timedelta(days=TRIAL_DAYS)


def effective_plan(plan, trial_ends_at, paid_until):
    """Calcule le statut réel à l'instant présent, indépendamment de ce que
    dit la colonne `plan` — un essai ou un payant expiré est traité comme
    'free' même si personne n'a encore mis à jour la ligne en base."""
    now = datetime.now(timezone.utc)

    if plan == "paid":
        if paid_until and paid_until < now:
            return "free"
        return "paid"

    if plan == "trial":
        if trial_ends_at and trial_ends_at < now:
            return "free"
        return "trial"

    return "free"


def get_workspace_subscription(workspace_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT plan, trial_ends_at, paid_until, billing_interval FROM workspaces WHERE id = %s",
                (workspace_id,),
            )
            row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        return None

    plan, trial_ends_at, paid_until, billing_interval = row
    effective = effective_plan(plan, trial_ends_at, paid_until)

    days_left = None
    if effective == "trial" and trial_ends_at:
        days_left = max(0, (trial_ends_at - datetime.now(timezone.utc)).days)

    return {
        "plan": plan,
        "plan_effective": effective,
        "trial_ends_at": trial_ends_at,
        "paid_until": paid_until,
        "billing_interval": billing_interval,
        "trial_days_left": days_left,
        "restricted": effective == RESTRICTED_PLAN,
    }


def is_restricted(workspace_id):
    """True si l'espace de travail est actuellement en version gratuite
    restreinte (export CSV et envoi de campagnes désactivés)."""
    sub = get_workspace_subscription(workspace_id)
    return bool(sub and sub["restricted"])
