import os
from datetime import timedelta

from flask import Flask, jsonify, request, session, redirect, render_template as flask_render_template
from werkzeug.middleware.proxy_fix import ProxyFix

from app.db import get_db
from app import csv_import
from app import naf_search
from app import ia_search
from app import workspace_settings
from app import campaigns
from app import campaign_image
from app import campaign_ai
from app import consent
from app import contact
from app import sending
from app import scheduler
from app import auth
from app.auth import login_required, require_own_workspace, require_role, WRITE_ROLES
from app import prospects
from app import text_parser
from app import prospect_types
from app import rendez_vous
from app import official_search
from app import superadmin
from app import subscriptions
from app import system_mail
from app import assistant
from app import rate_limit
from app import activity
from app import security_events
from app import kb
from app import mollie_billing
from app.app_logging import logger
from flask import Response

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")

app = Flask(__name__, template_folder=os.path.dirname(os.path.abspath(__file__)))

# Coolify place l'app derrière un reverse proxy : sans ProxyFix, toutes les
# requêtes semblent venir de l'IP du proxy (request.remote_addr), ce qui
# rendrait la limitation anti brute-force par IP inutile (tout le monde
# partagerait la même IP apparente). x_for=1 fait confiance à UN seul niveau
# de X-Forwarded-For (le proxy immédiat) — cohérent avec l'infra actuelle
# (un seul reverse proxy devant l'app, pas de chaîne de proxies).
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)

app.secret_key = os.environ.get("SECRET_KEY")
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=14)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# Préprod et prod tournent toutes les deux derrière Traefik en HTTPS sur le
# VPS Coolify ; seul le docker-compose local (ENV=local, pas de TLS) sert en
# HTTP. Les cookies "secure" doivent donc être actifs dans les deux vrais
# environnements déployés, pas seulement en préproduction.
IS_DEPLOYED_ENV = os.environ.get("ENV") in ("preproduction", "production")
app.config["SESSION_COOKIE_SECURE"] = IS_DEPLOYED_ENV

MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10 Mo
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_SIZE

# Chemins jamais bloqués par le mode maintenance : /supadmin (pour pouvoir
# le désactiver soi-même), /api/supadmin (idem, API associée), /health
# (sonde de Coolify — la bloquer ferait croire à un service en panne et
# provoquerait des redémarrages en boucle), /webhook (Mollie doit toujours
# pouvoir confirmer un paiement, même pendant une maintenance), et /static.
MAINTENANCE_BYPASS_PREFIXES = ("/supadmin", "/api/supadmin", "/health", "/webhook", "/static")


@app.before_request
def _maintenance_gate():
    if request.path.startswith(MAINTENANCE_BYPASS_PREFIXES):
        return None
    if superadmin.is_maintenance_mode():
        return flask_render_template("maintenance.html"), 503
    return None


def _client_ip():
    return request.remote_addr


@app.after_request
def set_security_headers(response):
    # Durcissement HTTP standard (défense en profondeur). CSP volontairement
    # permissive sur script-src car tout le JS de l'app est inline (pas de
    # système de nonce en place) — retirer 'unsafe-inline' demanderait de
    # réécrire l'injection de JS dans les gabarits, hors périmètre de ce
    # correctif. object-src/frame-ancestors/base-uri restent stricts.
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com data:; "
        "img-src 'self' data: https:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'",
    )
    if IS_DEPLOYED_ENV:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.errorhandler(500)
def handle_internal_error(exc):
    # Le détail de l'erreur est journalisé côté serveur uniquement — jamais
    # renvoyé au client, pour ne pas exposer de détails d'implémentation.
    logger.exception("Erreur serveur non gérée sur %s %s", request.method, request.path)
    return jsonify(error="Une erreur interne est survenue."), 500


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


UPGRADE_MESSAGE = (
    "Fonction réservée aux espaces de travail en essai ou payants. "
    "Contactez-nous pour passer en version payante."
)


def _restricted_response():
    return jsonify(error=UPGRADE_MESSAGE), 402


@app.route("/")
def index():
    return flask_render_template("landing.html")


@app.route("/tarifs")
def pricing_page():
    return flask_render_template("pricing.html")


@app.route("/signup")
def signup_page():
    return flask_render_template("signup.html")


@app.route("/cgv")
def cgv_page():
    return flask_render_template("cgv.html")


@app.route("/confidentialite")
def confidentialite_page():
    return flask_render_template("confidentialite.html")


@app.route("/contact")
def contact_page():
    return flask_render_template("contact.html")


@app.route("/login")
def login_page():
    return flask_render_template("login.html")


@app.route("/changer-mot-de-passe")
def change_password_page():
    return flask_render_template("change_password.html")


@app.route("/mot-de-passe-oublie")
def forgot_password_page():
    return flask_render_template("forgot_password.html")


@app.route("/mon-compte")
def my_account_page():
    return flask_render_template("my_account.html")


# --- Superadmin (chemin volontairement non lié depuis le reste du site) ---

@app.route("/supadmin")
def supadmin_page():
    return flask_render_template("supadmin.html")


@app.route("/supadmin/login")
def supadmin_login_page():
    return flask_render_template("supadmin_login.html")


@app.route("/dashboard")
def dashboard():
    return flask_render_template("dashboard.html")


@app.route("/parametres")
def parametres_page():
    return flask_render_template("settings.html")


@app.route("/prospects")
def prospects_page():
    return flask_render_template("prospects.html")


@app.route("/clients")
def clients_page():
    return flask_render_template("clients.html")


@app.route("/pipeline")
def pipeline_page():
    """Vue kanban des prospects par statut — réservée aux espaces en essai ou
    payants (fonctionnalité Premium, voir subscriptions.RESTRICTED_PLAN). Le
    lien de menu reste visible pour tous ; c'est le contenu de la page qui
    change selon la formule, avec une incitation claire plutôt qu'un lien
    caché (cohérent avec le reste de l'app, voir UPGRADE_MESSAGE)."""
    workspace_id = session.get("workspace_id")
    restricted = subscriptions.is_restricted(workspace_id) if workspace_id else False
    return flask_render_template("pipeline.html", restricted=restricted, upgrade_message=UPGRADE_MESSAGE)


@app.route("/campagnes")
def campagnes_page():
    return flask_render_template("campagnes.html")


@app.route("/recherche-ia")
def recherche_ia_page():
    return redirect("/import", code=301)


