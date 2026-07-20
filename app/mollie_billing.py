"""
Intégration Mollie (paiement CB, abonnement récurrent).

Principe : Mollie ne gère pas des "plans" comme Stripe — un abonnement est
juste (montant + fréquence + description) rattaché à un client Mollie. Notre
application pilote donc elle-même la logique métier (upgrade/downgrade =
annuler l'abonnement Mollie en cours puis en recréer un nouveau).

Flux :
1. L'utilisateur choisit un tarif -> create_first_payment() crée un paiement
   Mollie "premier paiement" et renvoie l'URL de paiement hébergée par Mollie.
2. Une fois payé, Mollie appelle notre webhook -> handle_webhook_payment()
   récupère le paiement, et si c'est un premier paiement réussi, crée
   l'abonnement récurrent (create_subscription) et met à jour l'espace de
   travail (plan='paid', paid_until, etc.).
3. À chaque renouvellement, Mollie crée un nouveau paiement pour cet
   abonnement et rappelle le webhook -> on prolonge paid_until.

Sécurité : le webhook ne fait jamais confiance au contenu reçu — il
recharge systématiquement le paiement depuis l'API Mollie avant d'agir
(pratique recommandée par Mollie, évite qu'une requête forgée ne déclenche
un changement de statut).
"""
import os
from datetime import datetime, timedelta, timezone

import requests

from app.db import get_db
from app import system_mail

MOLLIE_API_BASE = "https://api.mollie.com/v2"

PAYMENT_FAILED_SUBJECT = "Un problème est survenu avec votre paiement ClickProspect"
PAYMENT_FAILED_BODY_TEMPLATE = """Bonjour,

Le dernier prélèvement pour votre espace de travail « {workspace_name} » n'a
pas abouti (paiement {status}).

Votre accès reste actif pour le moment, mais nous vous invitons à vérifier
vos informations de paiement dès que possible pour éviter toute interruption
de service :
{app_base_url}/parametres

Si le problème persiste, n'hésitez pas à répondre à cet e-mail.

— L'équipe ClickProspect
"""

CARD_EXPIRING_SUBJECT = "Votre carte bancaire enregistrée sur ClickProspect expire bientôt"
CARD_EXPIRING_BODY_TEMPLATE = """Bonjour,

La carte bancaire enregistrée pour le paiement de « {workspace_name} »
expire fin {expiry_month_label}.

Pour éviter toute interruption au prochain prélèvement, pensez à mettre à
jour votre moyen de paiement :
{app_base_url}/parametres

— L'équipe ClickProspect
"""

# Fenêtre d'anticipation avant l'expiration réelle de la carte — choix
# raisonnable par défaut (le contexte v5 ne précisait pas de délai), à
# ajuster si Alexis préfère un délai différent.
CARD_EXPIRY_WARNING_DAYS = 30

PLAN_AMOUNTS = {
    "monthly": {"value": "12.00", "currency": "EUR", "label": "ClickProspect Premium — mensuel"},
    "annual": {"value": "108.00", "currency": "EUR", "label": "ClickProspect Premium — annuel"},
}
PLAN_INTERVALS = {
    "monthly": "1 month",
    "annual": "12 months",
}


class MollieError(Exception):
    pass


def _api_key():
    key = os.environ.get("MOLLIE_API_KEY")
    if not key:
        raise MollieError("MOLLIE_API_KEY n'est pas configurée sur ce serveur.")
    return key


def _headers():
    return {
        "Authorization": f"Bearer {_api_key()}",
        "Content-Type": "application/json",
    }


def _request(method, path, **kwargs):
    resp = requests.request(method, f"{MOLLIE_API_BASE}{path}", headers=_headers(), timeout=15, **kwargs)
    if resp.status_code >= 400:
        detail = ""
        try:
            detail = resp.json().get("detail", "")
        except Exception:
            pass
        raise MollieError(f"Erreur Mollie ({resp.status_code}) : {detail or resp.text[:200]}")
    return resp.json() if resp.text else {}


def _app_base_url():
    return os.environ.get("APP_BASE_URL", "https://preproduction.clickprospect.fr")


