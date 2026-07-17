"""
Envoi des campagnes (Option 3, onglet Envoi).

- Personnalisation du template (prénom, entreprise, lien d'avis, lien de désinscription).
- Vérifie le consentement (module consent) avant chaque envoi.
- Vérifie que le prospect est bien qualifié ou client au moment de la mise en
  file ET au moment de l'envoi effectif (garde-fou double).
- Respecte le quota quotidien de la campagne (quota_par_jour).
- Image optionnelle insérée dans le corps du message (Content-ID).
- Copie systématique de chaque envoi à l'expéditeur (BCC) : archive.
- Planification asynchrone via campaign_sends + process_due_sends().
"""
import html as html_module
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders

from app.db import get_db
from app import campaigns as campaigns_module
from app import consent as consent_module
from app import workspace_settings
from app import activity

ALLOWED_SEND_STATUTS = ("qualifie", "client")

# Délai minimum avant de pouvoir relancer une seconde fois le même prospect
# qualifié — évite de le solliciter trop souvent. Valeur fixe pour l'instant,
# pourra devenir un réglage par espace de travail si le besoin se confirme.
RELANCE_COOLDOWN_DAYS = 14


class SendError(Exception):
    pass


def get_relance_eligible_prospects(workspace_id):
    """Prospects qualifiés avec e-mail, séparés en deux groupes :
    - eligible : jamais relancés, ou dernière relance il y a plus de
      RELANCE_COOLDOWN_DAYS jours -> peuvent être relancés maintenant.
    - cooling_down : relancés récemment -> pas encore proposés, avec la date
      à partir de laquelle ils redeviennent éligibles (transparence, pas de
      surprise pour l'utilisateur qui se demande pourquoi ils manquent)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=RELANCE_COOLDOWN_DAYS)

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.id, p.nom_entreprise, p.email,
                       (SELECT max(cs.envoye_at) FROM campaign_sends cs
                        JOIN campaigns c ON c.id = cs.campaign_id
                        WHERE cs.prospect_id = p.id AND c.type = 'relance' AND cs.statut = 'envoye'
                       ) AS last_relance_at
                FROM prospects p
                WHERE p.workspace_id = %s AND p.statut = 'qualifie'
                  AND p.email IS NOT NULL AND p.email != ''
                ORDER BY p.nom_entreprise
                """,
                (workspace_id,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    eligible, cooling_down = [], []
    for pid, nom, email, last_relance_at in rows:
        entry = {"id": pid, "nom_entreprise": nom, "email": email, "last_relance_at": last_relance_at}
        if last_relance_at and last_relance_at > cutoff:
            entry["eligible_again_at"] = last_relance_at + timedelta(days=RELANCE_COOLDOWN_DAYS)
            cooling_down.append(entry)
        else:
            eligible.append(entry)
    return {"eligible": eligible, "cooling_down": cooling_down}


def _get_workspace_name(workspace_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM workspaces WHERE id = %s", (workspace_id,))
            row = cur.fetchone()
        return row[0] if row else ""
    finally:
        conn.close()


def render_template(content, prospect, workspace_id, google_profile_url=None):
    """{nom_entreprise} désigne le nom DE L'ESPACE DE TRAVAIL qui envoie la
    campagne (ex: signature "L'équipe {nom_entreprise}") — pas celui du
    prospect destinataire, sans quoi une campagne se signerait avec le nom de
    l'entreprise à qui elle est envoyée. {entreprise_prospect} reste
    disponible séparément pour personnaliser avec le nom du destinataire."""
    prenom = prospect.get("contact_prenom") or prospect.get("nom_entreprise") or ""
    nom_entreprise = _get_workspace_name(workspace_id)
    entreprise_prospect = prospect.get("nom_entreprise") or ""
    lien_desinscription = consent_module.build_unsubscribe_url(
        prospect["id"], prospect["_campaign_type"]
    )
    lien_avis_google = google_profile_url or ""

    return content.format(
        prenom=prenom,
        nom_entreprise=nom_entreprise,
        entreprise_prospect=entreprise_prospect,
        lien_avis_google=lien_avis_google,
        lien_desinscription=lien_desinscription,
        image="",
    )


def render_campaign_body_html(content, prospect, workspace_id, google_profile_url=None, has_image=False):
    prenom = html_module.escape(prospect.get("contact_prenom") or prospect.get("nom_entreprise") or "")
    nom_entreprise = html_module.escape(_get_workspace_name(workspace_id))
    entreprise_prospect = html_module.escape(prospect.get("nom_entreprise") or "")
    lien_avis_google = html_module.escape(google_profile_url or "")

    unsub_url = consent_module.build_unsubscribe_url(prospect["id"], prospect["_campaign_type"])
    lien_desinscription = f'<a href="{html_module.escape(unsub_url)}">se désinscrire</a>'

    image_tag = (
        '<br><img src="cid:campaign-image" alt="" style="max-width:100%; border-radius:8px; margin:12px 0;"><br>'
        if has_image else ""
    )

    escaped = html_module.escape(content)
    with_line_breaks = escaped.replace("\n", "<br>\n")

    rendered = with_line_breaks.format(
        prenom=prenom,
        nom_entreprise=nom_entreprise,
        entreprise_prospect=entreprise_prospect,
        lien_avis_google=lien_avis_google,
        lien_desinscription=lien_desinscription,
        image=image_tag,
    )

    if has_image and "{image}" not in content:
        rendered += image_tag

    return f'<div style="font-family:Arial,sans-serif; font-size:15px; line-height:1.55; color:#2b2b2b;">{rendered}</div>'


def render_plain_fallback(content, prospect, workspace_id, google_profile_url=None):
    rendered = render_template(content, prospect, workspace_id, google_profile_url)
    return rendered.replace("{image}", "").strip()


def _get_prospect(prospect_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, workspace_id, nom_entreprise, contact_prenom, email, statut FROM prospects WHERE id = %s",
                (prospect_id,),
            )
            row = cur.fetchone()
        if not row:
            return None
        cols = ["id", "workspace_id", "nom_entreprise", "contact_prenom", "email", "statut"]
        return dict(zip(cols, row))
    finally:
        conn.close()


def _get_prospects_statuts(workspace_id, prospect_ids):
    if not prospect_ids:
        return {}
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, statut, email FROM prospects WHERE workspace_id = %s AND id = ANY(%s)",
                (workspace_id, list(prospect_ids)),
            )
            rows = cur.fetchall()
        return {r[0]: {"statut": r[1], "email": r[2]} for r in rows}
    finally:
        conn.close()


def _get_campaign(campaign_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, workspace_id, type, nom, sujet, contenu, quota_par_jour, statut FROM campaigns WHERE id = %s",
                (campaign_id,),
            )
            row = cur.fetchone()
        if not row:
            return None
        cols = ["id", "workspace_id", "type", "nom", "sujet", "contenu", "quota_par_jour", "statut"]
        return dict(zip(cols, row))
    finally:
        conn.close()


def _count_sent_today(campaign_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) FROM campaign_sends
                WHERE campaign_id = %s
                  AND statut IN ('envoye', 'planifie')
                  AND coalesce(envoye_at, planifie_pour, created_at)::date = CURRENT_DATE
                """,
                (campaign_id,),
            )
            return cur.fetchone()[0]
    finally:
        conn.close()


def queue_send(campaign_id, prospect_ids, planifie_pour=None):
    campaign = _get_campaign(campaign_id)
    if not campaign:
        raise SendError("Campagne introuvable.")
    if campaign["statut"] != "active":
        raise SendError("La campagne n'est pas active.")

    already_today = _count_sent_today(campaign_id)
    quota = campaign["quota_par_jour"]

    statuts = _get_prospects_statuts(campaign["workspace_id"], prospect_ids)

    queued, skipped = [], []
    conn = get_db()
    try:
        for prospect_id in prospect_ids:
            if already_today + len(queued) >= quota:
                skipped.append({"prospect_id": prospect_id, "reason": "Quota quotidien de la campagne atteint."})
                continue

            info = statuts.get(prospect_id)
            if not info:
                skipped.append({"prospect_id": prospect_id, "reason": "Prospect introuvable dans cet espace de travail."})
                continue
            if info["statut"] not in ALLOWED_SEND_STATUTS:
                skipped.append({
                    "prospect_id": prospect_id,
                    "reason": "Le prospect n'est ni qualifié ni client — les campagnes sont réservées aux prospects validés.",
                })
                continue
            if not info["email"]:
                skipped.append({"prospect_id": prospect_id, "reason": "Le prospect n'a pas d'adresse e-mail."})
                continue

            allowed, reason = consent_module.can_send(prospect_id, campaign["type"])
            if not allowed:
                skipped.append({"prospect_id": prospect_id, "reason": reason})
                continue

            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO campaign_sends (campaign_id, prospect_id, canal, statut, planifie_pour)
                    VALUES (%s, %s, 'email', 'planifie', %s)
                    RETURNING id
                    """,
                    (campaign_id, prospect_id, planifie_pour),
                )
                send_id = cur.fetchone()[0]
            queued.append({"prospect_id": prospect_id, "send_id": send_id})
        conn.commit()
    finally:
        conn.close()

    return {"queued": queued, "skipped": skipped}