@app.route("/import")
def import_page():
    return flask_render_template("import.html")


@app.route("/calendrier")
def calendrier_page():
    return flask_render_template("calendrier.html")


@app.route("/api/status")
def api_status():
    return jsonify(status="ok", env=os.environ.get("ENV", "local"))


@app.route("/health")
def health():
    try:
        conn = get_db()
        conn.cursor().execute("SELECT 1")
        conn.close()
        return jsonify(status="healthy", db="ok")
    except Exception:
        # Route publique et sans authentification (utilisée par Coolify pour
        # le health check) : le détail de l'erreur est journalisé côté
        # serveur uniquement, jamais renvoyé au client.
        logger.exception("Health check : échec de connexion à la base de données")
        return jsonify(status="unhealthy", db="error"), 503


# --- Authentification -------------------------------------------------

@app.route("/api/contact", methods=["POST"])
def api_contact():
    """Formulaire public /contact (visiteurs non connectés). Le message est
    toujours enregistré en base même si l'e-mail de notification échoue —
    voir app/contact.py."""
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    email = (body.get("email") or "").strip()
    message = (body.get("message") or "").strip()
    # Champ piège invisible pour les robots : un vrai visiteur ne le remplit
    # jamais. On répond succès sans rien faire, pour ne pas révéler le piège.
    honeypot = (body.get("website") or "").strip()
    if honeypot:
        return jsonify(status="ok"), 201

    if not name or not email or not message:
        return jsonify(error="Nom, e-mail et message sont requis."), 400
    if "@" not in email or "." not in email.rsplit("@", 1)[-1]:
        return jsonify(error="Adresse e-mail invalide."), 400
    if len(name) > 200 or len(email) > 200:
        return jsonify(error="Nom ou e-mail trop long."), 400
    if len(message) > 5000:
        return jsonify(error="Message trop long (5000 caractères maximum)."), 400

    ip = _client_ip()
    if contact.is_rate_limited(ip):
        return jsonify(error="Trop de messages envoyés récemment. Réessaie un peu plus tard."), 429

    message_id = contact.create_message(name, email, message, ip)

    try:
        sent = system_mail.send_system_email(
            "contact@clickprospect.fr",
            f"Nouveau message de contact — {name}",
            f"De : {name} <{email}>\n\n{message}",
            reply_to=email,
        )
        if sent:
            contact.mark_notified(message_id)
    except system_mail.SystemMailError:
        # Le message reste enregistré en base même si l'envoi échoue — pas
        # de perte, juste une notification manquée.
        logger.exception("Échec d'envoi de la notification pour le message de contact #%s", message_id)

    return jsonify(status="ok"), 201


@app.route("/api/auth/signup", methods=["POST"])
def auth_signup():
    """Inscription d'un nouvel artisan : crée son espace de travail et son
    compte administrateur en une seule étape. L'acceptation des CGV et du
    traitement RGPD est obligatoire, vérifiée ici avant toute création —
    voir app/signup.html pour les deux cases à cocher correspondantes."""
    body = request.get_json(silent=True) or {}
    workspace_name = (body.get("workspace_name") or "").strip()
    email = (body.get("email") or "").strip()
    password = body.get("password") or ""
    cgv_accepted = bool(body.get("cgv_accepted"))
    rgpd_accepted = bool(body.get("rgpd_accepted"))

    if not workspace_name or not email or not password:
        return jsonify(error="workspace_name, email et password sont requis"), 400
    if not cgv_accepted or not rgpd_accepted:
        return jsonify(error="L'acceptation des CGV et du traitement des données est obligatoire."), 400

    try:
        workspace_id, user_id = auth.create_workspace_with_admin(
            workspace_name, email, password, consent_ip=_client_ip(),
        )
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

    ip = _client_ip()
    limited, retry_after = rate_limit.is_rate_limited(email, ip)
    if limited:
        logger.warning("Connexion bloquée (trop de tentatives) pour %s depuis %s", email, ip)
        return jsonify(error="Trop de tentatives échouées. Réessaie dans quelques minutes."), 429

    try:
        auth.login(email, password)
    except auth.AuthError as exc:
        rate_limit.record_attempt(email, ip, success=False)
        logger.warning("Échec de connexion pour %s depuis %s", email, ip)
        return jsonify(error=str(exc)), 401

    rate_limit.record_attempt(email, ip, success=True)
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

    sub = subscriptions.get_workspace_subscription(user["workspace_id"]) or {}

    return jsonify(
        email=email_row[0] if email_row else None,
        role=user["role"],
        workspace_id=user["workspace_id"],
        workspace_name=workspace_row[0] if workspace_row else None,
        must_change_password=user["must_change_password"],
        plan=sub.get("plan"),
        plan_effective=sub.get("plan_effective"),
        trial_days_left=sub.get("trial_days_left"),
        paid_until=sub.get("paid_until").isoformat() if sub.get("paid_until") else None,
        billing_interval=sub.get("billing_interval"),
        restricted=sub.get("restricted", False),
        impersonating=bool(session.get("impersonation_superadmin_id")),
    )


@app.route("/api/users/me/dashboard-layout", methods=["GET", "PUT"])
@login_required
def dashboard_layout():
    """Disposition personnelle des widgets du tableau de bord — propre à
    chaque utilisateur, n'affecte jamais les collègues du même espace."""
    if request.method == "PUT":
        body = request.get_json(silent=True) or {}
        try:
            auth.set_dashboard_layout(session["user_id"], body.get("layout"))
        except auth.AuthError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(status="ok")

    return jsonify(layout=auth.get_dashboard_layout(session["user_id"]))


@app.route("/api/auth/change-password", methods=["POST"])
@login_required
def auth_change_password():
    body = request.get_json(silent=True) or {}
    current_password = body.get("current_password") or ""
    new_password = body.get("new_password") or ""
    try:
        auth.change_own_password(session["user_id"], current_password, new_password)
    except auth.AuthError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(status="ok")


@app.route("/api/auth/pin", methods=["GET", "PUT"])
@login_required
def auth_pin():
    """Gestion du code PIN de récupération — strictement personnel, chaque
    utilisateur gère le sien depuis Mon compte."""
    if request.method == "GET":
        return jsonify(has_pin=auth.has_pin(session["user_id"]))

    body = request.get_json(silent=True) or {}
    current_password = body.get("current_password") or ""
    pin = (body.get("pin") or "").strip()
    try:
        auth.set_pin(session["user_id"], current_password, pin)
    except auth.AuthError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(status="ok")


