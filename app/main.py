import os

from flask import Flask, jsonify, request, render_template as flask_render_template

from app.db import get_db
from app import csv_import
from app import naf_search
from app import ia_search
from app import workspace_settings
from app import campaigns
from app import consent
from app import sending
from app import scheduler

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")

app = Flask(__name__)

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


@app.route("/dashboard")
def dashboard():
    return flask_render_template("dashboard.html")


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


# --- Import CSV -------------------------------------------------------

# NOTE : workspace_id est passé explicitement pour l'instant, en attendant
# la mise en place de l'authentification / session par espace de travail.

@app.route("/api/imports/preview", methods=["POST"])
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


@app.route("/api/imports/<int:job_id>/start", methods=["POST"])
def import_start(job_id):
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
def import_status(job_id):
    job = csv_import.get_job(job_id)
    if not job:
        return jsonify(error="Job introuvable"), 404
    job.pop("workspace_id", None)
    return jsonify(job)


@app.route("/api/imports/<int:job_id>/errors")
def import_errors(job_id):
    page = request.args.get("page", 1, type=int)
    errors = csv_import.get_job_errors(job_id, page=page)
    return jsonify(page=page, errors=errors)


# --- Recherche de code NAF ---------------------------------------------

@app.route("/api/naf-codes/search")
def naf_codes_search():
    query = request.args.get("q", "")
    if len(query.strip()) < 2:
        return jsonify(error="Précisez au moins 2 caractères"), 400
    results = naf_search.search_naf_codes(query)
    return jsonify(query=query, results=results)


# --- Recherche IA intégrée (Option 2) -----------------------------------

@app.route("/api/ia-search/quota")
def ia_search_quota():
    workspace_id = request.args.get("workspace_id", type=int)
    if not workspace_id:
        return jsonify(error="workspace_id requis"), 400
    return jsonify(ia_search.get_quota_status(workspace_id))


@app.route("/api/ia-search", methods=["POST"])
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


# --- Option 3 : Configuration --------------------------------------------

@app.route("/api/workspaces/<int:workspace_id>/google-business-profile", methods=["GET", "PUT"])
def google_business_profile(workspace_id):
    if request.method == "GET":
        return jsonify(workspace_settings.get_google_business_profile(workspace_id))

    body = request.get_json(silent=True) or {}
    profile_url = (body.get("profile_url") or "").strip()
    if not profile_url:
        return jsonify(error="profile_url requis"), 400
    workspace_settings.set_google_business_profile(workspace_id, profile_url)
    return jsonify(status="ok")


@app.route("/api/workspaces/<int:workspace_id>/smtp-config", methods=["GET", "PUT"])
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
def campaigns_collection():
    if request.method == "GET":
        workspace_id = request.args.get("workspace_id", type=int)
        if not workspace_id:
            return jsonify(error="workspace_id requis"), 400
        return jsonify(campaigns=campaigns.list_campaigns(workspace_id))

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


@app.route("/api/campaigns/<int:campaign_id>", methods=["PUT"])
def campaigns_update(campaign_id):
    body = request.get_json(silent=True) or {}
    workspace_id = body.pop("workspace_id", None)
    if not workspace_id:
        return jsonify(error="workspace_id requis"), 400

    try:
        campaigns.update_campaign(workspace_id, campaign_id, **body)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400

    return jsonify(status="updated")


# --- Option 3 : RGPD / Consentement ---------------------------------------

@app.route("/api/prospects/<int:prospect_id>/consent", methods=["GET", "POST"])
def prospect_consent(prospect_id):
    if request.method == "GET":
        return jsonify(consent.get_all_consent_status(prospect_id))

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
def admin_purge_consent_history():
    deleted = consent.purge_old_consent_history()
    return jsonify(status="ok", deleted_rows=deleted)


# --- Option 3 : Envoi ------------------------------------------------------

@app.route("/api/campaigns/<int:campaign_id>/send", methods=["POST"])
def campaign_send(campaign_id):
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
def campaign_sends_list(campaign_id):
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
def admin_process_due_sends():
    processed = sending.process_due_sends()
    return jsonify(status="ok", processed=processed)


# --- Données pour le dashboard ---------------------------------------------

@app.route("/api/workspaces", methods=["GET", "POST"])
def workspaces_list():
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        name = (body.get("name") or "").strip()
        if not name:
            return jsonify(error="name requis"), 400
        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO workspaces (name) VALUES (%s) RETURNING id", (name,))
                workspace_id = cur.fetchone()[0]
            conn.commit()
        finally:
            conn.close()
        return jsonify(id=workspace_id, name=name), 201

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name FROM workspaces ORDER BY name")
            rows = cur.fetchall()
        return jsonify(workspaces=[{"id": r[0], "name": r[1]} for r in rows])
    finally:
        conn.close()


@app.route("/api/prospects")
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
                    SELECT id, nom_entreprise, ville, email, telephone, statut, source, created_at
                    FROM prospects WHERE workspace_id = %s AND statut = %s
                    ORDER BY created_at DESC LIMIT 200
                    """,
                    (workspace_id, statut),
                )
            else:
                cur.execute(
                    """
                    SELECT id, nom_entreprise, ville, email, telephone, statut, source, created_at
                    FROM prospects WHERE workspace_id = %s
                    ORDER BY created_at DESC LIMIT 200
                    """,
                    (workspace_id,),
                )
            rows = cur.fetchall()
        cols = ["id", "nom_entreprise", "ville", "email", "telephone", "statut", "source", "created_at"]
        return jsonify(prospects=[dict(zip(cols, r)) for r in rows])
    finally:
        conn.close()


@app.route("/api/workspaces/<int:workspace_id>/dashboard-stats")
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