def preview_campaign_email(campaign_id, prospect_id, workspace_id):
    """Rendu du mail tel qu'il serait effectivement envoyé (sujet + corps HTML,
    avec l'image si présente), pour relecture avant un envoi réel — aucun
    e-mail n'est envoyé ici. Le prospect doit appartenir à l'espace de travail
    demandeur, comme partout ailleurs."""
    campaign = _get_campaign(campaign_id)
    if not campaign or campaign["workspace_id"] != workspace_id:
        raise SendError("Campagne introuvable.")
    prospect = _get_prospect(prospect_id)
    if not prospect or prospect["workspace_id"] != workspace_id:
        raise SendError("Prospect introuvable.")

    gbp = workspace_settings.get_google_business_profile(workspace_id)
    image = campaigns_module.get_campaign_image(workspace_id, campaign_id)
    prospect["_campaign_type"] = campaign["type"]

    subject = render_template(campaign["sujet"], prospect, workspace_id, gbp.get("profile_url"))
    body_html = render_campaign_body_html(
        campaign["contenu"], prospect, workspace_id, gbp.get("profile_url"), has_image=bool(image)
    )
    return {"subject": subject, "body_html": body_html, "to_email": prospect.get("email")}


def _send_via_smtp(smtp_creds, to_email, subject, body, attachments=None):
    if attachments:
        msg = MIMEMultipart()
        msg.attach(MIMEText(body, "plain", "utf-8"))
        for filename, content, mimetype in attachments:
            part = MIMEBase(*mimetype.split("/"))
            part.set_payload(content)
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
            msg.attach(part)
    else:
        msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = smtp_creds["from_email"]
    msg["To"] = to_email

    port = smtp_creds["port"]
    if port == 465:
        server = smtplib.SMTP_SSL(smtp_creds["host"], port, timeout=20)
    else:
        server = smtplib.SMTP(smtp_creds["host"], port, timeout=20)
        server.ehlo()
        if server.has_extn("STARTTLS"):
            server.starttls()
            server.ehlo()

    try:
        server.login(smtp_creds["username"], smtp_creds["password"])
        server.sendmail(smtp_creds["from_email"], [to_email], msg.as_string())
    finally:
        server.quit()


