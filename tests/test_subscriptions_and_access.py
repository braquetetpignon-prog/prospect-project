"""
Le statut d'abonnement effectif conditionne l'accès à Pipeline et au
Rapport d'équipe (contexte v5, chantiers 3 et 4 — "réservé hors Gratuit").
Une régression ici serait soit une fuite de fonctionnalité payante, soit
un blocage indu pour un client qui paie : les deux coûtent cher, donc ce
sont des cas à couvrir avant tout autre chantier.
"""
from datetime import datetime, timedelta, timezone

from app import subscriptions


def test_essai_en_cours_n_est_pas_restreint():
    trial_ends_at = datetime.now(timezone.utc) + timedelta(days=3)
    assert subscriptions.effective_plan("trial", trial_ends_at, None) == "trial"


def test_essai_expire_retombe_en_gratuit_meme_si_colonne_pas_a_jour():
    trial_ends_at = datetime.now(timezone.utc) - timedelta(days=1)
    assert subscriptions.effective_plan("trial", trial_ends_at, None) == "free"


def test_payant_expire_retombe_en_gratuit():
    paid_until = datetime.now(timezone.utc) - timedelta(days=1)
    assert subscriptions.effective_plan("paid", None, paid_until) == "free"


def test_payant_actif_n_est_pas_restreint():
    paid_until = datetime.now(timezone.utc) + timedelta(days=20)
    assert subscriptions.effective_plan("paid", None, paid_until) == "paid"


def test_rapport_equipe_accessible_a_l_admin_en_essai(logged_in_client):
    resp = logged_in_client.get("/rapports-equipe")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Réservé aux administrateurs" not in body
    assert "fait partie de l'offre Premium" not in body
    assert 'id="team-report-body"' in body


def test_rapport_equipe_bloque_pour_un_non_admin(client, workspace_and_admin):
    from app import auth

    commercial_id = auth.create_user(
        workspace_and_admin["workspace_id"], "commercial-test@test.clickprospect.local",
        "MotDePasseTest123!", "commercial",
    )
    with client.session_transaction() as sess:
        sess["user_id"] = commercial_id
        sess["workspace_id"] = workspace_and_admin["workspace_id"]
        sess["role"] = "commercial"

    resp = client.get("/rapports-equipe")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Réservé aux administrateurs" in body
    assert 'id="team-report-body"' not in body
