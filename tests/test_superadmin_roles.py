"""
Vérifie que la distinction de rôle administrateur/technicien introduite
pour /supadmin est réellement appliquée côté serveur — pas seulement
masquée côté interface. C'est le point le plus sensible de ce chantier :
un test qui passe à tort ici serait une vraie faille de sécurité.
"""
import pytest

from app import superadmin


@pytest.fixture()
def admin_account(db_conn):
    admin_id = superadmin.create_superadmin("admin-test@test.local", "MotDePasseAdmin123!", "administrateur")
    yield admin_id
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM superadmin_audit_log WHERE superadmin_id = %s", (admin_id,))
        cur.execute("DELETE FROM superadmins WHERE id = %s", (admin_id,))
    db_conn.commit()


@pytest.fixture()
def technicien_account(db_conn):
    tech_id = superadmin.create_superadmin("technicien-test@test.local", "MotDePasseTech123!", "technicien")
    yield tech_id
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM superadmin_audit_log WHERE superadmin_id = %s", (tech_id,))
        cur.execute("DELETE FROM superadmins WHERE id = %s", (tech_id,))
    db_conn.commit()


def _login_as(client, email, password):
    with client.session_transaction() as sess:
        pass  # session réelle posée via l'appel de connexion normal
    resp = client.post("/api/supadmin/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.get_data(as_text=True)


def test_creation_role_invalide_refusee(admin_account):
    with pytest.raises(superadmin.SuperadminError):
        superadmin.create_superadmin("x@test.local", "MotDePasseTest123!", "super-mega-admin")


def test_mot_de_passe_trop_court_refuse(admin_account):
    with pytest.raises(superadmin.SuperadminError):
        superadmin.create_superadmin("x@test.local", "court", "technicien")


def test_technicien_accede_aux_routes_de_consultation(client, technicien_account):
    _login_as(client, "technicien-test@test.local", "MotDePasseTech123!")
    resp = client.get("/api/supadmin/workspaces")
    assert resp.status_code == 200
    resp = client.get("/api/supadmin/accounts")
    assert resp.status_code == 200


@pytest.mark.parametrize("method,path,body", [
    ("GET", "/api/supadmin/maintenance", None),
    ("POST", "/api/supadmin/workspaces/1/login-as", None),
    ("DELETE", "/api/supadmin/workspaces/1", None),
    ("POST", "/api/supadmin/workspaces/1/dismiss-deletion", None),
    ("PUT", "/api/supadmin/workspaces/1/plan", {"plan": "paid"}),
    ("PUT", "/api/supadmin/workspaces/1/ia-quota", {"quota": 10}),
    ("POST", "/api/supadmin/accounts", {"email": "y@test.local", "password": "xxxxxxxx", "role": "administrateur"}),
    ("PUT", "/api/supadmin/vps/thresholds", {"vps_alert_disk_pct": 50}),
])
def test_technicien_bloque_sur_les_actions_reservees(client, technicien_account, method, path, body):
    _login_as(client, "technicien-test@test.local", "MotDePasseTech123!")
    resp = client.open(path, method=method, json=body)
    assert resp.status_code == 403, f"{method} {path} aurait dû être bloqué pour un technicien"


def test_administrateur_accede_aux_actions_reservees(client, admin_account):
    _login_as(client, "admin-test@test.local", "MotDePasseAdmin123!")
    resp = client.get("/api/supadmin/maintenance")
    assert resp.status_code == 200


def test_route_non_authentifiee_rejetee(client):
    resp = client.get("/api/supadmin/workspaces")
    assert resp.status_code == 401


def test_compte_desactive_ne_peut_plus_se_connecter(app, client, technicien_account):
    with app.test_request_context():
        superadmin.set_superadmin_active(technicien_account, False)
    resp = client.post(
        "/api/supadmin/login",
        json={"email": "technicien-test@test.local", "password": "MotDePasseTech123!"},
    )
    assert resp.status_code == 401


def test_on_ne_peut_pas_se_desactiver_soi_meme(app, admin_account):
    with app.test_request_context():
        from flask import session
        session["superadmin_id"] = admin_account
        with pytest.raises(superadmin.SuperadminError):
            superadmin.set_superadmin_active(admin_account, False)