def _send_campaign_email(smtp_creds, to_email, subject, body_html, body_text, image=None, bcc=None):
    root = MIMEMultipart("related")
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(body_text, "plain", "utf-8"))
    alt.attach(MIMEText(body_html, "html", "utf-8"))
    root.attach(alt)

    if image:
        subtype = (image["mimetype"].split("/")[-1] or "jpeg").lower()
        img_part = MIMEImage(image["data"], _subtype=subtype)
        img_part.add_header("Content-ID", "<campaign-image>")
        img_part.add_header("Content-Disposition", "inline", filename=f"image.{subtype}")
        root.attach(img_part)

    root["Subject"] = subject
    root["From"] = smtp_creds["from_email"]
    root["To"] = to_email

    recipients = [to_email]
    if bcc and bcc.lower() != to_email.lower():
        recipients.append(bcc)

    port = smtp_creds["port"]
    if port == 465:
        server = smtplib.SMTP_SSL(smtp_creds["host"], port, timeout=20)
    else:
        server = smtplib.SMTP(smtp_creds["host"], port, timeout=20)
        server.ehlo()
        if server.has_extn("STARTTLS"):
            server.starttls()
            server.ehlo()

    try:
        server.login(smtp_creds["username"], smtp_creds["password"])
        server.sendmail(smtp_creds["from_email"], recipients, root.as_string())
    finally:
        server.quit()


