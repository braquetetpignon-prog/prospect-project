import os
from datetime import timedelta

from flask import Flask, jsonify, request, session, redirect, render_template as flask_render_template

from app.db import get_db
from app import csv_import
from app import naf_search
from app import ia_search
from app import workspace_settings
from app import campaigns
from app import consent
from app import sending
from app import scheduler
from app import auth
from app.auth import login_required, require_own_workspace, require_role, WRITE_ROLES
from app import prospects
from app import text_parser

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")

app = Flask(__name__, template_folder=os.path.dirname(os.path.abspath(__file__)))

app.secret_key = os.environ.get("SECRET_KEY")
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=14)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("ENV") == "preproduction"

MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10 Mo
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_SIZE


def init_db():
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        schema_sql = f.read()
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(schema_sql)
        conn.commit()
    finally:
        conn.close()


@app.route("/")
def index():
    return flask_render_template("landing.html")


@app.route("/signup")
def signup_page():
    return flask_render_template("signup.html")


@app.route("/login")
def login_page():
    return flask_render_template("login.html")


@app.route("/dashboard")
def dashboard():
    return flask_render_template("dashboard.html")


@app.route("/parametres")
def parametres_page():
    return flask_render_template("settings.html")


@app.route("/prospects")
def prospects_page():
    return flask_render_template("prospects.html")


@app.route("/campagnes")
def campagnes_page():
    return flask_render_template("campagnes.html")


@app.route("/recherche-ia")
def recherche_ia_page():
    return redirect("/import", code=301)


@app.route("/import")
def import_page():
    return flask_render_template("import.html")


@app.route("/api/status")
def api_status():
    return jsonify(status="ok", env=os.environ.get("ENV", "dev"))


@app.route("/health")
def health():
    try:
        conn = get_db()
        conn.cursor().execute("SELECT 1")
        conn.close()
        return jsonify(status="healthy", db="ok")
    except Exception as e:
        return jsonify(status="unhealthy", db="error", detail=str(e)), 503


# --- Authentification -------------------------------------------------

@app.route("/api/auth/signup", methods=["POST"])
def auth_signup():
    """Inscription d'un nouvel artisan : crée son espace de travail et son
    compte administrateur en une seule étape."""
    body = request.get_json(silent=True) or {}
    workspace_name = (body.get("workspace_name") or "").strip()
    email = (body.get("email") or "").strip()
    password = body.get("password") or ""

    if not workspace_name or not email or not password:
        return jsonify(error="workspace_name, email et password sont requis"), 400

    try:
        workspace_id, user_id = auth.create_workspace_with_admin(workspace_name, email, password)
    except auth.AuthError as exc:
        return jsonify(error=str(exc)), 400

    auth.login(email, password)
    return jsonify(status="ok", workspace_id=workspace_id), 201


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    body = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip()
    password = body.get("password") or ""
    if not email or not password:
        return jsonify(error="email et password sont requis"), 400

    try:
        auth.login(email, password)
    except auth.AuthError as exc:
        return jsonify(error=str(exc)), 401

    return jsonify(status="ok")


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    auth.logout()
    return jsonify(status="ok")


@app.route("/api/auth/me")
def auth_me():
    user = auth.current_user()
    if not user:
        return jsonify(error="Non connecté."), 401

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT email FROM users WHERE id = %s", (user["user_id"],))
            email_row = cur.fetchone()
            cur.execute("SELECT name FROM workspaces WHERE id = %s", (user["workspace_id"],))
            workspace_row = cur.fetchone()
    finally:
        conn.close()

    return jsonify(
        email=email_row[0] if email_row else None,
        role=user["role"],
        workspace_id=user["workspace_id"],
        workspace_name=workspace_row[0] if workspace_row else None,
    )


# --- Gestion des membres (admin uniquement) --------------------------------

