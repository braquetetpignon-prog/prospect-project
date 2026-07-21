"""
Deux choses couvertes ici :
1. Régression corrigée : list_feedback() avait disparu (avalée par erreur
   dans change_own_password lors d'un précédent chantier) — /api/supadmin/
   feedback renvoyait 500 et la rubrique Suggestions restait vide.
2. Le PIN de confirmation pour le changement de mot de passe superadmin :
   tant qu'aucun PIN n'est configuré, le changement reste possible avec
   seulement le mot de passe actuel ; une fois configuré, le PIN devient
   obligatoire — c'est ce qui protège contre un tiers ayant récupéré la
   session (ordinateur laissé ouvert) mais ignorant le PIN.
"""
import pytest

from app import superadmin


@pytest.fixture()
def admin_account(db_conn):
    admin_id = superadmin.create_superadmin("admin-pin-test@test.local", "MotDePasseAdmin123!", "administrateur")
    yield admin_id
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM superadmin_audit_log WHERE superadmin_id = %s", (admin_id,))
        cur.execute("DELETE FROM superadmins WHERE id = %s", (admin_id,))
    db_conn.commit()


def _login_as(client, email, password):
    resp = client.post("/api/supadmin/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.get_data(as_text=True)


def test_list_feedback_ne_plante_plus(admin_account):
    """Régression : cette fonction avait disparu, cet appel suffit à
    vérifier qu'elle existe de nouveau et fonctionne (même sans donnée)."""
    entries = superadmin.list_feedback()
    assert entries == []


def test_route_feedback_repond_200(client, admin_account):
    _login_as(client, "admin-pin-test@test.local", "MotDePasseAdmin123!")
    resp = client.get("/api/supadmin/feedback")
    assert resp.status_code == 200
    assert resp.get_json() == {"entries": []}


def test_changement_mdp_reussi_ne_plante_plus(client, admin_account):
    """Régression : le chemin de succès de change_own_password référençait
    une variable `limit` inexistante (reste de la fonction avalée) —
    plantait uniquement quand le mot de passe actuel était CORRECT, donc
    invisible avec un test qui ne teste que le mauvais mot de passe."""
    _login_as(client, "admin-pin-test@test.local", "MotDePasseAdmin123!")
    resp = client.post("/api/supadmin/change-password", json={
        "current_password": "MotDePasseAdmin123!",
        "new_password": "NouveauMotDePasse456!",
    })
    assert resp.status_code == 200, resp.get_data(as_text=True)


def test_changement_mdp_sans_pin_configure_fonctionne(client, admin_account):
    _login_as(client, "admin-pin-test@test.local", "MotDePasseAdmin123!")
    resp = client.get("/api/supadmin/me")
    assert resp.get_json()["has_pin"] is False

    resp = client.post("/api/supadmin/change-password", json={
        "current_password": "MotDePasseAdmin123!",
        "new_password": "NouveauMotDePasse456!",
    })
    assert resp.status_code == 200


def test_configurer_pin_faible_refuse(client, admin_account):
    _login_as(client, "admin-pin-test@test.local", "MotDePasseAdmin123!")
    resp = client.put("/api/supadmin/pin", json={
        "current_password": "MotDePasseAdmin123!", "pin": "111111",
    })
    assert resp.status_code == 400
    resp = client.put("/api/supadmin/pin", json={
        "current_password": "MotDePasseAdmin123!", "pin": "123456",
    })
    assert resp.status_code == 400
    resp = client.put("/api/supadmin/pin", json={
        "current_password": "MotDePasseAdmin123!", "pin": "42",
    })
    assert resp.status_code == 400


def test_configurer_pin_exige_le_bon_mot_de_passe(client, admin_account):
    _login_as(client, "admin-pin-test@test.local", "MotDePasseAdmin123!")
    resp = client.put("/api/supadmin/pin", json={
        "current_password": "MauvaisMotDePasse", "pin": "482917",
    })
    assert resp.status_code == 400


def test_pin_devient_obligatoire_une_fois_configure(client, admin_account):
    _login_as(client, "admin-pin-test@test.local", "MotDePasseAdmin123!")
    resp = client.put("/api/supadmin/pin", json={
        "current_password": "MotDePasseAdmin123!", "pin": "482917",
    })
    assert resp.status_code == 200

    resp = client.get("/api/supadmin/me")
    assert resp.get_json()["has_pin"] is True

    # Sans PIN du tout -> refusé
    resp = client.post("/api/supadmin/change-password", json={
        "current_password": "MotDePasseAdmin123!", "new_password": "AutreMotDePasse789!",
    })
    assert resp.status_code == 400
    assert "PIN" in resp.get_json()["error"]

    # Mauvais PIN -> refusé
    resp = client.post("/api/supadmin/change-password", json={
        "current_password": "MotDePasseAdmin123!", "new_password": "AutreMotDePasse789!", "pin": "000000",
    })
    assert resp.status_code == 400

    # Bon PIN -> accepté
    resp = client.post("/api/supadmin/change-password", json={
        "current_password": "MotDePasseAdmin123!", "new_password": "AutreMotDePasse789!", "pin": "482917",
    })
    assert resp.status_code == 200
