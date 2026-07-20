"""
Vérifie que /version expose bien le commit réellement construit dans le
conteneur — c'est le point qui a manqué lors de l'incident où la prod
servait encore l'ancienne règle CSS `.modal` malgré un merge visible sur
GitHub (contexte v5, section 3).
"""
import os


def test_version_expose_commit_et_env(client):
    resp = client.get("/version")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["git_commit"] == os.environ["GIT_COMMIT"]
    assert data["env"] == "local"


def test_health_verifie_la_base(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "healthy", "db": "ok"}


def test_version_accessible_meme_en_maintenance(client, monkeypatch):
    """/version doit rester lisible même si le mode maintenance est activé,
    sinon impossible de vérifier un déploiement pendant une maintenance."""
    from app import superadmin

    monkeypatch.setattr(superadmin, "is_maintenance_mode", lambda: True)
    resp = client.get("/version")
    assert resp.status_code == 200