def _log_event(workspace_id, mollie_payment_id, event_type, details=""):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO mollie_events (workspace_id, mollie_payment_id, event_type, details)
                VALUES (%s, %s, %s, %s)
                """,
                (workspace_id, mollie_payment_id, event_type, details),
            )
        conn.commit()
    finally:
        conn.close()


def _get_or_create_customer(workspace_id, email, name):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT mollie_customer_id FROM workspaces WHERE id = %s", (workspace_id,))
            row = cur.fetchone()
    finally:
        conn.close()

    if row and row[0]:
        return row[0]

    customer = _request("POST", "/customers", json={
        "name": name,
        "email": email,
        "metadata": {"workspace_id": workspace_id},
    })
    customer_id = customer["id"]

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE workspaces SET mollie_customer_id = %s WHERE id = %s",
                (customer_id, workspace_id),
            )
        conn.commit()
    finally:
        conn.close()

    return customer_id


def create_first_payment(workspace_id, workspace_name, admin_email, interval):
    """Crée le premier paiement (carte) pour un espace de travail et renvoie
    l'URL de paiement hébergée par Mollie vers laquelle rediriger l'utilisateur."""
    if interval not in PLAN_AMOUNTS:
        raise MollieError(f"Intervalle de facturation invalide : {interval}")

    customer_id = _get_or_create_customer(workspace_id, admin_email, workspace_name)
    plan_info = PLAN_AMOUNTS[interval]

    payment = _request("POST", "/payments", json={
        "amount": {"currency": plan_info["currency"], "value": plan_info["value"]},
        "customerId": customer_id,
        "sequenceType": "first",
        "description": plan_info["label"],
        "redirectUrl": f"{_app_base_url()}/parametres?paiement=confirmation",
        "webhookUrl": f"{_app_base_url()}/webhook/mollie",
        "metadata": {"workspace_id": workspace_id, "interval": interval},
    })

    _log_event(workspace_id, payment["id"], "first_payment_created", f"Intervalle : {interval}")
    return payment["_links"]["checkout"]["href"]


def create_subscription(workspace_id, customer_id, interval):
    plan_info = PLAN_AMOUNTS[interval]
    subscription = _request("POST", f"/customers/{customer_id}/subscriptions", json={
        "amount": {"currency": plan_info["currency"], "value": plan_info["value"]},
        "interval": PLAN_INTERVALS[interval],
        "description": plan_info["label"],
        "webhookUrl": f"{_app_base_url()}/webhook/mollie",
        "metadata": {"workspace_id": workspace_id, "interval": interval},
    })
    return subscription["id"]


def cancel_subscription(workspace_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT mollie_customer_id, mollie_subscription_id FROM workspaces WHERE id = %s",
                (workspace_id,),
            )
            row = cur.fetchone()
    finally:
        conn.close()

    if not row or not row[0] or not row[1]:
        raise MollieError("Aucun abonnement Mollie actif pour cet espace de travail.")

    customer_id, subscription_id = row
    _request("DELETE", f"/customers/{customer_id}/subscriptions/{subscription_id}")

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE workspaces SET mollie_subscription_status = 'canceled' WHERE id = %s",
                (workspace_id,),
            )
        conn.commit()
    finally:
        conn.close()

    _log_event(workspace_id, None, "subscription_canceled_by_user")


def _period_end(interval, start=None):
    start = start or datetime.now(timezone.utc)
    if interval == "annual":
        return start + timedelta(days=365)
    return start + timedelta(days=31)  # légèrement généreux, le prochain paiement Mollie corrige de toute façon


def _notify_payment_failed(workspace_id, status):
    """Prévient le(s) administrateur(s) de l'espace par e-mail système. Ne
    lève jamais d'exception : un souci d'envoi ne doit jamais faire échouer
    le traitement du webhook (même principe que lifecycle.py)."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM workspaces WHERE id = %s", (workspace_id,))
            row = cur.fetchone()
            workspace_name = row[0] if row else "votre espace"

            cur.execute(
                "SELECT email FROM users WHERE workspace_id = %s AND role = 'admin' AND is_active",
                (workspace_id,),
            )
            admin_emails = [r[0] for r in cur.fetchall()]
    finally:
        conn.close()

    body = PAYMENT_FAILED_BODY_TEMPLATE.format(
        workspace_name=workspace_name,
        status=status,
        app_base_url=_app_base_url(),
    )
    for email in admin_emails:
        try:
            system_mail.send_system_email(email, PAYMENT_FAILED_SUBJECT, body)
        except system_mail.SystemMailError:
            pass  # ne bloque jamais le traitement du webhook pour un souci d'envoi ponctuel