@app.route("/api/billing/subscribe", methods=["POST"])
@login_required
@require_role("admin")
def billing_subscribe():
    body = request.get_json(silent=True) or {}
    interval = body.get("interval")
    if interval not in ("monthly", "annual"):
        return jsonify(error="Intervalle de facturation invalide."), 400

    workspace_id = session["workspace_id"]
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM workspaces WHERE id = %s", (workspace_id,))
            row = cur.fetchone()
    finally:
        conn.close()
    workspace_name = row[0] if row else "ClickProspect"

    try:
        checkout_url = mollie_billing.create_first_payment(
            workspace_id, workspace_name, session.get("email", ""), interval
        )
    except mollie_billing.MollieError as exc:
        return jsonify(error=str(exc)), 502

    return jsonify(checkout_url=checkout_url)


@app.route("/api/billing/cancel", methods=["POST"])
@login_required
@require_role("admin")
def billing_cancel():
    try:
        mollie_billing.cancel_subscription(session["workspace_id"])
    except mollie_billing.MollieError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(status="ok")


@app.route("/webhook/mollie", methods=["POST"])
def webhook_mollie():
    """Appelé par Mollie, jamais par le navigateur. Ne recevra que
    l'identifiant du paiement — on recharge systématiquement l'état réel
    depuis l'API Mollie avant d'agir (voir mollie_billing.py)."""
    payment_id = request.form.get("id")
    if not payment_id:
        return "", 400
    try:
        mollie_billing.handle_webhook_payment(payment_id)
    except mollie_billing.MollieError as exc:
        logger.error(f"Webhook Mollie en erreur pour {payment_id} : {exc}")
        return "", 200  # on répond 200 quand même pour éviter un flood de retries Mollie sur une erreur de config
    return "", 200


@app.route("/api/auth/forgot-password", methods=["POST"])
def auth_forgot_password():
    """Réinitialisation du mot de passe via code PIN, sans e-mail. Fortement
    limitée en débit (par e-mail visé ET par IP) — c'est un second chemin
    d'authentification, donc aussi sensible qu'un login."""
    body = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip()
    pin = (body.get("pin") or "").strip()
    new_password = body.get("new_password") or ""
    if not email or not pin or not new_password:
        return jsonify(error="email, pin et new_password sont requis"), 400

    ip = _client_ip()
    identifier = f"pinreset:{email}"
    limited, retry_after = rate_limit.is_rate_limited(
        identifier, ip, max_per_identifier=5, max_per_ip=15,
    )
    if limited:
        logger.warning("Réinitialisation par PIN bloquée (trop de tentatives) pour %s depuis %s", email, ip)
        security_events.notify_pin_rate_limited(email, ip)
        return jsonify(error="Trop de tentatives. Réessaie dans quelques minutes."), 429

    try:
        workspace_id = auth.reset_password_with_pin(email, pin, new_password)
    except auth.AuthError as exc:
        rate_limit.record_attempt(identifier, ip, success=False)
        return jsonify(error=str(exc)), 400

    rate_limit.record_attempt(identifier, ip, success=True)
    logger.warning("Mot de passe réinitialisé via PIN pour %s depuis %s", email, ip)
    security_events.notify_password_reset_via_pin(workspace_id, email, ip)
    return jsonify(status="ok")


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


@app.route("/api/workspaces/<int:workspace_id>/users/<int:user_id>", methods=["PUT", "DELETE"])
@login_required
@require_own_workspace
@require_role("admin")
def workspace_user_update(workspace_id, user_id):
    if request.method == "DELETE":
        if user_id == session.get("user_id"):
            return jsonify(error="Tu ne peux pas supprimer ton propre compte — demande à un autre administrateur."), 400
        try:
            auth.delete_user(workspace_id, user_id)
        except auth.AuthError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(status="deleted")

    body = request.get_json(silent=True) or {}
    if not any(k in body for k in ("is_active", "email", "role")):
        return jsonify(error="Aucun champ reconnu (is_active, email ou role attendu)."), 400

    if "is_active" in body:
        try:
            auth.set_user_active(workspace_id, user_id, bool(body["is_active"]))
        except auth.AuthError as exc:
            return jsonify(error=str(exc)), 404

    if "email" in body or "role" in body:
        try:
            auth.update_user(workspace_id, user_id, body)
        except auth.AuthError as exc:
            return jsonify(error=str(exc)), 400

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


# --- Assistant d'aide en ligne ------------------------------------------
# workspace_id vient toujours de la session, jamais d'un paramètre fourni par
# le client — pas besoin de require_own_workspace ici.

@app.route("/api/assistant/quota")
@login_required
def assistant_quota():
    return jsonify(assistant.get_quota_status(session["workspace_id"]))


@app.route("/api/assistant/chat", methods=["POST"])
@login_required
def assistant_chat():
    body = request.get_json(silent=True) or {}
    message = body.get("message")
    history = body.get("history") or []
    if not isinstance(history, list):
        history = []
    try:
        reply = assistant.send_message(session["workspace_id"], history, message)
    except assistant.AssistantError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(reply=reply)


@app.route("/api/assistant/feedback", methods=["POST"])
@login_required
def assistant_feedback():
    body = request.get_json(silent=True) or {}
    message = body.get("message")

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT email FROM users WHERE id = %s", (session["user_id"],))
            email_row = cur.fetchone()
            cur.execute("SELECT name FROM workspaces WHERE id = %s", (session["workspace_id"],))
            workspace_row = cur.fetchone()
    finally:
        conn.close()

    try:
        assistant.submit_feedback(
            session["workspace_id"],
            workspace_row[0] if workspace_row else None,
            email_row[0] if email_row else None,
            message,
        )
    except assistant.AssistantError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(status="ok")


@app.route("/api/admin/gemini-model", methods=["GET", "PUT"])
@login_required
@require_role("admin")
def admin_gemini_model():
    if request.method == "GET":
        return jsonify(model=ia_search.get_current_model(), default=ia_search.DEFAULT_GEMINI_MODEL)
    body = request.get_json(silent=True) or {}
    ia_search.set_current_model(body.get("model"))
    return jsonify(status="ok", model=ia_search.get_current_model())


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
        if text_parser.looks_like_unfilled_prompt(text):
            return jsonify(error=(
                "Ce texte contient encore des champs entre crochets (ex: [Nom de l'entreprise]) — "
                "on dirait que c'est le PROMPT qui a été collé, pas la réponse de l'IA. Colle d'abord "
                "ce texte sur Gemini/ChatGPT, récupère sa réponse, puis colle CETTE réponse ici."
            )), 400
        return jsonify(error="Aucune entreprise reconnue dans ce texte. Vérifie le format (liste à puces)."), 400
    return jsonify(prospects=results)


