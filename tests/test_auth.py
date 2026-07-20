from app import auth


def test_login_avec_bon_mot_de_passe(app, workspace_and_admin):
    with app.test_request_context():
        auth.login(workspace_and_admin["admin_email"], "MotDePasseTest123!")
        from flask import session
        assert session["user_id"] == workspace_and_admin["admin_id"]
        assert session["role"] == "admin"


def test_login_avec_mauvais_mot_de_passe_rejete(app, workspace_and_admin):
    with app.test_request_context():
        try:
            auth.login(workspace_and_admin["admin_email"], "mauvais-mot-de-passe")
            assert False, "Devrait lever AuthError"
        except auth.AuthError:
            pass


def test_compte_desactive_ne_peut_plus_se_connecter(app, workspace_and_admin, db_conn):
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE users SET is_active = FALSE WHERE id = %s",
            (workspace_and_admin["admin_id"],),
        )
    db_conn.commit()

    with app.test_request_context():
        try:
            auth.login(workspace_and_admin["admin_email"], "MotDePasseTest123!")
            assert False, "Un compte désactivé ne doit pas pouvoir se connecter"
        except auth.AuthError as exc:
            assert "désactivé" in str(exc)
