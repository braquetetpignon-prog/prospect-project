"""
Ces tests protègent contre la régression découverte pendant le chantier
"Rapport d'équipe" (contexte v5, section 3) : l'import CSV et la recherche
IA — la source probable de la majorité des prospects — n'enregistraient
AUCUN événement d'activité, ni pour l'utilisateur ni pour la fiche
elle-même. Si un futur changement recasse ce log, un test doit le voir
avant qu'il ne faille attendre un chantier ultérieur pour le remarquer.
"""
import io
import json

from app import activity, csv_import, prospects


def _count_activity(db_conn, workspace_id, event_type="cree"):
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM prospect_activity WHERE workspace_id = %s AND event_type = %s",
            (workspace_id, event_type),
        )
        return cur.fetchone()[0]


def test_creation_manuelle_logue_activite_avec_user_id(workspace_and_admin, db_conn):
    workspace_id = workspace_and_admin["workspace_id"]
    admin_id = workspace_and_admin["admin_id"]

    prospect_id, _ = prospects.create_prospect(
        workspace_id,
        {"nom_entreprise": "Plomberie Test SARL", "ville": "Niort"},
        source="manuel",
        user_id=admin_id,
    )

    events = activity.list_activity(prospect_id, workspace_id)
    assert len(events) == 1
    assert events[0]["event_type"] == "cree"

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT user_id FROM prospect_activity WHERE prospect_id = %s", (prospect_id,)
        )
        assert cur.fetchone()[0] == admin_id


def test_import_csv_logue_activite_pour_chaque_prospect(workspace_and_admin, db_conn):
    """C'est précisément le cas qui ne fonctionnait pas avant le chantier 4 :
    _insert_prospect ne loguait rien du tout."""
    workspace_id = workspace_and_admin["workspace_id"]
    admin_id = workspace_and_admin["admin_id"]

    csv_content = (
        "nom_entreprise,ville,email\n"
        "Electricite Dupont,Rochefort,contact@dupont-elec.fr\n"
        "Menuiserie Martin,La Rochelle,contact@martin-menuiserie.fr\n"
    ).encode("utf-8")

    job_id = csv_import.create_import_job(
        workspace_id, "test_import.csv", csv_content,
        header=["nom_entreprise", "ville", "email"], total_rows=2, user_id=admin_id,
    )
    mapping = {"nom_entreprise": "nom_entreprise", "ville": "ville", "email": "email"}
    # start_import() lance le traitement dans un thread daemon (asynchrone,
    # pour ne pas bloquer la requête HTTP réelle) — on appelle directement
    # _process_job ici pour un test déterministe, sans dépendre du minutage
    # du thread.
    conn = csv_import.get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE import_jobs SET mapping = %s, status = 'pending', started_at = now() WHERE id = %s",
                (json.dumps(mapping), job_id),
            )
        conn.commit()
    finally:
        conn.close()
    csv_import._process_job(job_id, mapping)

    created_events = _count_activity(db_conn, workspace_id, "cree")
    assert created_events == 2, (
        "L'import CSV doit créer un événement d'activité 'cree' par ligne importée "
        "(régression du bug corrigé au chantier 4 : import_csv ne loguait rien)."
    )

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT user_id FROM prospect_activity WHERE workspace_id = %s AND event_type = 'cree'",
            (workspace_id,),
        )
        rows = cur.fetchall()
    assert all(r[0] == admin_id for r in rows), (
        "Chaque prospect importé doit être attribué à l'utilisateur qui a lancé l'import "
        "(nécessaire pour /rapports-equipe)."
    )