def send_card_expiring_reminders():
    """Carte bancaire expirant bientôt — donnée disponible côté Mollie (API
    Mandats, champ details.cardExpiryDate au format YYYY-MM-DD pour les
    mandats carte) mais jamais exploitée jusqu'ici (voir contexte v5,
    section 4, point 2). Appelée une fois par jour depuis
    lifecycle.run_daily_maintenance (déjà limité à une fois/jour) : un seul
    appel Mollie par espace payant actif, pas à chaque passage du
    planificateur toutes les 30s.

    Ne fait jamais planter le job appelant : un souci ponctuel avec l'API
    Mollie pour un espace ne doit pas empêcher de vérifier les autres."""
    if not system_mail.is_configured():
        return
    try:
        _api_key()
    except MollieError:
        return  # MOLLIE_API_KEY pas configurée sur cet environnement : rien à vérifier

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, mollie_customer_id, card_expiry_reminder_sent_for
                FROM workspaces
                WHERE plan = 'paid' AND mollie_subscription_status = 'active'
                  AND mollie_customer_id IS NOT NULL
                """
            )
            candidates = cur.fetchall()
    finally:
        conn.close()

    cutoff = (datetime.now(timezone.utc) + timedelta(days=CARD_EXPIRY_WARNING_DAYS)).date()

    for workspace_id, name, customer_id, already_sent_for in candidates:
        try:
            mandates = _request("GET", f"/customers/{customer_id}/mandates")
        except MollieError:
            continue

        for mandate in mandates.get("_embedded", {}).get("mandates", []):
            if mandate.get("method") != "creditcard" or mandate.get("status") != "valid":
                continue
            expiry_str = ((mandate.get("details") or {}).get("cardExpiryDate") or "")
            if not expiry_str:
                continue
            try:
                expiry_date = datetime.strptime(expiry_str, "%Y-%m-%d").date()
            except ValueError:
                continue
            if expiry_date > cutoff:
                continue
            if already_sent_for and already_sent_for.isoformat() == expiry_str:
                continue  # déjà prévenu pour cette carte précise (même date d'expiration)

            _notify_card_expiring(workspace_id, name, expiry_date)
            break  # un seul mandat carte actif par espace dans notre modèle


def _notify_card_expiring(workspace_id, workspace_name, expiry_date):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT email FROM users WHERE workspace_id = %s AND role = 'admin' AND is_active",
                (workspace_id,),
            )
            admin_emails = [r[0] for r in cur.fetchall()]
    finally:
        conn.close()

    body = CARD_EXPIRING_BODY_TEMPLATE.format(
        workspace_name=workspace_name,
        expiry_month_label=expiry_date.strftime("%m/%Y"),
        app_base_url=_app_base_url(),
    )
    for email in admin_emails:
        try:
            system_mail.send_system_email(email, CARD_EXPIRING_SUBJECT, body)
        except system_mail.SystemMailError:
            pass

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE workspaces SET card_expiry_reminder_sent_for = %s WHERE id = %s",
                (expiry_date, workspace_id),
            )
        conn.commit()
    finally:
        conn.close()


def handle_webhook_payment(payment_id):
    """Appelé depuis la route /webhook/mollie. Ne fait jamais confiance au
    contenu de la requête entrante : recharge systématiquement le paiement
    depuis l'API Mollie avant d'agir."""
    payment = _request("GET", f"/payments/{payment_id}")
    status = payment.get("status")
    metadata = payment.get("metadata") or {}
    workspace_id = metadata.get("workspace_id")
    customer_id = payment.get("customerId")
    sequence_type = payment.get("sequenceType")

    if not workspace_id and customer_id:
        # Paiement de renouvellement : pas de metadata directe, on retrouve
        # l'espace de travail via le customerId Mollie.
        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM workspaces WHERE mollie_customer_id = %s", (customer_id,))
                row = cur.fetchone()
        finally:
            conn.close()
        workspace_id = row[0] if row else None

    if not workspace_id:
        _log_event(None, payment_id, "payment_unmatched", "Aucun espace de travail retrouvé pour ce paiement.")
        return

    workspace_id = int(workspace_id)
    interval = metadata.get("interval")

    if status != "paid":
        _log_event(workspace_id, payment_id, f"payment_{status}")
        if status in ("failed", "expired"):
            # "expired" couvre le cas d'un prélèvement automatique resté sans
            # réponse (ex: 3D Secure non validé) — même situation concrète
            # pour l'utilisateur qu'un échec direct, donc même notification.
            _notify_payment_failed(workspace_id, status)
        return

    if sequence_type == "first":
        # Premier paiement réussi : on crée l'abonnement récurrent Mollie,
        # puis on active l'espace de travail.
        subscription_id = create_subscription(workspace_id, customer_id, interval)
        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE workspaces
                    SET plan = 'paid', paid_until = %s, billing_interval = %s,
                        mollie_subscription_id = %s, mollie_subscription_status = 'active'
                    WHERE id = %s
                    """,
                    (_period_end(interval), interval, subscription_id, workspace_id),
                )
            conn.commit()
        finally:
            conn.close()
        _log_event(workspace_id, payment_id, "first_payment_paid", "Abonnement créé, espace activé.")
    else:
        # Paiement de renouvellement : on prolonge simplement l'accès.
        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE workspaces
                    SET paid_until = %s, plan = 'paid', mollie_subscription_status = 'active'
                    WHERE id = %s
                    """,
                    (_period_end(interval or "monthly"), workspace_id),
                )
            conn.commit()
        finally:
            conn.close()
        _log_event(workspace_id, payment_id, "renewal_payment_paid")
