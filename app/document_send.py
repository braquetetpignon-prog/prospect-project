"""
Envoi ponctuel d'un fichier (devis) à un prospect "en attente" ou "client"
(client), depuis sa fiche. Contrainte forte, voulue par Alexis : ni le
fichier ni le contenu de l'e-mail ne sont jamais stockés côté serveur —
ni sur disque, ni en base, pas même temporairement. Le fichier est traité
en mémoire pour la durée de l'envoi puis jeté avec la requête. Seul un
événement de traçabilité (nom de fichier, taille, date, expéditeur) reste
sur la fiche prospect via prospect_activity — voir activity.py.

Conséquence assumée de ce choix : en cas de litige ("vous ne m'avez jamais
envoyé ce devis"), seul ce rapport fait foi — il n'existe aucun moyen de
reproduire le contenu exact envoyé après coup.
"""
from app.db import get_db
from app import sending, workspace_settings, activity

# Volontairement distinct de sending.ALLOWED_SEND_STATUTS (qui inclut aussi
# "qualifie") : un devis n'a de sens qu'à partir du moment où le prospect
# est en attente d'une proposition concrète ou déjà client. Ne PAS
# réutiliser/dupliquer cette liste ailleurs sans passer par cette constante
# (voir erreurs-connues-clickprospect.md — bug de désynchronisation déjà
# rencontré une fois sur un principe similaire).
DEVIS_SEND_STATUTS = ("en_attente", "client")

MAX_FILE_SIZE_BYTES = 700 * 1024  # 700 Ko — relèvé de 200 à 700 Ko : les devis embarquent les CGV obligatoires, plus volumineux qu'un simple devis nu
DAILY_SEND_LIMIT = 20  # par utilisateur et par jour, anti-abus — ajustable
PDF_MAGIC = b"%PDF-"


class DocumentSendError(Exception):
    """Erreur métier — le message est destiné à être renvoyé tel quel à
    l'utilisateur, jamais de détail technique interne dedans."""


def _validate_pdf(file_bytes, filename):
    if not file_bytes:
        raise DocumentSendError("Fichier vide.")
    if len(file_bytes) > MAX_FILE_SIZE_BYTES:
        raise DocumentSendError(f"Fichier trop volumineux ({len(file_bytes) // 1024} Ko, maximum 700 Ko).")
    # Validation par signature binaire, jamais par extension ni Content-Type
    # (trivialement falsifiables côté client) — même principe déjà appliqué
    # aux images de campagne dans campaign_image.py.
    if not file_bytes.startswith(PDF_MAGIC):
        raise DocumentSendError("Le fichier n'est pas un PDF valide.")
    filename = (filename or "document.pdf").strip()
    if not filename.lower().endswith(".pdf"):
        filename += ".pdf"
    return filename


def count_sends_today(workspace_id, user_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) FROM prospect_activity
                WHERE workspace_id = %s AND user_id = %s AND event_type = 'fichier_envoye'
                  AND created_at >= date_trunc('day', now())
                """,
                (workspace_id, user_id),
            )
            return cur.fetchone()[0]
    finally:
        conn.close()


def send_document(workspace_id, user_id, prospect, file_bytes, filename, message):
    """`prospect` : dict déjà chargé et déjà vérifié appartenir au bon
    workspace par l'appelant (voir la route dans main.py) — cette fonction
    ne recharge pas le prospect elle-même."""
    if prospect.get("statut") not in DEVIS_SEND_STATUTS:
        raise DocumentSendError(
            "Ce prospect n'est pas éligible à l'envoi de fichier "
            "(statut « en attente » ou « client » requis)."
        )
    if not prospect.get("email"):
        raise DocumentSendError("Ce prospect n'a pas d'adresse e-mail renseignée.")

    if count_sends_today(workspace_id, user_id) >= DAILY_SEND_LIMIT:
        raise DocumentSendError(
            f"Limite de {DAILY_SEND_LIMIT} envois de fichier atteinte pour aujourd'hui — réessayez demain."
        )

    filename = _validate_pdf(file_bytes, filename)

    subject = f"Document — {prospect.get('nom_entreprise') or ''}".strip(" —")
    body = (message or "").strip() or "Veuillez trouver le document joint."

    try:
        sending.send_email(
            workspace_id,
            prospect["email"],
            subject,
            body,
            attachments=[(filename, file_bytes, "application/pdf")],
            bcc_sender=True,
            require_verified=True,
        )
    except sending.EmailSendError as exc:
        raise DocumentSendError(str(exc)) from exc

    size_kb = max(1, len(file_bytes) // 1024)
    activity.log_event(
        prospect["id"], workspace_id, "fichier_envoye",
        f"Fichier envoyé : {filename} ({size_kb} Ko).",
        user_id=user_id,
    )