@app.route("/api/workspaces/<int:workspace_id>/users", methods=["GET", "POST"])
@login_required
@require_own_workspace
@require_role("admin")
def workspace_users(workspace_id):
    if request.method == "GET":
        return jsonify(users=auth.list_users(workspace_id))

    body = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip()
    password = body.get("password") or ""
    role = body.get("role") or "commercial"

    if not email or not password:
        return jsonify(error="email et password sont requis"), 400

    try:
        user_id = auth.create_user(workspace_id, email, password, role)
    except auth.AuthError as exc:
        return jsonify(error=str(exc)), 400

    return jsonify(id=user_id, status="created"), 201


@app.route("/api/workspaces/<int:workspace_id>/users/<int:user_id>", methods=["PUT"])
@login_required
@require_own_workspace
@require_role("admin")
def workspace_user_update(workspace_id, user_id):
    body = request.get_json(silent=True) or {}
    if "is_active" not in body:
        return jsonify(error="is_active requis"), 400

    try:
        auth.set_user_active(workspace_id, user_id, bool(body["is_active"]))
    except auth.AuthError as exc:
        return jsonify(error=str(exc)), 404

    return jsonify(status="updated")


# --- Import CSV -------------------------------------------------------

@app.route("/api/imports/preview", methods=["POST"])
@login_required
@require_own_workspace
@require_role(*WRITE_ROLES)
def import_preview():
    if "file" not in request.files:
        return jsonify(error="Fichier manquant (champ 'file')"), 400

    workspace_id = request.form.get("workspace_id", type=int)
    if not workspace_id:
        return jsonify(error="workspace_id requis"), 400

    file = request.files["file"]
    if not file.filename.lower().endswith(".csv"):
        return jsonify(error="Le fichier doit être un .csv"), 400

    file_bytes = file.read()

    try:
        header, sample_rows, total_rows = csv_import.parse_preview(file_bytes)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400

    job_id = csv_import.create_import_job(
        workspace_id, file.filename, file_bytes, header, total_rows
    )

    return jsonify(
        job_id=job_id,
        columns=header,
        sample_rows=sample_rows,
        total_rows=total_rows,
        available_fields=list(csv_import.PROSPECT_FIELDS.keys()),
    )


def _check_import_job_access(job_id):
    """Renvoie (job, error_response) — error_response est None si l'accès est autorisé."""
    job = csv_import.get_job(job_id)
    if not job:
        return None, (jsonify(error="Job introuvable"), 404)
    if job["workspace_id"] != session.get("workspace_id"):
        return None, (jsonify(error="Job introuvable"), 404)
    return job, None


@app.route("/api/imports/<int:job_id>/start", methods=["POST"])
@login_required
@require_role(*WRITE_ROLES)
def import_start(job_id):
    job, error = _check_import_job_access(job_id)
    if error:
        return error

    body = request.get_json(silent=True) or {}
    mapping = body.get("mapping")
    if not mapping or not isinstance(mapping, dict):
        return jsonify(error="mapping requis, ex: {\"Nom\": \"nom_entreprise\"}"), 400

    try:
        csv_import.start_import(job_id, mapping)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400

    return jsonify(job_id=job_id, status="pending"), 202


@app.route("/api/imports/<int:job_id>/status")
@login_required
def import_status(job_id):
    job, error = _check_import_job_access(job_id)
    if error:
        return error
    job.pop("workspace_id", None)
    return jsonify(job)


@app.route("/api/imports/<int:job_id>/errors")
@login_required
def import_errors(job_id):
    _, error = _check_import_job_access(job_id)
    if error:
        return error
    page = request.args.get("page", 1, type=int)
    errors = csv_import.get_job_errors(job_id, page=page)
    return jsonify(page=page, errors=errors)


# --- Recherche de code NAF (référence publique, pas de donnée d'espace) ----

@app.route("/api/naf-codes/search")
@login_required
def naf_codes_search():
    query = request.args.get("q", "")
    if len(query.strip()) < 2:
        return jsonify(error="Précisez au moins 2 caractères"), 400
    results = naf_search.search_naf_codes(query)
    return jsonify(query=query, results=results)