# --- Recherche automatique par APIs officielles (aucune donnée inventée) ---

@app.route("/api/official-search", methods=["POST"])
@login_required
@require_role(*WRITE_ROLES)
def official_search_route():
    body = request.get_json(silent=True) or {}
    zone = body.get("zone")
    secteur = body.get("secteur")
    forms = body.get("forms") or {}  # {"sarl_eurl": 20, "sas_sasu": 20, ...}

    try:
        forms_clean = {k: int(v) for k, v in forms.items() if int(v or 0) > 0}
        result = official_search.search_by_legal_forms(zone, secteur, forms_clean)
    except official_search.OfficialSearchError as exc:
        return jsonify(error=str(exc)), 400
    except (ValueError, TypeError):
        return jsonify(error="Quantités invalides."), 400

    return jsonify(result)


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


@app.route("/api/workspaces/<int:workspace_id>/smtp-config/test", methods=["POST"])
@login_required
@require_own_workspace
@require_role("admin")
def smtp_config_test(workspace_id):
    """Envoie un e-mail de test réel à l'adresse d'expédition elle-même.
    Ne marque la config comme vérifiée qu'en cas de succès — c'est ce flag
    qui conditionne ensuite le lancement de campagnes (cf. app/sending.py)."""
    try:
        sending.send_smtp_test(workspace_id)
    except sending.SmtpTestError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(status="ok", verified=True)


@app.route("/api/workspaces/<int:workspace_id>/logo", methods=["POST", "DELETE"])
@login_required
@require_own_workspace
@require_role("admin")
def workspace_logo_route(workspace_id):
    if request.method == "DELETE":
        workspace_settings.remove_workspace_logo(workspace_id)
        return jsonify(status="removed")

    if "logo" not in request.files:
        return jsonify(error="Aucun fichier reçu (champ 'logo' attendu)."), 400
    file_bytes = request.files["logo"].read()
    try:
        logo_bytes, mimetype = campaign_image.process_upload(file_bytes)
    except campaign_image.ImageError as exc:
        return jsonify(error=str(exc)), 400
    workspace_settings.set_workspace_logo(workspace_id, logo_bytes, mimetype)
    return jsonify(status="ok"), 201


@app.route("/api/workspaces/<int:workspace_id>/logo/preview")
@login_required
@require_own_workspace
def workspace_logo_preview(workspace_id):
    logo = workspace_settings.get_workspace_logo(workspace_id)
    if not logo:
        return jsonify(error="Aucun logo pour cet espace de travail."), 404
    return Response(logo["data"], mimetype=logo["mimetype"])


@app.route("/api/workspaces/<int:workspace_id>/rgpd-registre.csv")
@login_required
@require_own_workspace
@require_role("admin")
def workspace_rgpd_registre(workspace_id):
    csv_content = sending.registre_traitement_csv(workspace_id)
    return Response(
        csv_content,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=registre-traitement-rgpd.csv"},
    )


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


@app.route("/api/campaigns/<int:campaign_id>/image", methods=["POST", "DELETE"])
@login_required
@require_role(*WRITE_ROLES)
def campaign_image_route(campaign_id):
    error = _check_campaign_access(campaign_id)
    if error:
        return error

    if request.method == "DELETE":
        campaigns.remove_campaign_image(session["workspace_id"], campaign_id)
        return jsonify(status="removed")

    if "image" not in request.files:
        return jsonify(error="Aucun fichier image reçu (champ 'image' attendu)."), 400
    file = request.files["image"]
    file_bytes = file.read()

    crop_box = None
    for field in ("crop_left", "crop_top", "crop_right", "crop_bottom"):
        if request.form.get(field) is None:
            crop_box = None
            break
    else:
        try:
            crop_box = (
                float(request.form["crop_left"]), float(request.form["crop_top"]),
                float(request.form["crop_right"]), float(request.form["crop_bottom"]),
            )
        except ValueError:
            return jsonify(error="Coordonnées de recadrage invalides."), 400

    try:
        image_bytes, mimetype = campaign_image.process_upload(file_bytes, crop_box=crop_box)
    except campaign_image.ImageError as exc:
        return jsonify(error=str(exc)), 400

    campaigns.set_campaign_image(session["workspace_id"], campaign_id, image_bytes, mimetype)
    return jsonify(status="ok", size_bytes=len(image_bytes)), 201


@app.route("/api/campaigns/<int:campaign_id>/image/preview")
@login_required
def campaign_image_preview(campaign_id):
    error = _check_campaign_access(campaign_id)
    if error:
        return error
    image = campaigns.get_campaign_image(session["workspace_id"], campaign_id)
    if not image:
        return jsonify(error="Aucune image pour cette campagne."), 404
    return Response(image["data"], mimetype=image["mimetype"])


@app.route("/api/campaigns/<int:campaign_id>/improve-content", methods=["POST"])
@login_required
@require_role(*WRITE_ROLES)
def campaign_improve_content(campaign_id):
    error = _check_campaign_access(campaign_id)
    if error:
        return error
    if subscriptions.is_restricted(session["workspace_id"]):
        return _restricted_response()

    body = request.get_json(silent=True) or {}
    draft = body.get("contenu")
    campaign_type = body.get("type")
    if not draft:
        return jsonify(error="contenu requis"), 400

    try:
        improved = campaign_ai.improve_content(draft, campaign_type)
    except campaign_ai.ImprovementError as exc:
        return jsonify(error=str(exc)), 400

    return jsonify(contenu=improved)


@app.route("/api/campaigns/<int:campaign_id>/preview")
@login_required
def campaign_preview(campaign_id):
    """Aperçu du mail tel qu'il serait réellement envoyé (avec image et
    variables remplies) pour un prospect donné, sans envoi réel — filet de
    sécurité avant un envoi à plusieurs dizaines de destinataires."""
    error = _check_campaign_access(campaign_id)
    if error:
        return error
    prospect_id = request.args.get("prospect_id", type=int)
    if not prospect_id:
        return jsonify(error="prospect_id requis"), 400
    try:
        preview = sending.preview_campaign_email(campaign_id, prospect_id, session["workspace_id"])
    except sending.SendError as exc:
        return jsonify(error=str(exc)), 404
    return jsonify(preview)