def _process_one_send(conn, send_row):
    send_id, campaign_id, prospect_id = send_row

    campaign = _get_campaign(campaign_id)
    prospect = _get_prospect(prospect_id)

    if not campaign or not prospect:
        _mark_send(conn, send_id, "echec", "Campagne ou prospect introuvable.")
        return

    if not prospect.get("email"):
        _mark_send(conn, send_id, "echec", "Le prospect n'a pas d'adresse e-mail.")
        return

    if prospect.get("statut") not in ALLOWED_SEND_STATUTS:
        _mark_send(
            conn, send_id, "echec",
            "Le prospect n'est plus qualifié/client au moment de l'envoi (statut modifié depuis la mise en file).",
        )
        return

    allowed, reason = consent_module.can_send(prospect_id, campaign["type"])
    if not allowed:
        _mark_send(conn, send_id, "echec", f"Consentement refusé au moment de l'envoi : {reason}")
        return

    smtp_creds = workspace_settings.get_smtp_credentials_for_sending(campaign["workspace_id"], require_verified=True)
    if not smtp_creds:
        _mark_send(
            conn, send_id, "echec",
            "Configuration SMTP absente ou non vérifiée — testez l'envoi depuis Paramètres avant de lancer une campagne.",
        )
        return

    gbp = workspace_settings.get_google_business_profile(campaign["workspace_id"])
    image = campaigns_module.get_campaign_image(None, campaign_id)

    prospect["_campaign_type"] = campaign["type"]
    try:
        subject = render_template(campaign["sujet"], prospect, campaign["workspace_id"], gbp.get("profile_url"))
        body_html = render_campaign_body_html(
            campaign["contenu"], prospect, campaign["workspace_id"], gbp.get("profile_url"), has_image=bool(image)
        )
        body_text = render_plain_fallback(campaign["contenu"], prospect, campaign["workspace_id"], gbp.get("profile_url"))
        _send_campaign_email(
            smtp_creds, prospect["email"], subject, body_html, body_text,
            image=image, bcc=smtp_creds["from_email"],
        )
    except Exception as exc:  # noqa: BLE001
        _mark_send(conn, send_id, "echec", str(exc)[:500])
        return

    _mark_send(conn, send_id, "envoye", None)
    activity.log_event(
        prospect["id"], campaign["workspace_id"], "campagne_envoyee",
        f"Campagne « {campaign['nom']} » envoyée.",
    )