# --- Recherche IA intégrée (Option 2) -----------------------------------

@app.route("/api/ia-search/quota")
@login_required
@require_own_workspace
def ia_search_quota():
    workspace_id = request.args.get("workspace_id", type=int)
    if not workspace_id:
        return jsonify(error="workspace_id requis"), 400
    return jsonify(ia_search.get_quota_status(workspace_id))


@app.route("/api/ia-search", methods=["POST"])
@login_required
@require_own_workspace
@require_role(*WRITE_ROLES)
def ia_search_start():
    body = request.get_json(silent=True) or {}
    workspace_id = body.get("workspace_id")
    lieu = (body.get("lieu") or "").strip()
    type_entreprise = (body.get("type_entreprise") or "").strip()
    criteres_additionnels = (body.get("criteres_additionnels") or "").strip() or None

    if not workspace_id or not lieu or not type_entreprise:
        return jsonify(error="workspace_id, lieu et type_entreprise sont requis"), 400

    try:
        result = ia_search.perform_search(workspace_id, lieu, type_entreprise, criteres_additionnels)
    except ia_search.QuotaExceeded as exc:
        return jsonify(error=str(exc)), 429
    except ia_search.GeminiError as exc:
        return jsonify(error=str(exc)), 502

    return jsonify(result)


# --- Recherches IA planifiées ------------------------------------------

@app.route("/api/workspaces/<int:workspace_id>/scheduled-searches", methods=["GET", "POST"])
@login_required
@require_own_workspace
def scheduled_searches_collection(workspace_id):
    if request.method == "GET":
        return jsonify(scheduled_searches=ia_search.list_scheduled_searches(workspace_id))

    if session.get("role") not in WRITE_ROLES:
        return jsonify(error="Permission insuffisante pour cette action."), 403

    body = request.get_json(silent=True) or {}
    lieu = (body.get("lieu") or "").strip()
    type_entreprise = (body.get("type_entreprise") or "").strip()
    heure = body.get("heure")
    if not lieu or not type_entreprise or not heure:
        return jsonify(error="lieu, type_entreprise et heure sont requis"), 400

    search_id = ia_search.create_scheduled_search(
        workspace_id, lieu, type_entreprise,
        (body.get("criteres_additionnels") or "").strip() or None, heure,
    )
    return jsonify(id=search_id, status="created"), 201


@app.route("/api/workspaces/<int:workspace_id>/scheduled-searches/<int:search_id>", methods=["PUT", "DELETE"])
@login_required
@require_own_workspace
@require_role(*WRITE_ROLES)
def scheduled_search_item(workspace_id, search_id):
    try:
        if request.method == "DELETE":
            ia_search.delete_scheduled_search(workspace_id, search_id)
            return jsonify(status="deleted")

        body = request.get_json(silent=True) or {}
        if "actif" in body:
            ia_search.set_scheduled_search_active(workspace_id, search_id, bool(body["actif"]))
        return jsonify(status="updated")
    except ia_search.GeminiError as exc:
        return jsonify(error=str(exc)), 404


@app.route("/api/workspaces/<int:workspace_id>/scheduled-search-results")
@login_required
@require_own_workspace
def scheduled_search_results(workspace_id):
    return jsonify(results=ia_search.list_pending_scheduled_results(workspace_id))


@app.route("/api/scheduled-search-results/<int:result_id>/dismiss", methods=["POST"])
@login_required
def dismiss_scheduled_result(result_id):
    try:
        ia_search.dismiss_scheduled_result(session.get("workspace_id"), result_id)
    except ia_search.GeminiError as exc:
        return jsonify(error=str(exc)), 404
    return jsonify(status="ok")


# --- Coller une réponse IA externe (sans clé API, sans coût) -----------

@app.route("/api/text-parse", methods=["POST"])
@login_required
@require_role(*WRITE_ROLES)
def text_parse():
    body = request.get_json(silent=True) or {}
    text = body.get("text") or ""
    results = text_parser.parse_pasted_text(text)
    if not results:
        return jsonify(error="Aucune entreprise reconnue dans ce texte. Vérifie le format (liste à puces)."), 400
    return jsonify(prospects=results)


