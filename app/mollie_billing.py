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

MOLLIE_API_BASE = "https://api.mollie.com/v2"

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