def _mark_send(conn, send_id, statut, error_message):
    with conn.cursor() as cur:
        if statut == "envoye":
            cur.execute(
                "UPDATE campaign_sends SET statut = %s, envoye_at = now() WHERE id = %s",
                (statut, send_id),
            )
        else:
            cur.execute(
                "UPDATE campaign_sends SET statut = %s WHERE id = %s",
                (statut, send_id),
            )
    conn.commit()


def process_due_sends(batch_size=20, stale_after_minutes=5):
    conn = get_db()
    processed = 0
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE campaign_sends SET statut = 'planifie', locked_at = NULL
                WHERE statut = 'en_cours' AND locked_at < now() - (%s || ' minutes')::interval
                """,
                (stale_after_minutes,),
            )
        conn.commit()

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, campaign_id, prospect_id FROM campaign_sends
                WHERE statut = 'planifie'
                  AND (planifie_pour IS NULL OR planifie_pour <= now())
                ORDER BY created_at
                LIMIT %s
                FOR UPDATE SKIP LOCKED
                """,
                (batch_size,),
            )
            rows = cur.fetchall()
            for row in rows:
                cur.execute("UPDATE campaign_sends SET statut = 'en_cours', locked_at = now() WHERE id = %s", (row[0],))
        conn.commit()

        for row in rows:
            _process_one_send(conn, row)
            processed += 1
    finally:
        conn.close()

    return processed


def get_unsubscribe_rate(campaign_id):
    """Parmi les prospects à qui cette campagne a été effectivement envoyée,
    quelle proportion s'est désabonnée depuis du type de communication
    concerné (avis/publicitaire/newsletter). Le consentement est suivi par
    (prospect, type) et non par campagne individuelle (cf. schema.sql) : deux
    campagnes du même type partagent le même opt-out — cohérent avec le
    fonctionnement RGPD actuel (désinscription globale par type de
    communication, pas campagne par campagne)."""
    campaign = _get_campaign(campaign_id)
    if not campaign:
        return None
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(DISTINCT prospect_id) FROM campaign_sends WHERE campaign_id = %s AND statut = 'envoye'",
                (campaign_id,),
            )
            sent_count = cur.fetchone()[0]
            if sent_count == 0:
                return {"sent": 0, "unsubscribed": 0, "rate": None}
            cur.execute(
                """
                SELECT count(DISTINCT cs.prospect_id)
                FROM campaign_sends cs
                JOIN consents c ON c.prospect_id = cs.prospect_id AND c.type = %s AND c.statut = 'opt_out'
                WHERE cs.campaign_id = %s AND cs.statut = 'envoye'
                """,
                (campaign["type"], campaign_id),
            )
            unsub_count = cur.fetchone()[0]
        return {"sent": sent_count, "unsubscribed": unsub_count, "rate": round(100 * unsub_count / sent_count, 1)}
    finally:
        conn.close()


FINALITES = {
    "avis": "Demande d'avis client sur une fiche Google Business",
    "publicitaire": "Communication publicitaire (offres, actualités commerciales)",
    "newsletter": "Newsletter d'actualités",
}
BASES_LEGALES = {
    "avis": "Intérêt légitime (client déjà servi)",
    "publicitaire": "Consentement (opt-in)",
    "newsletter": "Consentement (opt-in)",
}