@app.route("/api/campaigns/<int:campaign_id>/unsubscribe-rate")
@login_required
def campaign_unsubscribe_rate(campaign_id):
    error = _check_campaign_access(campaign_id)
    if error:
        return error
    return jsonify(sending.get_unsubscribe_rate(campaign_id) or {})


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
    if subscriptions.is_restricted(session["workspace_id"]):
        campaign = campaigns.get_campaign(campaign_id)
        if not campaign or campaign["type"] != "relance":
            return _restricted_response()

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


@app.route("/api/campaigns/<int:campaign_id>/relance-eligible")
@login_required
def campaign_relance_eligible(campaign_id):
    error = _check_campaign_access(campaign_id)
    if error:
        return error
    campaign = campaigns.get_campaign(campaign_id)
    if not campaign or campaign["type"] != "relance":
        return jsonify(error="Cette route est réservée aux campagnes de type relance."), 400
    return jsonify(sending.get_relance_eligible_prospects(session["workspace_id"]))


@app.route("/api/campaigns/<int:campaign_id>/send-by-type", methods=["POST"])
@login_required
@require_role(*WRITE_ROLES)
def campaign_send_by_type(campaign_id):
    error = _check_campaign_access(campaign_id)
    if error:
        return error
    if subscriptions.is_restricted(session["workspace_id"]):
        return _restricted_response()

    body = request.get_json(silent=True) or {}
    prospect_type_id = body.get("prospect_type_id")
    if not prospect_type_id:
        return jsonify(error="prospect_type_id requis"), 400

    # Garde-fou : une campagne ne cible jamais les prospects "en attente" ou
    # "recalés", même quand l'envoi est déclenché par type de statut juridique
    # plutôt que par sélection manuelle — cf. sending.ALLOWED_SEND_STATUTS.
    targets = prospects.search_prospects(session["workspace_id"], prospect_type_id=prospect_type_id, limit=10000)
    prospect_ids = [
        p["id"] for p in targets
        if p.get("email") and p.get("statut") in sending.ALLOWED_SEND_STATUTS
    ]
    if not prospect_ids:
        return jsonify(error="Aucun prospect qualifié ou client avec e-mail pour ce type."), 400

    try:
        result = sending.queue_send(campaign_id, prospect_ids, body.get("planifie_pour"))
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


@app.route("/api/campaigns/<int:campaign_id>/sends/<int:send_id>/cancel", methods=["POST"])
@login_required
@require_role(*WRITE_ROLES)
def campaign_send_cancel(campaign_id, send_id):
    """Annule un envoi planifié individuel — filet de sécurité en cas d'erreur
    (mauvais destinataire sélectionné, etc.). N'annule que s'il n'a pas déjà
    démarré."""
    error = _check_campaign_access(campaign_id)
    if error:
        return error
    cancelled = sending.cancel_send(campaign_id, send_id)
    if not cancelled:
        return jsonify(error="Cet envoi n'est plus annulable (déjà en cours, envoyé ou en échec)."), 409
    return jsonify(status="annule")


@app.route("/api/campaigns/<int:campaign_id>/sends/cancel-all", methods=["POST"])
@login_required
@require_role(*WRITE_ROLES)
def campaign_sends_cancel_all(campaign_id):
    """Annule en masse tous les envois encore planifiés pour cette campagne."""
    error = _check_campaign_access(campaign_id)
    if error:
        return error
    count = sending.cancel_all_planned(campaign_id)
    return jsonify(status="ok", cancelled=count)


@app.route("/api/admin/process-due-sends", methods=["POST"])
@login_required
@require_role("admin")
def admin_process_due_sends():
    processed = sending.process_due_sends()
    return jsonify(status="ok", processed=processed)


