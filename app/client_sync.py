"""
Synchronisation automatique des profils "administrateur" des espaces de
travail clients vers l'espace de travail personnel du superadmin, dans sa
propre rubrique Gestion Client — pour qu'il puisse gérer ses clients
ClickProspect avec ClickProspect lui-même (campagnes, relances...).

Se déclenche à chaque enregistrement du profil "Mon compte" par
l'administrateur d'un espace de travail (voir app/auth.py::update_profile,
appelée depuis la route PUT /api/auth/profile), uniquement si une cible a
été configurée par un administrateur superadmin depuis /supadmin (voir
set_crm_target_workspace_id — désactivé par défaut, tant qu'aucune cible
n'est choisie, rien ne se synchronise).

Ne synchronise QUE les informations d'identité/contact fournies
volontairement par l'administrateur (nom, prénom, téléphone, nom
d'entreprise, SIRET, adresse) — jamais son mot de passe, ni son PIN, qui ne
transitent jamais par ce module. Voir la politique de confidentialité,
section "Les finalités des traitements", pour la base légale de cet usage.
"""
from app.db import get_db
from app import subscriptions

CRM_TARGET_SETTING_KEY = "supadmin_crm_workspace_id"


def get_crm_target_workspace_id():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM app_settings WHERE key = %s", (CRM_TARGET_SETTING_KEY,))
            row = cur.fetchone()
    finally:
        conn.close()
    return int(row[0]) if row and row[0] else None


def set_crm_target_workspace_id(workspace_id):
    """workspace_id=None désactive la synchronisation."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app_settings (key, value, updated_at) VALUES (%s, %s, now())
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
                """,
                (CRM_TARGET_SETTING_KEY, str(workspace_id) if workspace_id else None),
            )
        conn.commit()
    finally:
        conn.close()


def sync_workspace_admin_to_crm(source_workspace_id):
    """Point d'entrée appelé après l'enregistrement du profil d'un
    administrateur (voir app/auth.py::update_profile)."""
    _trigger_sync(source_workspace_id)


def sync_subscription_status(source_workspace_id):
    """Point d'entrée appelé quand le plan ou la date de fin d'abonnement
    d'un espace change (premier paiement Mollie, renouvellement, ou
    changement manuel de plan via /supadmin) — met à jour la fiche client
    synchronisée avec le nouveau statut et la nouvelle date de fin, pour
    permettre de filtrer facilement les renouvellements récents dans un
    export CSV (facturation manuelle). Crée la fiche si elle n'existe pas
    encore (ex: abonnement souscrit avant tout enregistrement de profil)."""
    _trigger_sync(source_workspace_id)


def _trigger_sync(source_workspace_id):
    """Ne fait rien silencieusement si aucune cible n'est configurée, ou si
    l'espace source EST la cible (on ne se synchronise pas soi-même). Ne
    lève jamais d'exception vers l'appelant : un souci de synchronisation ne
    doit jamais bloquer l'action déclenchante (enregistrement de profil,
    webhook de paiement, changement de plan)."""
    try:
        target_id = get_crm_target_workspace_id()
        if not target_id or target_id == source_workspace_id:
            return
        _do_sync(source_workspace_id, target_id)
    except Exception:
        pass


def _subscription_fields(plan, trial_ends_at, paid_until, billing_interval):
    """Statut lisible + date de fin, calculés sur le statut RÉEL
    (subscriptions.effective_plan, pas seulement la colonne `plan` qui peut
    rester à 'trial' indéfiniment après expiration si personne ne l'a mise à
    jour) — important pour ne pas laisser croire à un essai encore actif
    dans l'export de facturation."""
    effective = subscriptions.effective_plan(plan, trial_ends_at, paid_until)
    if effective == "trial":
        return "Essai", trial_ends_at.date() if trial_ends_at else None
    if effective == "paid":
        label = "Payant annuel" if billing_interval == "annual" else "Payant mensuel"
        return label, paid_until.date() if paid_until else None
    return "Gratuit", None


def _do_sync(source_workspace_id, target_workspace_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT w.name, w.siret, w.adresse, w.code_postal, w.ville,
                       w.plan, w.trial_ends_at, w.paid_until, w.billing_interval,
                       u.first_name, u.last_name, u.phone, u.email
                FROM workspaces w
                JOIN users u ON u.workspace_id = w.id AND u.role = 'admin' AND u.is_active
                WHERE w.id = %s
                ORDER BY u.created_at
                LIMIT 1
                """,
                (source_workspace_id,),
            )
            row = cur.fetchone()
            if not row:
                return
            (company_name, siret, adresse, code_postal, ville,
             plan, trial_ends_at, paid_until, billing_interval,
             first_name, last_name, phone, email) = row

            subscription_status, subscription_end_date = _subscription_fields(
                plan, trial_ends_at, paid_until, billing_interval
            )

            cur.execute(
                "SELECT id FROM prospects WHERE workspace_id = %s AND synced_from_workspace_id = %s",
                (target_workspace_id, source_workspace_id),
            )
            existing = cur.fetchone()

            fields = {
                "nom_entreprise": company_name,
                "contact_prenom": first_name,
                "contact_nom": last_name,
                "siret": siret,
                "adresse": adresse,
                "code_postal": code_postal,
                "ville": ville,
                "telephone": phone,
                "email": email,
            }
            fields = {k: v for k, v in fields.items() if v}
            # Ces deux-là sont toujours renseignés, même à "Gratuit"/None —
            # contrairement aux autres champs, l'export de facturation doit
            # refléter l'état réel actuel, pas seulement la dernière valeur
            # connue non vide.
            fields["synced_subscription_status"] = subscription_status
            fields["synced_subscription_end_date"] = subscription_end_date

            if existing:
                prospect_id = existing[0]
                if fields:
                    set_clause = ", ".join(f"{k} = %s" for k in fields)
                    cur.execute(
                        f"UPDATE prospects SET {set_clause}, updated_at = now() WHERE id = %s",
                        (*fields.values(), prospect_id),
                    )
            else:
                if not company_name:
                    return  # nom_entreprise est NOT NULL en base, rien à créer sans lui
                fields.setdefault(
                    "notes",
                    "Client ClickProspect — profil synchronisé automatiquement depuis son espace de travail.",
                )
                columns = list(fields.keys()) + ["workspace_id", "statut", "source", "synced_from_workspace_id"]
                placeholders = ", ".join(["%s"] * len(columns))
                values = list(fields.values()) + [target_workspace_id, "client", "sync_compte_client", source_workspace_id]
                cur.execute(
                    f"INSERT INTO prospects ({', '.join(columns)}) VALUES ({placeholders}) RETURNING id",
                    values,
                )
        conn.commit()
    finally:
        conn.close()


def sync_all_workspaces():
    """Resynchronisation manuelle en masse (bouton dédié dans /supadmin) —
    utile après avoir changé la cible, ou pour rattraper les profils déjà
    renseignés avant l'activation de cette fonctionnalité. Renvoie le
    nombre d'espaces traités."""
    target_id = get_crm_target_workspace_id()
    if not target_id:
        return 0
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM workspaces WHERE id != %s", (target_id,))
            workspace_ids = [r[0] for r in cur.fetchall()]
    finally:
        conn.close()

    count = 0
    for wid in workspace_ids:
        try:
            _do_sync(wid, target_id)
            count += 1
        except Exception:
            pass
    return count
