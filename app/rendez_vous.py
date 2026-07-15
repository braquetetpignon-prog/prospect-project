"""
Rendez-vous (Option 3+ : calendrier partagé).

- Visible par tous les membres de l'espace de travail.
- Chaque utilisateur ne modifie que ses propres rendez-vous.
- Un administrateur peut modifier ceux des autres — dans ce cas, le
  propriétaire d'origine reçoit un e-mail de notification du changement.
- Export .ics au choix : téléchargement ou envoi automatique par e-mail
  (au choix de l'utilisateur à chaque fois).
"""
import uuid
from datetime import timedelta

from app.db import get_db
from app import sending
from app import activity


class RdvError(Exception):
    pass


class RdvPermissionError(RdvError):
    pass


def _get_owner_and_titre(conn, rdv_id, workspace_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT user_id, titre, date_heure FROM rendez_vous WHERE id = %s AND workspace_id = %s",
            (rdv_id, workspace_id),
        )
        return cur.fetchone()


def create_rendez_vous(workspace_id, user_id, titre, date_heure, duree_minutes=30, prospect_id=None, notes=None):
    if not titre or not date_heure:
        raise RdvError("titre et date_heure sont requis.")
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO rendez_vous (workspace_id, user_id, prospect_id, titre, date_heure, duree_minutes, notes)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (workspace_id, user_id, prospect_id, titre, date_heure, duree_minutes, notes),
            )
            rdv_id = cur.fetchone()[0]
        conn.commit()
    finally:
        conn.close()

    if prospect_id:
        activity.log_event(
            prospect_id, workspace_id, "rdv_planifie",
            f"Rendez-vous « {titre} » planifié pour le {date_heure}.",
        )
    return rdv_id


def list_rendez_vous(workspace_id, start=None, end=None):
    conditions = ["rv.workspace_id = %s"]
    params = [workspace_id]
    if start:
        conditions.append("rv.date_heure >= %s")
        params.append(start)
    if end:
        conditions.append("rv.date_heure <= %s")
        params.append(end)

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT rv.id, rv.titre, rv.date_heure, rv.duree_minutes, rv.notes,
                       rv.user_id, u.email AS user_email, rv.prospect_id, p.nom_entreprise
                FROM rendez_vous rv
                JOIN users u ON u.id = rv.user_id
                LEFT JOIN prospects p ON p.id = rv.prospect_id
                WHERE {' AND '.join(conditions)}
                ORDER BY rv.date_heure
                """,
                params,
            )
            rows = cur.fetchall()
        cols = ["id", "titre", "date_heure", "duree_minutes", "notes", "user_id", "user_email",
                "prospect_id", "prospect_nom"]
        return [dict(zip(cols, r)) for r in rows]
    finally:
        conn.close()


def get_rendez_vous(rdv_id, workspace_id):
    results = [r for r in list_rendez_vous(workspace_id) if r["id"] == rdv_id]
    return results[0] if results else None


def _check_permission(conn, rdv_id, workspace_id, requesting_user_id, requesting_role):
    owner = _get_owner_and_titre(conn, rdv_id, workspace_id)
    if not owner:
        raise RdvError("Rendez-vous introuvable.")
    owner_id, titre, date_heure = owner
    is_owner = owner_id == requesting_user_id
    is_admin = requesting_role == "admin"
    if not is_owner and not is_admin:
        raise RdvPermissionError("Tu ne peux modifier que tes propres rendez-vous.")
    return owner_id, titre, date_heure, is_owner


def update_rendez_vous(rdv_id, workspace_id, requesting_user_id, requesting_role, fields):
    conn = get_db()
    try:
        owner_id, old_titre, old_date, is_owner = _check_permission(
            conn, rdv_id, workspace_id, requesting_user_id, requesting_role
        )

        allowed = {"titre", "date_heure", "duree_minutes", "notes", "prospect_id"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            raise RdvError("Aucun champ à mettre à jour.")

        set_clause = ", ".join(f"{k} = %s" for k in updates) + ", updated_at = now()"
        values = list(updates.values()) + [rdv_id, workspace_id]
        with conn.cursor() as cur:
            cur.execute(f"UPDATE rendez_vous SET {set_clause} WHERE id = %s AND workspace_id = %s", values)
        conn.commit()

        # Notification si un admin modifie le rendez-vous de quelqu'un d'autre
        if not is_owner:
            _notify_owner_of_change(workspace_id, owner_id, updates.get("titre", old_titre))
    finally:
        conn.close()


def delete_rendez_vous(rdv_id, workspace_id, requesting_user_id, requesting_role):
    conn = get_db()
    try:
        owner_id, titre, date_heure, is_owner = _check_permission(
            conn, rdv_id, workspace_id, requesting_user_id, requesting_role
        )
        with conn.cursor() as cur:
            cur.execute("DELETE FROM rendez_vous WHERE id = %s AND workspace_id = %s", (rdv_id, workspace_id))
        conn.commit()
        if not is_owner:
            _notify_owner_of_change(workspace_id, owner_id, titre, deleted=True)
    finally:
        conn.close()


def _notify_owner_of_change(workspace_id, owner_user_id, titre, deleted=False):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT email FROM users WHERE id = %s", (owner_user_id,))
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return
    owner_email = row[0]
    action = "supprimé" if deleted else "modifié"
    try:
        sending.send_email(
            workspace_id, owner_email,
            f"Votre rendez-vous « {titre} » a été {action}",
            f"Un administrateur a {action} votre rendez-vous « {titre} ». "
            f"Connectez-vous à ClickProspect pour vérifier votre planning.",
        )
    except sending.EmailSendError:
        pass  # notification best-effort : ne bloque jamais l'action elle-même


# --- Export .ics -----------------------------------------------------------

def _escape_ics(text):
    return (text or "").replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")


def generate_ics(rdv):
    dt_start = rdv["date_heure"]
    dt_end = dt_start + timedelta(minutes=rdv["duree_minutes"] or 30)
    uid = f"clickprospect-rdv-{rdv['id']}-{uuid.uuid4().hex[:8]}@clickprospect.fr"

    def fmt(dt):
        return dt.strftime("%Y%m%dT%H%M%SZ")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//ClickProspect//FR",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{fmt(dt_start)}",
        f"DTSTART:{fmt(dt_start)}",
        f"DTEND:{fmt(dt_end)}",
        f"SUMMARY:{_escape_ics(rdv['titre'])}",
    ]
    if rdv.get("notes"):
        lines.append(f"DESCRIPTION:{_escape_ics(rdv['notes'])}")
    if rdv.get("prospect_nom"):
        lines.append(f"LOCATION:{_escape_ics(rdv['prospect_nom'])}")
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(lines)


def send_ics_by_email(workspace_id, rdv, to_email):
    ics_content = generate_ics(rdv)
    sending.send_email(
        workspace_id, to_email,
        f"Rendez-vous : {rdv['titre']}",
        f"Voici le fichier .ics de votre rendez-vous « {rdv['titre']} », à importer dans votre agenda.",
        attachments=[("rendez-vous.ics", ics_content.encode("utf-8"), "text/calendar")],
    )