@app.route("/api/search")
@login_required
def global_search():
    """Recherche globale (prospects, campagnes, rendez-vous) — une seule barre
    plutôt que de devoir savoir où chercher. Résultats limités et strictement
    filtrés sur l'espace de travail de la session, comme partout ailleurs."""
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify(results=[])
    workspace_id = session["workspace_id"]
    like = f"%{q}%"

    results = []
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, nom_entreprise FROM prospects
                WHERE workspace_id = %s AND (nom_entreprise ILIKE %s OR email ILIKE %s)
                ORDER BY created_at DESC LIMIT 5
                """,
                (workspace_id, like, like),
            )
            for pid, nom in cur.fetchall():
                results.append({"type": "prospect", "id": pid, "label": nom, "url": f"/prospects?id={pid}"})

            cur.execute(
                "SELECT id, nom FROM campaigns WHERE workspace_id = %s AND nom ILIKE %s ORDER BY created_at DESC LIMIT 5",
                (workspace_id, like),
            )
            for cid, nom in cur.fetchall():
                results.append({"type": "campagne", "id": cid, "label": nom, "url": f"/campagnes?id={cid}"})

            cur.execute(
                "SELECT id, titre FROM rendez_vous WHERE workspace_id = %s AND titre ILIKE %s ORDER BY date_heure DESC LIMIT 5",
                (workspace_id, like),
            )
            for rid, titre in cur.fetchall():
                results.append({"type": "rendez-vous", "id": rid, "label": titre, "url": f"/calendrier?id={rid}"})
    finally:
        conn.close()

    return jsonify(results=results)


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

    workspace_id = request.args.get("workspace_id", type=int)
    if not workspace_id:
        return jsonify(error="workspace_id requis"), 400
    statut_values = request.args.getlist("statut")  # supporte ?statut=qualifie&statut=client
    results = prospects.search_prospects(
        workspace_id,
        query=request.args.get("q"),
        statut=statut_values if len(statut_values) > 1 else (statut_values[0] if statut_values else None),
        prospect_type_id=request.args.get("prospect_type_id", type=int),
        limit=request.args.get("limit", type=int) or 500,
    )
    return jsonify(prospects=results)


@app.route("/api/prospects/bulk-delete", methods=["POST"])
@login_required
@require_role(*WRITE_ROLES)
def prospects_bulk_delete():
    body = request.get_json(silent=True) or {}
    ids = body.get("ids") or []
    try:
        deleted, protected_count = prospects.delete_prospects_bulk(ids, session.get("workspace_id"))
    except prospects.ProspectError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(status="deleted", deleted_ids=deleted, count=len(deleted), protected_clients=protected_count)


@app.route("/api/prospects/export.csv")
@login_required
@require_own_workspace
def prospects_export_csv():
    workspace_id = request.args.get("workspace_id", type=int)
    if not workspace_id:
        return jsonify(error="workspace_id requis"), 400
    if subscriptions.is_restricted(workspace_id):
        return _restricted_response()
    csv_content = prospects.export_csv(workspace_id, statut=request.args.get("statut"))
    return Response(
        csv_content,
        mimetype="text/csv",
        headers={
            "Content-Disposition": "attachment; filename=prospects.csv",
            "Cache-Control": "no-store",
        },
    )


@app.route("/api/prospects/verify-siret", methods=["POST"])
@login_required
@require_role(*WRITE_ROLES)
def prospects_verify_siret():
    body = request.get_json(silent=True) or {}
    try:
        result = prospects.verify_siret(body.get("siret"))
    except prospects.ProspectError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(result)


@app.route("/api/prospects/<int:prospect_id>", methods=["GET", "PUT", "DELETE"])
@login_required
def prospect_detail(prospect_id):
    if request.method == "DELETE":
        if session.get("role") not in WRITE_ROLES:
            return jsonify(error="Permission insuffisante pour cette action."), 403
        try:
            prospects.delete_prospect(prospect_id, session.get("workspace_id"))
        except prospects.ProspectError as exc:
            return jsonify(error=str(exc)), 404
        return jsonify(status="deleted")

    if request.method == "PUT":
        if session.get("role") not in WRITE_ROLES:
            return jsonify(error="Permission insuffisante pour cette action."), 403
        body = request.get_json(silent=True) or {}
        try:
            prospects.update_prospect(prospect_id, session.get("workspace_id"), body)
        except prospects.ProspectError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(status="updated")

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


@app.route("/api/prospects/<int:prospect_id>/activity")
@login_required
def prospect_activity(prospect_id):
    """Historique d'activité (timeline) — création, changements de statut,
    RDV planifiés, campagnes reçues. Vérifie l'appartenance à l'espace de
    travail via get_prospect (renvoie None si un autre workspace essaie)."""
    prospect = prospects.get_prospect(prospect_id, session.get("workspace_id"))
    if not prospect:
        return jsonify(error="Prospect introuvable"), 404
    return jsonify(activity=activity.list_activity(prospect_id, session.get("workspace_id")))


# --- Types de statut (forme juridique / catégorie) --------------------

@app.route("/api/workspaces/<int:workspace_id>/prospect-types", methods=["GET", "POST"])
@login_required
@require_own_workspace
def prospect_types_collection(workspace_id):
    if request.method == "GET":
        return jsonify(
            types=prospect_types.list_types(workspace_id),
            unclassified_count=prospect_types.count_unclassified(workspace_id),
        )
    if session.get("role") != "admin":
        return jsonify(error="Réservé aux administrateurs."), 403
    body = request.get_json(silent=True) or {}
    try:
        type_id = prospect_types.create_type(workspace_id, body.get("nom"))
    except prospect_types.ProspectTypeError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(id=type_id, status="created"), 201


@app.route("/api/workspaces/<int:workspace_id>/prospect-types/<int:type_id>", methods=["DELETE"])
@login_required
@require_own_workspace
@require_role("admin")
def prospect_types_item(workspace_id, type_id):
    try:
        prospect_types.delete_type(workspace_id, type_id)
    except prospect_types.ProspectTypeError as exc:
        return jsonify(error=str(exc)), 404
    return jsonify(status="deleted")


@app.route("/api/workspaces/<int:workspace_id>/prospect-types-stats")
@login_required
@require_own_workspace
def prospect_types_stats(workspace_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT pt.id, pt.nom, count(p.id) AS total,
                       count(p.id) FILTER (WHERE p.email IS NOT NULL AND p.email != '') AS avec_email,
                       count(p.id) FILTER (
                           WHERE p.email IS NOT NULL AND p.email != '' AND p.statut IN ('qualifie', 'client')
                       ) AS eligibles
                FROM prospect_types pt
                LEFT JOIN prospects p ON p.prospect_type_id = pt.id AND p.workspace_id = pt.workspace_id
                WHERE pt.workspace_id = %s
                GROUP BY pt.id, pt.nom
                ORDER BY pt.nom
                """,
                (workspace_id,),
            )
            rows = cur.fetchall()
        return jsonify(types=[
            {"id": r[0], "nom": r[1], "total": r[2], "avec_email": r[3], "eligibles": r[4]} for r in rows
        ])
    finally:
        conn.close()


# --- Rendez-vous (calendrier partagé) -----------------------------------

@app.route("/api/workspaces/<int:workspace_id>/rendez-vous", methods=["GET", "POST"])
@login_required
@require_own_workspace
def rendez_vous_collection(workspace_id):
    if request.method == "GET":
        return jsonify(rendez_vous=rendez_vous.list_rendez_vous(
            workspace_id, start=request.args.get("start"), end=request.args.get("end")
        ))

    body = request.get_json(silent=True) or {}
    try:
        rdv_id = rendez_vous.create_rendez_vous(
            workspace_id, session["user_id"], body.get("titre"), body.get("date_heure"),
            duree_minutes=body.get("duree_minutes", 30), prospect_id=body.get("prospect_id"),
            notes=body.get("notes"),
        )
    except rendez_vous.RdvError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(id=rdv_id, status="created"), 201


@app.route("/api/rendez-vous/<int:rdv_id>", methods=["PUT", "DELETE"])
@login_required
def rendez_vous_item(rdv_id):
    workspace_id = session.get("workspace_id")
    try:
        if request.method == "DELETE":
            rendez_vous.delete_rendez_vous(rdv_id, workspace_id, session["user_id"], session.get("role"))
            return jsonify(status="deleted")

        body = request.get_json(silent=True) or {}
        rendez_vous.update_rendez_vous(rdv_id, workspace_id, session["user_id"], session.get("role"), body)
        return jsonify(status="updated")
    except rendez_vous.RdvPermissionError as exc:
        return jsonify(error=str(exc)), 403
    except rendez_vous.RdvError as exc:
        return jsonify(error=str(exc)), 404


