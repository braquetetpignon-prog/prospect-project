import os

from flask import Flask, jsonify, request

from app.db import get_db
from app import csv_import
from app import naf_search
from app import ia_search
from app import workspace_settings
from app import campaigns

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


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000)