def registre_traitement_csv(workspace_id):
    """Génère le registre de traitement RGPD (une ligne par campagne) au
    format CSV, pour export depuis Paramètres — évite d'avoir à le
    reconstituer à la main en cas de contrôle."""
    import csv
    import io

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.id, c.nom, c.type, c.created_at,
                       count(DISTINCT cs.prospect_id) FILTER (WHERE cs.statut = 'envoye') AS destinataires
                FROM campaigns c
                LEFT JOIN campaign_sends cs ON cs.campaign_id = c.id
                WHERE c.workspace_id = %s
                GROUP BY c.id, c.nom, c.type, c.created_at
                ORDER BY c.created_at DESC
                """,
                (workspace_id,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "Campagne", "Type", "Finalité", "Base légale", "Créée le",
        "Nombre de destinataires effectifs", "Durée de conservation des preuves de consentement",
    ])
    for campaign_id, nom, type_, created_at, destinataires in rows:
        writer.writerow([
            nom, type_, FINALITES.get(type_, type_), BASES_LEGALES.get(type_, ""),
            created_at, destinataires,
            "Voir politique de conservation de l'espace de travail (Paramètres)",
        ])
    return buf.getvalue()


class EmailSendError(Exception):
    pass


def send_email(workspace_id, to_email, subject, body, attachments=None):
    smtp_creds = workspace_settings.get_smtp_credentials_for_sending(workspace_id)
    if not smtp_creds:
        raise EmailSendError("Aucune configuration SMTP pour cet espace de travail.")
    _send_via_smtp(smtp_creds, to_email, subject, body, attachments=attachments)


class SmtpTestError(Exception):
    pass


def cancel_send(campaign_id, send_id):
    """Annule un envoi planifié individuel. N'annule QUE s'il est encore au
    statut 'planifie' — mise à jour conditionnelle atomique pour éviter une
    course avec process_due_sends() qui pourrait être en train de le traiter
    au même moment (SELECT ... FOR UPDATE SKIP LOCKED passe alors à la ligne
    suivante, donc pas de double-traitement). Renvoie True si annulé, False
    si l'envoi n'était plus annulable (déjà en cours, envoyé ou en échec)."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE campaign_sends SET statut = 'annule'
                WHERE id = %s AND campaign_id = %s AND statut = 'planifie'
                RETURNING id
                """,
                (send_id, campaign_id),
            )
            updated = cur.fetchone()
        conn.commit()
        return updated is not None
    finally:
        conn.close()


def cancel_all_planned(campaign_id):
    """Annule en masse tous les envois encore planifiés pour une campagne —
    filet de sécurité en cas d'erreur (mauvaise sélection, mauvais contenu...).
    Ne touche jamais les envois déjà en cours, envoyés ou en échec. Renvoie
    le nombre d'envois annulés."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE campaign_sends SET statut = 'annule'
                WHERE campaign_id = %s AND statut = 'planifie'
                RETURNING id
                """,
                (campaign_id,),
            )
            cancelled = cur.fetchall()
        conn.commit()
        return len(cancelled)
    finally:
        conn.close()


def send_smtp_test(workspace_id):
    smtp_creds = workspace_settings.get_smtp_credentials_for_sending(workspace_id)
    if not smtp_creds:
        raise SmtpTestError("Aucune configuration SMTP enregistrée pour cet espace de travail.")

    subject = "ClickProspect — test de configuration SMTP"
    body = (
        "Cet e-mail confirme que votre configuration SMTP fonctionne correctement.\n\n"
        "Vous pouvez maintenant lancer des campagnes depuis ClickProspect."
    )
    try:
        _send_via_smtp(smtp_creds, smtp_creds["from_email"], subject, body)
    except Exception as exc:  # noqa: BLE001
        # Cas fréquent avec Microsoft 365/Outlook : l'authentification SMTP
        # classique est désactivée par défaut côté Microsoft (pas un problème
        # d'identifiants). On reconnaît ce message précis pour donner une
        # explication actionnable plutôt que l'erreur technique brute.
        if "smtpclientauthentication is disabled" in str(exc).lower():
            raise SmtpTestError(
                "Ce compte Microsoft 365/Outlook a l'authentification SMTP désactivée par défaut "
                "(réglage de sécurité côté Microsoft, pas un souci d'identifiants). Un administrateur "
                "doit l'activer dans le centre d'administration Microsoft 365 (« Authentification SMTP "
                "authentifiée »). Voir https://aka.ms/smtp_auth_disabled — ou utilisez une autre boîte mail."
            ) from exc
        raise SmtpTestError(f"Échec de l'envoi de test : {exc}") from exc

    workspace_settings.mark_smtp_verified(workspace_id)