# --- Option 3 : Configuration (paramètres réservés à l'administrateur) ----

@app.route("/api/workspaces/<int:workspace_id>/google-business-profile", methods=["GET", "PUT"])
@login_required
@require_own_workspace
def google_business_profile(workspace_id):
    if request.method == "GET":
        return jsonify(workspace_settings.get_google_business_profile(workspace_id))

    if session.get("role") != "admin":
        return jsonify(error="Réservé aux administrateurs."), 403

    body = request.get_json(silent=True) or {}
    profile_url = (body.get("profile_url") or "").strip()
    if not profile_url:
        return jsonify(error="profile_url requis"), 400
    workspace_settings.set_google_business_profile(workspace_id, profile_url)
    return jsonify(status="ok")


@app.route("/api/workspaces/<int:workspace_id>/smtp-config", methods=["GET", "PUT"])
@login_required
@require_own_workspace
@require_role("admin")
def smtp_config(workspace_id):
    if request.method == "GET":
        return jsonify(workspace_settings.get_smtp_config(workspace_id))

    body = request.get_json(silent=True) or {}
    required = ["host", "port", "username", "password", "from_email"]
    missing = [f for f in required if not body.get(f)]
    if missing:
        return jsonify(error=f"Champs manquants : {', '.join(missing)}"), 400

    try:
        workspace_settings.set_smtp_config(
            workspace_id, body["host"], body["port"], body["username"],
            body["password"], body["from_email"],
        )
    except workspace_settings.crypto_utils.EncryptionNotConfigured as exc:
        return jsonify(error=str(exc)), 503

    return jsonify(status="ok")


@app.route("/api/campaigns", methods=["GET", "POST"])
@login_required
@require_own_workspace
def campaigns_collection():
    if request.method == "GET":
        workspace_id = request.args.get("workspace_id", type=int)
        if not workspace_id:
            return jsonify(error="workspace_id requis"), 400
        return jsonify(campaigns=campaigns.list_campaigns(workspace_id))

    if session.get("role") not in WRITE_ROLES:
        return jsonify(error="Permission insuffisante pour cette action."), 403

    body = request.get_json(silent=True) or {}
    workspace_id = body.get("workspace_id")
    type_ = body.get("type")
    nom = body.get("nom")
    if not workspace_id or not type_ or not nom:
        return jsonify(error="workspace_id, type et nom sont requis"), 400

    try:
        campaign_id = campaigns.create_campaign(
            workspace_id, type_, nom,
            sujet=body.get("sujet"), contenu=body.get("contenu"),
            quota_par_jour=body.get("quota_par_jour", 100),
        )
    except ValueError as exc:
        return jsonify(error=str(exc)), 400

    return jsonify(id=campaign_id, status="created"), 201


def _check_campaign_access(campaign_id):
    ws_id = campaigns.campaign_workspace_id(campaign_id)
    if ws_id is None or ws_id != session.get("workspace_id"):
        return jsonify(error="Campagne introuvable"), 404
    return None


@app.route("/api/campaigns/<int:campaign_id>", methods=["PUT"])
@login_required
@require_role(*WRITE_ROLES)
def campaigns_update(campaign_id):
    error = _check_campaign_access(campaign_id)
    if error:
        return error

    body = request.get_json(silent=True) or {}
    body.pop("workspace_id", None)

    try:
        campaigns.update_campaign(session["workspace_id"], campaign_id, **body)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400

    return jsonify(status="updated")


# --- Option 3 : RGPD / Consentement ---------------------------------------

