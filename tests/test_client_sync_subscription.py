"""
Couvre l'ajout du statut d'abonnement (+ date de fin) à la synchro
Gestion Client — pour permettre un export CSV filtrable facilement en vue
d'une facturation manuelle (MEG).
"""
from datetime import datetime, timedelta, timezone

import pytest

from app import auth, client_sync, prospects, superadmin


@pytest.fixture()
def target_workspace(db_conn):
    workspace_id, admin_id = auth.create_workspace_with_admin(
        "Espace CRM Sub Test", f"crm-target-sub-{datetime.now(timezone.utc).timestamp()}@test.local",
        "MotDePasseTest123!",
    )
    client_sync.set_crm_target_workspace_id(workspace_id)
    yield workspace_id
    client_sync.set_crm_target_workspace_id(None)
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM prospects WHERE workspace_id = %s", (workspace_id,))
        cur.execute("DELETE FROM users WHERE workspace_id = %s", (workspace_id,))
        cur.execute("DELETE FROM workspaces WHERE id = %s", (workspace_id,))
    db_conn.commit()


@pytest.fixture()
def client_workspace(db_conn):
    workspace_id, admin_id = auth.create_workspace_with_admin(
        "Plomberie Sub Test SARL", f"client-sub-{datetime.now(timezone.utc).timestamp()}@test.local",
        "MotDePasseTest123!",
    )
    yield {"workspace_id": workspace_id, "admin_id": admin_id}
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM prospects WHERE workspace_id = %s", (workspace_id,))
        cur.execute("DELETE FROM users WHERE workspace_id = %s", (workspace_id,))
        cur.execute("DELETE FROM workspaces WHERE id = %s", (workspace_id,))
    db_conn.commit()


def _get_synced(db_conn, target_workspace_id, source_workspace_id):
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT synced_subscription_status, synced_subscription_end_date FROM prospects "
            "WHERE workspace_id = %s AND synced_from_workspace_id = %s",
            (target_workspace_id, source_workspace_id),
        )
        return cur.fetchone()


def test_passage_payant_cree_la_fiche_avec_statut_et_date(target_workspace, client_workspace, db_conn):
    end_date = datetime.now(timezone.utc) + timedelta(days=30)
    superadmin.set_plan(client_workspace["workspace_id"], "paid", paid_until=end_date)

    status, date = _get_synced(db_conn, target_workspace, client_workspace["workspace_id"])
    assert status == "Payant mensuel"
    assert date == end_date.date()


def test_renouvellement_met_a_jour_la_date_sans_doublon(target_workspace, client_workspace, db_conn):
    superadmin.set_plan(client_workspace["workspace_id"], "paid", paid_until=datetime.now(timezone.utc) + timedelta(days=30))
    superadmin.set_plan(client_workspace["workspace_id"], "paid", paid_until=datetime.now(timezone.utc) + timedelta(days=60))

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM prospects WHERE workspace_id = %s AND synced_from_workspace_id = %s",
            (target_workspace, client_workspace["workspace_id"]),
        )
        count = cur.fetchone()[0]
    assert count == 1

    status, date = _get_synced(db_conn, target_workspace, client_workspace["workspace_id"])
    assert date == (datetime.now(timezone.utc) + timedelta(days=60)).date()


def test_passage_gratuit_efface_le_statut_payant(target_workspace, client_workspace, db_conn):
    superadmin.set_plan(client_workspace["workspace_id"], "paid", paid_until=datetime.now(timezone.utc) + timedelta(days=30))
    superadmin.set_plan(client_workspace["workspace_id"], "free")

    status, date = _get_synced(db_conn, target_workspace, client_workspace["workspace_id"])
    assert status == "Gratuit"
    assert date is None


def test_essai_expire_affiche_gratuit_pas_essai(target_workspace, client_workspace, db_conn):
    """Le statut doit refléter la réalité (subscriptions.effective_plan), pas
    la seule colonne `plan` qui peut rester à 'trial' indéfiniment après
    expiration si personne ne l'a mise à jour."""
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE workspaces SET plan = 'trial', trial_ends_at = %s WHERE id = %s",
            (datetime.now(timezone.utc) - timedelta(days=1), client_workspace["workspace_id"]),
        )
    db_conn.commit()

    client_sync.sync_subscription_status(client_workspace["workspace_id"])

    status, date = _get_synced(db_conn, target_workspace, client_workspace["workspace_id"])
    assert status == "Gratuit"


def test_export_csv_contient_abonnement_et_date(target_workspace, client_workspace, db_conn):
    superadmin.set_plan(client_workspace["workspace_id"], "paid", paid_until=datetime.now(timezone.utc) + timedelta(days=30))

    csv_text = prospects.export_csv(target_workspace, statut="client")
    assert "Abonnement" in csv_text
    assert "Fin abonnement" in csv_text
    assert "Payant mensuel" in csv_text


def test_abonnement_annuel_libelle_correct(target_workspace, client_workspace, db_conn):
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE workspaces SET plan = 'paid', paid_until = %s, billing_interval = 'annual' WHERE id = %s",
            (datetime.now(timezone.utc) + timedelta(days=365), client_workspace["workspace_id"]),
        )
    db_conn.commit()

    client_sync.sync_subscription_status(client_workspace["workspace_id"])

    status, _ = _get_synced(db_conn, target_workspace, client_workspace["workspace_id"])
    assert status == "Payant annuel"