@app.route("/api/rendez-vous/<int:rdv_id>/ics")
@login_required
def rendez_vous_ics(rdv_id):
    rdv = rendez_vous.get_rendez_vous(rdv_id, session.get("workspace_id"))
    if not rdv:
        return jsonify(error="Rendez-vous introuvable"), 404
    ics_content = rendez_vous.generate_ics(rdv)
    return Response(
        ics_content,
        mimetype="text/calendar",
        headers={"Content-Disposition": "attachment; filename=rendez-vous.ics"},
    )


@app.route("/api/rendez-vous/<int:rdv_id>/send-ics", methods=["POST"])
@login_required
def rendez_vous_send_ics(rdv_id):
    rdv = rendez_vous.get_rendez_vous(rdv_id, session.get("workspace_id"))
    if not rdv:
        return jsonify(error="Rendez-vous introuvable"), 404

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT email FROM users WHERE id = %s", (session["user_id"],))
            to_email = cur.fetchone()[0]
    finally:
        conn.close()

    try:
        rendez_vous.send_ics_by_email(session.get("workspace_id"), rdv, to_email)
    except sending.EmailSendError as exc:
        return jsonify(error=str(exc)), 503
    return jsonify(status="sent", to=to_email)





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

            cur.execute(
                """
                SELECT rv.id, rv.titre, rv.date_heure, rv.duree_minutes, p.nom_entreprise
                FROM rendez_vous rv
                LEFT JOIN prospects p ON p.id = rv.prospect_id
                WHERE rv.workspace_id = %s AND rv.date_heure > now()
                ORDER BY rv.date_heure ASC LIMIT 8
                """,
                (workspace_id,),
            )
            rdv_cols = ["id", "titre", "date_heure", "duree_minutes", "prospect_nom"]
            upcoming_rdv = [dict(zip(rdv_cols, r)) for r in cur.fetchall()]

        return jsonify(
            total_prospects=total_prospects,
            prospects_by_statut=by_statut,
            active_campaigns=active_campaigns,
            emails_sent_7d=emails_sent_7d,
            recent_activity=activity,
            upcoming_rdv=upcoming_rdv,
        )
    finally:
        conn.close()


# --- Superadmin (API) -------------------------------------------------
# Toutes ces routes utilisent leur propre décorateur d'authentification
# (superadmin.login_required), complètement séparé de login_required /
# require_role utilisés partout ailleurs — un compte utilisateur normal,
# même admin de son espace, ne peut jamais accéder à ces routes.

@app.route("/api/supadmin/login", methods=["POST"])
def supadmin_login():
    body = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip()
    password = body.get("password") or ""
    if not email or not password:
        return jsonify(error="email et password sont requis"), 400

    ip = _client_ip()
    limited, retry_after = rate_limit.is_rate_limited(
        email, ip,
        max_per_identifier=rate_limit.MAX_ATTEMPTS_SUPERADMIN_IDENTIFIER,
        max_per_ip=rate_limit.MAX_ATTEMPTS_SUPERADMIN_IP,
    )
    if limited:
        logger.warning("Connexion superadmin bloquée (trop de tentatives) pour %s depuis %s", email, ip)
        return jsonify(error="Trop de tentatives échouées. Réessaie dans quelques minutes."), 429

    try:
        superadmin.login(email, password)
    except superadmin.SuperadminError as exc:
        rate_limit.record_attempt(email, ip, success=False)
        logger.warning("Échec de connexion superadmin pour %s depuis %s", email, ip)
        return jsonify(error=str(exc)), 401

    rate_limit.record_attempt(email, ip, success=True)
    return jsonify(status="ok")


@app.route("/api/supadmin/logout", methods=["POST"])
def supadmin_logout():
    superadmin.logout()
    return jsonify(status="ok")


@app.route("/api/supadmin/maintenance", methods=["GET", "POST"])
@superadmin.login_required
def supadmin_maintenance():
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        enabled = bool(body.get("enabled"))
        superadmin.set_maintenance_mode(enabled)
        return jsonify(status="ok", enabled=enabled)
    return jsonify(enabled=superadmin.is_maintenance_mode())


@app.route("/api/supadmin/workspaces")
@superadmin.login_required
def supadmin_workspaces():
    return jsonify(workspaces=superadmin.list_workspaces())


@app.route("/api/supadmin/workspaces/<int:workspace_id>/detail")
@superadmin.login_required
def supadmin_workspace_detail(workspace_id):
    return jsonify(**superadmin.get_workspace_detail(workspace_id))


@app.route("/api/supadmin/workspaces/<int:workspace_id>/plan", methods=["PUT"])
@superadmin.login_required
def supadmin_set_plan(workspace_id):
    body = request.get_json(silent=True) or {}
    plan = body.get("plan")
    paid_until = body.get("paid_until")  # ISO 8601, requis si plan == 'paid'
    try:
        superadmin.set_plan(workspace_id, plan, paid_until=paid_until)
    except superadmin.SuperadminError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(status="ok")


@app.route("/api/supadmin/workspaces/<int:workspace_id>/ia-quota", methods=["PUT"])
@superadmin.login_required
def supadmin_set_ia_quota(workspace_id):
    body = request.get_json(silent=True) or {}
    quota = body.get("quota")  # entier, ou None/absent pour revenir au défaut global
    try:
        superadmin.set_ia_search_quota_override(workspace_id, quota)
    except superadmin.SuperadminError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(status="ok")


@app.route("/api/supadmin/workspaces/<int:workspace_id>/reset-admin-password", methods=["POST"])
@superadmin.login_required
def supadmin_reset_password(workspace_id):
    try:
        result = superadmin.reset_workspace_admin_password(workspace_id)
    except superadmin.SuperadminError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(result)


@app.route("/api/supadmin/workspaces/<int:workspace_id>", methods=["DELETE"])
@superadmin.login_required
def supadmin_delete_workspace(workspace_id):
    try:
        superadmin.delete_workspace(workspace_id)
    except superadmin.SuperadminError as exc:
        return jsonify(error=str(exc)), 404
    return jsonify(status="ok")


@app.route("/api/supadmin/workspaces/<int:workspace_id>/dismiss-deletion", methods=["POST"])
@superadmin.login_required
def supadmin_dismiss_deletion(workspace_id):
    try:
        superadmin.dismiss_deletion_request(workspace_id)
    except superadmin.SuperadminError as exc:
        return jsonify(error=str(exc)), 404
    return jsonify(status="ok")