@app.route("/api/prospects/<int:prospect_id>/consent", methods=["GET", "POST"])
@login_required
def prospect_consent(prospect_id):
    ws_id = consent.prospect_workspace_id(prospect_id)
    if ws_id is None or ws_id != session.get("workspace_id"):
        return jsonify(error="Prospect introuvable"), 404

    if request.method == "GET":
        return jsonify(consent.get_all_consent_status(prospect_id))

    if session.get("role") not in WRITE_ROLES:
        return jsonify(error="Permission insuffisante pour cette action."), 403

    body = request.get_json(silent=True) or {}
    type_ = body.get("type")
    statut = body.get("statut")
    source = body.get("source")
    if not type_ or not statut:
        return jsonify(error="type et statut sont requis"), 400

    try:
        consent.record_consent(prospect_id, type_, statut, source)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400

    return jsonify(status="recorded"), 201


@app.route("/unsubscribe")
def unsubscribe():
    # Route publique, volontairement sans authentification : cliquée par un
    # destinataire externe depuis un e-mail, pas par un utilisateur ClickProspect.
    prospect_id = request.args.get("prospect_id", type=int)
    type_ = request.args.get("type")
    sig = request.args.get("sig", "")

    if not prospect_id or not type_:
        return jsonify(error="Lien de désinscription incomplet."), 400

    ok, message = consent.verify_and_unsubscribe(prospect_id, type_, sig)
    if not ok:
        return jsonify(error=message), 400

    return jsonify(status="unsubscribed", message="Vous avez bien été désinscrit·e.")


@app.route("/api/admin/purge-consent-history", methods=["POST"])
@login_required
@require_role("admin")
def admin_purge_consent_history():
    deleted = consent.purge_old_consent_history()
    return jsonify(status="ok", deleted_rows=deleted)


# --- Option 3 : Envoi ------------------------------------------------------

@app.route("/api/campaigns/<int:campaign_id>/send", methods=["POST"])
@login_required
@require_role(*WRITE_ROLES)
def campaign_send(campaign_id):
    error = _check_campaign_access(campaign_id)
    if error:
        return error

    body = request.get_json(silent=True) or {}
    prospect_ids = body.get("prospect_ids")
    planifie_pour = body.get("planifie_pour")  # ISO 8601, optionnel

    if not prospect_ids or not isinstance(prospect_ids, list):
        return jsonify(error="prospect_ids requis (liste, même à un seul élément pour un envoi unitaire)"), 400

    try:
        result = sending.queue_send(campaign_id, prospect_ids, planifie_pour)
    except sending.SendError as exc:
        return jsonify(error=str(exc)), 400

    return jsonify(result), 202


