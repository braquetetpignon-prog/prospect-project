"""
send_card_expiring_reminders() appelle l'API Mollie réelle (_request) — on
la simule ici pour ne dépendre d'aucune clé Mollie ni d'appel réseau réel,
tout en vérifiant la vraie logique de fenêtre et de déduplication sur la
base Postgres locale.
"""
from datetime import datetime, timedelta, timezone

from app import mollie_billing, system_mail


def _set_workspace_fields(db_conn, workspace_id, **fields):
    columns = ", ".join(f"{k} = %s" for k in fields)
    with db_conn.cursor() as cur:
        cur.execute(f"UPDATE workspaces SET {columns} WHERE id = %s", (*fields.values(), workspace_id))
    db_conn.commit()


def _capture_sent_emails(monkeypatch):
    sent = []

    def fake_send(to_email, subject, body, reply_to=None):
        sent.append({"to": to_email, "subject": subject, "body": body})
        return True

    monkeypatch.setattr(mollie_billing.system_mail, "send_system_email", fake_send)
    return sent


def _mock_mandates_response(monkeypatch, expiry_date_str):
    def fake_request(method, path, **kwargs):
        assert "/mandates" in path
        return {
            "_embedded": {
                "mandates": [
                    {
                        "method": "creditcard",
                        "status": "valid",
                        "details": {"cardExpiryDate": expiry_date_str},
                    }
                ]
            }
        }

    monkeypatch.setattr(mollie_billing, "_request", fake_request)
    monkeypatch.setattr(mollie_billing, "_api_key", lambda: "test_dummy_key")


def test_carte_expirant_bientot_envoyee_et_dedoublonnee(workspace_and_admin, db_conn, monkeypatch):
    monkeypatch.setattr(system_mail, "is_configured", lambda: True)
    sent = _capture_sent_emails(monkeypatch)
    workspace_id = workspace_and_admin["workspace_id"]

    expiry = (datetime.now(timezone.utc) + timedelta(days=10)).date()
    _mock_mandates_response(monkeypatch, expiry.isoformat())
    _set_workspace_fields(
        db_conn, workspace_id,
        plan="paid", mollie_subscription_status="active", mollie_customer_id="cst_test123",
    )

    mollie_billing.send_card_expiring_reminders()
    assert len(sent) == 1
    assert "expire" in sent[0]["subject"]

    with db_conn.cursor() as cur:
        cur.execute("SELECT card_expiry_reminder_sent_for FROM workspaces WHERE id = %s", (workspace_id,))
        assert cur.fetchone()[0] == expiry

    # Deuxième passage, même carte -> pas de doublon
    mollie_billing.send_card_expiring_reminders()
    assert len(sent) == 1


def test_carte_expirant_loin_ignoree(workspace_and_admin, db_conn, monkeypatch):
    monkeypatch.setattr(system_mail, "is_configured", lambda: True)
    sent = _capture_sent_emails(monkeypatch)
    workspace_id = workspace_and_admin["workspace_id"]

    # Carte qui expire dans 200 jours : bien au-delà de la fenêtre d'alerte
    far_expiry = (datetime.now(timezone.utc) + timedelta(days=200)).date()
    _mock_mandates_response(monkeypatch, far_expiry.isoformat())
    _set_workspace_fields(
        db_conn, workspace_id,
        plan="paid", mollie_subscription_status="active", mollie_customer_id="cst_test456",
    )

    mollie_billing.send_card_expiring_reminders()
    assert sent == []


def test_carte_expiration_changee_reautorise_un_envoi(workspace_and_admin, db_conn, monkeypatch):
    monkeypatch.setattr(system_mail, "is_configured", lambda: True)
    sent = _capture_sent_emails(monkeypatch)
    workspace_id = workspace_and_admin["workspace_id"]

    first_expiry = (datetime.now(timezone.utc) + timedelta(days=5)).date()
    _mock_mandates_response(monkeypatch, first_expiry.isoformat())
    _set_workspace_fields(
        db_conn, workspace_id,
        plan="paid", mollie_subscription_status="active", mollie_customer_id="cst_test789",
    )
    mollie_billing.send_card_expiring_reminders()
    assert len(sent) == 1

    # Le client met à jour sa carte : nouvelle date d'expiration -> nouvelle
    # alerte légitime, ce n'est plus la même carte.
    new_expiry = (datetime.now(timezone.utc) + timedelta(days=6)).date()
    _mock_mandates_response(monkeypatch, new_expiry.isoformat())
    mollie_billing.send_card_expiring_reminders()
    assert len(sent) == 2