@app.route("/api/supadmin/workspaces/<int:workspace_id>/login-as", methods=["POST"])
@superadmin.login_required
def supadmin_login_as(workspace_id):
    try:
        superadmin.login_as(workspace_id)
    except superadmin.SuperadminError as exc:
        return jsonify(error=str(exc)), 404
    return jsonify(status="ok")


@app.route("/api/auth/end-impersonation", methods=["POST"])
@login_required
def auth_end_impersonation():
    if not session.get("impersonation_superadmin_id"):
        return jsonify(error="Aucune session de dépannage en cours."), 400
    session.clear()
    return jsonify(status="ok")


@app.route("/api/supadmin/audit-log")
@superadmin.login_required
def supadmin_audit_log():
    return jsonify(entries=superadmin.list_audit_log())


@app.route("/api/kb/articles/<slug>")
@login_required
def kb_get_article(slug):
    article = kb.get_article_by_slug(slug)
    if not article:
        return jsonify(error="Procédure introuvable."), 404
    return jsonify(article=article)


@app.route("/api/supadmin/security-events")
@superadmin.login_required
def supadmin_security_events():
    """Réinitialisations de mot de passe par PIN et blocages anti brute-force
    — pour repérer une activité suspecte (ex: beaucoup de blocages sur un
    même espace). Distinct du journal d'audit, qui trace les actions DU
    superadmin lui-même."""
    return jsonify(entries=security_events.list_recent_events())


@app.route("/api/supadmin/kb-articles", methods=["GET", "POST"])
@superadmin.login_required
def supadmin_kb_articles():
    if request.method == "GET":
        return jsonify(articles=kb.list_articles())

    body = request.get_json(silent=True) or {}
    try:
        new_id = kb.create_article(
            body.get("slug"), body.get("title"), body.get("content"),
            display_order=body.get("display_order") or 0,
        )
    except kb.KbError as exc:
        return jsonify(error=str(exc)), 400
    superadmin._log_action("kb_article_create", details=f"Article « {body.get('title')} » créé.")
    return jsonify(status="ok", id=new_id)


@app.route("/api/supadmin/kb-articles/<int:article_id>", methods=["PUT", "DELETE"])
@superadmin.login_required
def supadmin_kb_article_detail(article_id):
    if request.method == "DELETE":
        try:
            kb.delete_article(article_id)
        except kb.KbError as exc:
            return jsonify(error=str(exc)), 400
        superadmin._log_action("kb_article_delete", details=f"Article #{article_id} supprimé.")
        return jsonify(status="ok")

    body = request.get_json(silent=True) or {}
    try:
        kb.update_article(
            article_id, body.get("slug"), body.get("title"), body.get("content"),
            display_order=body.get("display_order") or 0,
        )
    except kb.KbError as exc:
        return jsonify(error=str(exc)), 400
    superadmin._log_action("kb_article_update", details=f"Article « {body.get('title')} » modifié.")
    return jsonify(status="ok")


@app.route("/api/supadmin/feedback/<int:feedback_id>/reply", methods=["POST"])
@superadmin.login_required
def supadmin_feedback_reply(feedback_id):
    body = request.get_json(silent=True) or {}
    subject = (body.get("subject") or "").strip()
    message_body = (body.get("body") or "").strip()
    if not subject or not message_body:
        return jsonify(error="Objet et message requis."), 400

    feedback = superadmin.get_feedback(feedback_id)
    if not feedback:
        return jsonify(error="Suggestion introuvable."), 404
    if not feedback["user_email"]:
        return jsonify(error="Cette suggestion n'a pas d'adresse e-mail associée."), 400

    if not system_mail.is_configured():
        return jsonify(error=(
            "SYSTEM_SMTP_* n'est pas configuré (ou incomplet) sur le serveur. "
            "Vérifie les 5 variables sur Coolify puis redéploie."
        )), 400

    try:
        system_mail.send_system_email(feedback["user_email"], subject, message_body)
    except system_mail.SystemMailError as exc:
        return jsonify(error=str(exc)), 400
    except Exception as exc:
        return jsonify(error=f"Erreur inattendue : {exc}"), 400

    superadmin.mark_feedback_replied(feedback_id)
    superadmin._log_action(
        "feedback_reply",
        details=f"Réponse envoyée à {feedback['user_email']} (suggestion #{feedback_id}).",
    )
    return jsonify(status="ok")


@app.route("/api/supadmin/feedback")
@superadmin.login_required
def supadmin_feedback():
    return jsonify(entries=superadmin.list_feedback())


@app.route("/api/supadmin/db-stats")
@superadmin.login_required
def supadmin_db_stats():
    return jsonify(superadmin.get_db_stats())


@app.route("/api/supadmin/purge", methods=["POST"])
@superadmin.login_required
def supadmin_purge():
    body = request.get_json(silent=True) or {}
    target = body.get("target")
    if target == "scheduled_search_results":
        count = superadmin.purge_stale_scheduled_results()
    elif target == "import_jobs":
        count = superadmin.purge_abandoned_import_jobs()
    else:
        return jsonify(error="Cible de purge invalide."), 400
    return jsonify(status="ok", deleted_count=count)


@app.route("/api/supadmin/test-email", methods=["POST"])
@superadmin.login_required
def supadmin_test_email():
    body = request.get_json(silent=True) or {}
    to_email = (body.get("to_email") or "").strip()
    if not to_email:
        return jsonify(error="Adresse e-mail requise."), 400
    if not system_mail.is_configured():
        return jsonify(error=(
            "SYSTEM_SMTP_* n'est pas configuré (ou incomplet) sur le serveur. "
            "Vérifie les 5 variables sur Coolify puis redéploie."
        )), 400
    try:
        system_mail.send_system_email(
            to_email,
            "Test — ClickProspect",
            "Ceci est un e-mail de test envoyé depuis la console superadmin. Si tu le reçois, la configuration SYSTEM_SMTP_* fonctionne.",
        )
    except system_mail.SystemMailError as exc:
        return jsonify(error=str(exc)), 400
    except Exception as exc:  # filet de sécurité : jamais de 500 brut sur ce test
        return jsonify(error=f"Erreur inattendue : {exc}"), 400
    return jsonify(status="ok")


init_db()
superadmin.ensure_bootstrap_superadmin()
scheduler.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000)