@app.route("/api/campaigns/<int:campaign_id>/sends")
@login_required
def campaign_sends_list(campaign_id):
    error = _check_campaign_access(campaign_id)
    if error:
        return error

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, prospect_id, canal, statut, planifie_pour, envoye_at, created_at
                FROM campaign_sends WHERE campaign_id = %s ORDER BY created_at DESC LIMIT 200
                """,
                (campaign_id,),
            )
            rows = cur.fetchall()
        cols = ["id", "prospect_id", "canal", "statut", "planifie_pour", "envoye_at", "created_at"]
        return jsonify(sends=[dict(zip(cols, r)) for r in rows])
    finally:
        conn.close()


@app.route("/api/admin/process-due-sends", methods=["POST"])
@login_required
@require_role("admin")
def admin_process_due_sends():
    processed = sending.process_due_sends()
    return jsonify(status="ok", processed=processed)


# --- Données pour le dashboard ---------------------------------------------

@app.route("/api/prospects", methods=["GET", "POST"])
@login_required
@require_own_workspace
def prospects_collection():
    if request.method == "POST":
        if session.get("role") not in WRITE_ROLES:
            return jsonify(error="Permission insuffisante pour cette action."), 403

        body = request.get_json(silent=True) or {}
        workspace_id = body.get("workspace_id")
        fields = {k: v for k, v in body.items() if k != "workspace_id"}
        try:
            prospect_id, warnings = prospects.create_prospect(workspace_id, fields, source="manuel")
        except prospects.ProspectError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(id=prospect_id, warnings=warnings, status="created"), 201

    return prospects_list()


@app.route("/api/prospects/<int:prospect_id>")
@login_required
def prospect_detail(prospect_id):
    prospect = prospects.get_prospect(prospect_id, session.get("workspace_id"))
    if not prospect:
        return jsonify(error="Prospect introuvable"), 404
    return jsonify(prospect)


@app.route("/api/prospects/<int:prospect_id>/statut", methods=["PUT"])
@login_required
@require_role(*WRITE_ROLES)
def prospect_update_statut(prospect_id):
    body = request.get_json(silent=True) or {}
    statut = body.get("statut")
    motif = body.get("motif")
    if not statut:
        return jsonify(error="statut requis"), 400
    try:
        prospects.update_statut(prospect_id, session.get("workspace_id"), statut, motif)
    except prospects.ProspectError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(status="updated")


def prospects_list():
    workspace_id = request.args.get("workspace_id", type=int)
    if not workspace_id:
        return jsonify(error="workspace_id requis"), 400
    statut = request.args.get("statut")

    conn = get_db()
    try:
        with conn.cursor() as cur:
            if statut:
                cur.execute(
                    """
                    SELECT id, nom_entreprise, contact_prenom, contact_nom, ville, email,
                           telephone, statut, source, created_at
                    FROM prospects WHERE workspace_id = %s AND statut = %s
                    ORDER BY created_at DESC LIMIT 500
                    """,
                    (workspace_id, statut),
                )
            else:
                cur.execute(
                    """
                    SELECT id, nom_entreprise, contact_prenom, contact_nom, ville, email,
                           telephone, statut, source, created_at
                    FROM prospects WHERE workspace_id = %s
                    ORDER BY created_at DESC LIMIT 500
                    """,
                    (workspace_id,),
                )
            rows = cur.fetchall()
        cols = ["id", "nom_entreprise", "contact_prenom", "contact_nom", "ville", "email",
                "telephone", "statut", "source", "created_at"]
        return jsonify(prospects=[dict(zip(cols, r)) for r in rows])
    finally:
        conn.close()


@app.route("/api/workspaces/<int:workspace_id>/dashboard-stats")
@login_required
@require_own_workspace
def dashboard_stats(workspace_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM prospects WHERE workspace_id = %s", (workspace_id,))
            total_prospects = cur.fetchone()[0]

            cur.execute(
                "SELECT statut, count(*) FROM prospects WHERE workspace_id = %s GROUP BY statut",
                (workspace_id,),
            )
            by_statut = {row[0]: row[1] for row in cur.fetchall()}

            cur.execute(
                "SELECT count(*) FROM campaigns WHERE workspace_id = %s AND statut = 'active'",
                (workspace_id,),
            )
            active_campaigns = cur.fetchone()[0]

            cur.execute(
                """
                SELECT count(*) FROM campaign_sends cs
                JOIN campaigns c ON c.id = cs.campaign_id
                WHERE c.workspace_id = %s AND cs.statut = 'envoye' AND cs.envoye_at > now() - interval '7 days'
                """,
                (workspace_id,),
            )
            emails_sent_7d = cur.fetchone()[0]

            cur.execute(
                """
                SELECT cs.id, p.nom_entreprise, c.nom AS campaign_nom, cs.statut, cs.envoye_at, cs.created_at
                FROM campaign_sends cs
                JOIN campaigns c ON c.id = cs.campaign_id
                JOIN prospects p ON p.id = cs.prospect_id
                WHERE c.workspace_id = %s
                ORDER BY cs.created_at DESC LIMIT 10
                """,
                (workspace_id,),
            )
            activity_cols = ["id", "prospect_nom", "campagne_nom", "statut", "envoye_at", "created_at"]
            activity = [dict(zip(activity_cols, r)) for r in cur.fetchall()]

        return jsonify(
            total_prospects=total_prospects,
            prospects_by_statut=by_statut,
            active_campaigns=active_campaigns,
            emails_sent_7d=emails_sent_7d,
            recent_activity=activity,
        )
    finally:
        conn.close()


init_db()
scheduler.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000)
