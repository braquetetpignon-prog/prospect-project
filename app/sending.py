"""
Envoi des campagnes (Option 3, onglet Envoi).

- Personnalisation du template (prénom, entreprise, lien d'avis, lien de désinscription).
- Vérifie le consentement (module consent) avant chaque envoi.
- Respecte le quota quotidien de la campagne (quota_par_jour).
- Planification : les envois sont mis en file dans campaign_sends (statut 'planifie'),
  puis dispatchés par process_due_sends(), appelée périodiquement par un planificateur
  en arrière-plan (voir scheduler.py). Utilise SELECT ... FOR UPDATE SKIP LOCKED pour
  rester correct même avec plusieurs workers gunicorn en parallèle (pas de double-envoi).
"""
import smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText

from app.db import get_db
from app import consent as consent_module
from app import workspace_settings


class SendError(Exception):
    pass


def render_template(content, prospect, workspace_id, google_profile_url=None):
    prenom = prospect.get("contact_prenom") or prospect.get("nom_entreprise") or ""
    nom_entreprise = prospect.get("nom_entreprise") or ""
    lien_desinscription = consent_module.build_unsubscribe_url(
        prospect["id"], prospect["_campaign_type"]
    )
    lien_avis_google = google_profile_url or ""

    return content.format(
        prenom=prenom,
        nom_entreprise=nom_entreprise,
        lien_avis_google=lien_avis_google,
        lien_desinscription=lien_desinscription,
    )


def _get_prospect(prospect_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, workspace_id, nom_entreprise, contact_prenom, email FROM prospects WHERE id = %s",
                (prospect_id,),
            )
            row = cur.fetchone()
        if not row:
            return None
        cols = ["id", "workspace_id", "nom_entreprise", "contact_prenom", "email"]
        return dict(zip(cols, row))
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
    """Met en file un envoi pour chaque prospect. Vérifie le consentement et le quota
    quotidien de la campagne avant d'ajouter chaque entrée. Retourne un rapport
    {queued: [...], skipped: [{prospect_id, reason}, ...]}."""
    campaign = _get_campaign(campaign_id)
    if not campaign:
        raise SendError("Campagne introuvable.")
    if campaign["statut"] != "active":
        raise SendError("La campagne n'est pas active.")

    already_today = _count_sent_today(campaign_id)
    quota = campaign["quota_par_jour"]

    queued, skipped = [], []
    conn = get_db()
    try:
        for prospect_id in prospect_ids:
            if already_today + len(queued) >= quota:
                skipped.append({"prospect_id": prospect_id, "reason": "Quota quotidien de la campagne atteint."})
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


def _send_via_smtp(smtp_creds, to_email, subject, body):
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

    allowed, reason = consent_module.can_send(prospect_id, campaign["type"])
    if not allowed:
        _mark_send(conn, send_id, "echec", f"Consentement refusé au moment de l'envoi : {reason}")
        return

    smtp_creds = workspace_settings.get_smtp_credentials_for_sending(campaign["workspace_id"])
    if not smtp_creds:
        _mark_send(conn, send_id, "echec", "Aucune configuration SMTP pour cet espace de travail.")
        return

    gbp = workspace_settings.get_google_business_profile(campaign["workspace_id"])

    prospect["_campaign_type"] = campaign["type"]
    try:
        subject = render_template(campaign["sujet"], prospect, campaign["workspace_id"], gbp.get("profile_url"))
        body = render_template(campaign["contenu"], prospect, campaign["workspace_id"], gbp.get("profile_url"))
        _send_via_smtp(smtp_creds, prospect["email"], subject, body)
    except Exception as exc:  # noqa: BLE001 — on veut consigner l'échec, pas planter le worker
        _mark_send(conn, send_id, "echec", str(exc)[:500])
        return

    _mark_send(conn, send_id, "envoye", None)


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
    """Traite les envois planifiés arrivés à échéance. Sûr avec plusieurs workers
    gunicorn en parallèle : FOR UPDATE SKIP LOCKED évite qu'un même envoi soit
    traité deux fois. Récupère aussi les envois restés bloqués en 'en_cours' trop
    longtemps (ex: worker redémarré en pleine tâche), pour qu'ils soient retentés
    plutôt que perdus."""
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
            # marquage immédiat pour éviter qu'un autre worker ne reprenne ces lignes
            for row in rows:
                cur.execute("UPDATE campaign_sends SET statut = 'en_cours', locked_at = now() WHERE id = %s", (row[0],))
        conn.commit()

        for row in rows:
            _process_one_send(conn, row)
            processed += 1
    finally:
        conn.close()

    return processed
