"""
Ces tests couvrent les 3 e-mails automatiques ajoutés à lifecycle.py
(contexte v5, section 4, point 2). system_mail.send_system_email est
simulée (monkeypatch) pour ne dépendre d'aucun SMTP réel — ce qui est
vérifié ici, c'est la logique métier : fenêtre de déclenchement et
déduplication, sur une vraie base Postgres.
"""
from datetime import datetime, timedelta, timezone

from app import lifecycle, system_mail


def _set_workspace_fields(db_conn, workspace_id, **fields):
    columns = ", ".join(f"{k} = %s" for k in fields)
    with db_conn.cursor() as cur:
        cur.execute(f"UPDATE workspaces SET {columns} WHERE id = %s", (*fields.values(), workspace_id))
    db_conn.commit()


def _force_smtp_configured(monkeypatch):
    monkeypatch.setattr(system_mail, "is_configured", lambda: True)


def _capture_sent_emails(monkeypatch):
    sent = []

    def fake_send(to_email, subject, body, reply_to=None):
        sent.append({"to": to_email, "subject": subject, "body": body})
        return True

    monkeypatch.setattr(system_mail, "send_system_email", fake_send)
    monkeypatch.setattr(lifecycle.system_mail, "send_system_email", fake_send)
    return sent


def test_rappel_j2_fin_essai_envoye_et_dedoublonne(workspace_and_admin, db_conn, monkeypatch):
    _force_smtp_configured(monkeypatch)
    sent = _capture_sent_emails(monkeypatch)
    workspace_id = workspace_and_admin["workspace_id"]

    trial_ends_at = datetime.now(timezone.utc) + timedelta(days=1, hours=12)
    _set_workspace_fields(db_conn, workspace_id, trial_ends_at=trial_ends_at)

    lifecycle.send_trial_ending_reminders()
    assert len(sent) == 1
    assert "2 jours" in sent[0]["subject"]

    with db_conn.cursor() as cur:
        cur.execute("SELECT trial_ending_reminder_sent_at FROM workspaces WHERE id = %s", (workspace_id,))
        assert cur.fetchone()[0] is not None

    # Deuxième passage (ex: prochain tour du planificateur) : pas de doublon
    lifecycle.send_trial_ending_reminders()
    assert len(sent) == 1


def test_rappel_j2_fin_essai_hors_fenetre_ignore(workspace_and_admin, db_conn, monkeypatch):
    _force_smtp_configured(monkeypatch)
    sent = _capture_sent_emails(monkeypatch)
    workspace_id = workspace_and_admin["workspace_id"]

    # Essai qui se termine dans 10 jours : bien trop tôt pour le rappel J-2
    _set_workspace_fields(
        db_conn, workspace_id, trial_ends_at=datetime.now(timezone.utc) + timedelta(days=10)
    )
    lifecycle.send_trial_ending_reminders()
    assert sent == []


def test_rappel_renouvellement_annuel_envoye_et_dedoublonne(workspace_and_admin, db_conn, monkeypatch):
    _force_smtp_configured(monkeypatch)
    sent = _capture_sent_emails(monkeypatch)
    workspace_id = workspace_and_admin["workspace_id"]

    paid_until = datetime.now(timezone.utc) + timedelta(days=1)
    _set_workspace_fields(
        db_conn, workspace_id,
        plan="paid", billing_interval="annual", mollie_subscription_status="active",
        paid_until=paid_until,
    )

    lifecycle.send_annual_renewal_reminders()
    assert len(sent) == 1
    assert "renouvelle" in sent[0]["subject"]

    # Même paid_until -> pas de second envoi
    lifecycle.send_annual_renewal_reminders()
    assert len(sent) == 1

    # Renouvellement effectif (nouvelle paid_until, distincte de la
    # précédente mais toujours dans la fenêtre J-2) -> se réarme
    # automatiquement, comme prévu par la conception (comparaison à
    # paid_until plutôt qu'un simple booléen).
    new_paid_until = datetime.now(timezone.utc) + timedelta(days=1, hours=1)
    _set_workspace_fields(db_conn, workspace_id, paid_until=new_paid_until)
    lifecycle.send_annual_renewal_reminders()
    assert len(sent) == 2


def test_rappel_renouvellement_ignore_pour_abonnement_mensuel(workspace_and_admin, db_conn, monkeypatch):
    _force_smtp_configured(monkeypatch)
    sent = _capture_sent_emails(monkeypatch)
    workspace_id = workspace_and_admin["workspace_id"]

    _set_workspace_fields(
        db_conn, workspace_id,
        plan="paid", billing_interval="monthly", mollie_subscription_status="active",
        paid_until=datetime.now(timezone.utc) + timedelta(days=1),
    )
    lifecycle.send_annual_renewal_reminders()
    assert sent == []  # ce rappel ne concerne volontairement que l'annuel


def test_relance_j7_apres_essai_non_converti(workspace_and_admin, db_conn, monkeypatch):
    _force_smtp_configured(monkeypatch)
    sent = _capture_sent_emails(monkeypatch)
    workspace_id = workspace_and_admin["workspace_id"]

    # Essai terminé il y a 7 jours et demi, jamais converti (plan toujours 'trial')
    trial_ends_at = datetime.now(timezone.utc) - timedelta(days=7, hours=12)
    _set_workspace_fields(db_conn, workspace_id, trial_ends_at=trial_ends_at)

    lifecycle.send_free_downgrade_followups()
    assert len(sent) == 1
    assert "ClickProspect" in sent[0]["subject"]

    lifecycle.send_free_downgrade_followups()
    assert len(sent) == 1  # pas de doublon


def test_relance_j7_ignoree_si_essai_converti_en_payant(workspace_and_admin, db_conn, monkeypatch):
    _force_smtp_configured(monkeypatch)
    sent = _capture_sent_emails(monkeypatch)
    workspace_id = workspace_and_admin["workspace_id"]

    # L'essai s'est terminé il y a 7 jours mais l'espace est maintenant payant
    # (conversion réussie) : plan n'est plus 'trial', donc hors périmètre.
    _set_workspace_fields(
        db_conn, workspace_id,
        plan="paid",
        trial_ends_at=datetime.now(timezone.utc) - timedelta(days=7, hours=12),
        paid_until=datetime.now(timezone.utc) + timedelta(days=358),
    )
    lifecycle.send_free_downgrade_followups()
    assert sent == []
