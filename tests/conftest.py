"""
Configuration pytest pour la suite de non-régression.

Contexte : voir contexte-reprise-clickprospect-v5.md, chantier "tests
automatisés + vérification post-déploiement" — ces tests couvrent en
priorité les chemins qui ont déjà cassé silencieusement en prod (import
CSV qui ne loguait rien, panneau Automatisations planté par une variable
JS non définie détecté seulement en test manuel, etc.).

Prérequis local : PostgreSQL tournant sur localhost avec un rôle
`clickprospect` / base `clickprospect` (voir docker-compose.yml — mêmes
identifiants qu'en local). Ne touche jamais à une base de préprod/prod.
"""
import os
import uuid

import psycopg2
import pytest

os.environ.setdefault("ENV", "local")
os.environ.setdefault("DATABASE_URL", "postgresql://clickprospect:changeme@localhost:5432/clickprospect")
os.environ.setdefault("SECRET_KEY", "test-secret-key-do-not-use-in-prod")
os.environ.setdefault("GIT_COMMIT", "test")

from app.main import app as flask_app  # noqa: E402  (après réglage des env vars)
from app import auth  # noqa: E402


@pytest.fixture(scope="session")
def app():
    flask_app.config.update(TESTING=True)
    return flask_app


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def db_conn():
    """Connexion brute pour vérifier directement l'état en base après un
    appel — c'est ce niveau de vérification qui a manqué pour repérer que
    l'import CSV ne loguait aucune activité."""
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    yield conn
    conn.close()


@pytest.fixture()
def workspace_and_admin():
    """Crée un espace de travail + admin isolés (nom/e-mail uniques par
    test) et nettoie tout ce qui a été créé, prospects et activité inclus,
    à la fin du test — la base locale reste réutilisable d'un test à
    l'autre sans dépendre de l'ordre d'exécution."""
    suffix = uuid.uuid4().hex[:8]
    workspace_name = f"Test Workspace {suffix}"
    admin_email = f"admin-{suffix}@test.clickprospect.local"
    workspace_id, admin_id = auth.create_workspace_with_admin(
        workspace_name, admin_email, "MotDePasseTest123!"
    )
    yield {"workspace_id": workspace_id, "admin_id": admin_id, "admin_email": admin_email}

    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM prospect_activity WHERE workspace_id = %s", (workspace_id,))
            cur.execute("DELETE FROM import_jobs WHERE workspace_id = %s", (workspace_id,))
            cur.execute("DELETE FROM prospects WHERE workspace_id = %s", (workspace_id,))
            cur.execute("DELETE FROM users WHERE workspace_id = %s", (workspace_id,))
            cur.execute("DELETE FROM workspaces WHERE id = %s", (workspace_id,))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def logged_in_client(client, workspace_and_admin):
    """Client de test avec une session déjà authentifiée comme l'admin du
    workspace créé par la fixture ci-dessus."""
    with client.session_transaction() as sess:
        sess["user_id"] = workspace_and_admin["admin_id"]
        sess["workspace_id"] = workspace_and_admin["workspace_id"]
        sess["role"] = "admin"
    return client
