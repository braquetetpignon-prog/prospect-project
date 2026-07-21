"""
Couvre le chantier "Synchro Gestion Client" : les informations facultatives
de Mon compte (identité + entreprise) et leur remontée automatique vers
l'espace de travail CRM personnel du superadmin.

Points critiques testés : l'idempotence (pas de doublon à chaque
sauvegarde), la restriction des champs entreprise au rôle admin, et le
fait qu'un espace ne se synchronise jamais avec lui-même.
"""
from datetime import datetime, timezone

import pytest

from app import auth, client_sync


@pytest.fixture()
def target_workspace(db_conn):
    """L'espace CRM personnel du superadmin (ex: 'Supervision')."""
    workspace_id, admin_id = auth.create_workspace_with_admin(
        "Espace CRM Test", f"crm-target-{datetime.now(timezone.utc).timestamp()}@test.local",
        "MotDePasseTest123!",
    )
    client_sync.set_crm_target_workspace_id(workspace_id)
    yield workspace_id
    client_sync.set_crm_target_workspace_id(None)
    conn = db_conn
    with conn.cursor() as cur:
        cur.execute("DELETE FROM prospects WHERE workspace_id = %s", (workspace_id,))
        cur.execute("DELETE FROM users WHERE workspace_id = %s", (workspace_id,))
        cur.execute("DELETE FROM workspaces WHERE id = %s", (workspace_id,))
    conn.commit()


@pytest.fixture()
def client_workspace(db_conn):
    """Un espace de travail client type (celui d'un artisan)."""
    workspace_id, admin_id = auth.create_workspace_with_admin(
        "Plomberie Test SARL", f"client-admin-{datetime.now(timezone.utc).timestamp()}@test.local",
        "MotDePasseTest123!",
    )
    yield {"workspace_id": workspace_id, "admin_id": admin_id}
    conn = db_conn
    with conn.cursor() as cur:
        cur.execute("DELETE FROM prospects WHERE workspace_id = %s", (workspace_id,))
        cur.execute("DELETE FROM users WHERE workspace_id = %s", (workspace_id,))
        cur.execute("DELETE FROM workspaces WHERE id = %s", (workspace_id,))
    conn.commit()


def test_sync_cree_une_fiche_client(target_workspace, client_workspace, db_conn):
    auth.update_profile(
        client_workspace["admin_id"], client_workspace["workspace_id"], True,
        {"first_name": "Jean", "last_name": "Dupont", "phone": "0601020304",
         "company_name": "Plomberie Test SARL", "siret": "12345678900012",
         "adresse": "12 rue des Artisans", "code_postal": "17000", "ville": "La Rochelle"},
    )

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT nom_entreprise, contact_prenom, siret, statut, source, synced_from_workspace_id "
            "FROM prospects WHERE workspace_id = %s",
            (target_workspace,),
        )
        rows = cur.fetchall()

    assert len(rows) == 1
    row = rows[0]
    assert row[0] == "Plomberie Test SARL"
    assert row[1] == "Jean"
    assert row[2] == "12345678900012"
    assert row[3] == "client"
    assert row[4] == "sync_compte_client"
    assert row[5] == client_workspace["workspace_id"]


def test_sync_est_idempotente(target_workspace, client_workspace, db_conn):
    for phone in ("0601020304", "0699999999"):
        auth.update_profile(
            client_workspace["admin_id"], client_workspace["workspace_id"], True,
            {"first_name": "Jean", "phone": phone, "company_name": "Plomberie Test SARL"},
        )

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*), max(telephone) FROM prospects "
            "WHERE workspace_id = %s AND synced_from_workspace_id = %s",
            (target_workspace, client_workspace["workspace_id"]),
        )
        count, phone = cur.fetchone()

    assert count == 1
    assert phone == "0699999999"


def test_commercial_ne_peut_pas_changer_les_champs_entreprise(client_workspace, db_conn):
    commercial_id = auth.create_user(
        client_workspace["workspace_id"], "commercial-sync-test@test.local",
        "MotDePasseTest123!", "commercial",
    )
    auth.update_profile(
        commercial_id, client_workspace["workspace_id"], False,  # is_admin=False
        {"first_name": "Marie", "company_name": "ENTREPRISE PIRATEE", "siret": "99999999999999"},
    )

    with db_conn.cursor() as cur:
        cur.execute("SELECT name, siret FROM workspaces WHERE id = %s", (client_workspace["workspace_id"],))
        name, siret = cur.fetchone()
        cur.execute("SELECT first_name FROM users WHERE id = %s", (commercial_id,))
        first_name = cur.fetchone()[0]

    assert name == "Plomberie Test SARL"  # inchangé
    assert siret is None  # jamais défini, la tentative a été ignorée
    assert first_name == "Marie"  # le champ personnel, lui, est bien passé


def test_pas_de_sync_sans_cible_configuree(client_workspace, db_conn):
    client_sync.set_crm_target_workspace_id(None)
    auth.update_profile(
        client_workspace["admin_id"], client_workspace["workspace_id"], True,
        {"company_name": "Plomberie Test SARL", "siret": "12345678900012"},
    )
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM prospects WHERE synced_from_workspace_id = %s",
            (client_workspace["workspace_id"],),
        )
        count = cur.fetchone()[0]
    assert count == 0


def test_espace_ne_se_synchronise_pas_avec_lui_meme(target_workspace, db_conn):
    admin_id = None
    with db_conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE workspace_id = %s AND role = 'admin'", (target_workspace,))
        admin_id = cur.fetchone()[0]

    auth.update_profile(admin_id, target_workspace, True, {"company_name": "Espace CRM Test"})

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM prospects WHERE workspace_id = %s AND synced_from_workspace_id = %s",
            (target_workspace, target_workspace),
        )
        count = cur.fetchone()[0]
    assert count == 0


def test_routes_crm_sync_reservees_administrateur(client, db_conn):
    from app import superadmin
    tech_id = superadmin.create_superadmin("tech-crm-test@test.local", "MotDePasseTech123!", "technicien")
    try:
        resp = client.post("/api/supadmin/login", json={
            "email": "tech-crm-test@test.local", "password": "MotDePasseTech123!",
        })
        assert resp.status_code == 200

        resp = client.get("/api/supadmin/crm-sync")
        assert resp.status_code == 403

        resp = client.put("/api/supadmin/crm-sync", json={"target_workspace_id": 1})
        assert resp.status_code == 403

        resp = client.post("/api/supadmin/crm-sync/run")
        assert resp.status_code == 403
    finally:
        with db_conn.cursor() as cur:
            cur.execute("DELETE FROM superadmin_audit_log WHERE superadmin_id = %s", (tech_id,))
            cur.execute("DELETE FROM superadmins WHERE id = %s", (tech_id,))
        db_conn.commit()
