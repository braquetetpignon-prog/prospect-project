"""
Import CSV des prospects.

Flux :
1. POST /api/imports/preview   -> upload du fichier, détection des colonnes, stockage temporaire
2. POST /api/imports/<id>/start -> reçoit le mapping colonne CSV -> champ prospect, lance le traitement
3. GET  /api/imports/<id>/status -> progression (pour un sondage/polling côté interface)
4. GET  /api/imports/<id>/errors -> rapport d'erreurs ligne par ligne

Traitement asynchrone géré par un thread en arrière-plan (pas de dépendance à Redis/Celery :
volume attendu modeste au lancement). L'état du job est toujours lu/écrit en base PostgreSQL,
ce qui fonctionne correctement même avec plusieurs workers gunicorn.
"""
import csv
import io
import json
import re
import threading
from datetime import datetime, timezone

from app.db import get_db

# Colonnes acceptées comme cible de mapping, et contrainte associée.
PROSPECT_FIELDS = {
    "nom_entreprise": {"required": True, "max_length": 255},
    "contact_prenom": {"required": False, "max_length": 100},
    "contact_nom": {"required": False, "max_length": 100},
    "siren": {"required": False, "pattern": re.compile(r"^\d{9}$")},
    "siret": {"required": False, "pattern": re.compile(r"^\d{14}$")},
    "naf_code": {"required": False, "max_length": 10},
    "adresse": {"required": False, "max_length": 500},
    "batiment": {"required": False, "max_length": 100},
    "etage": {"required": False, "max_length": 50},
    "code_postal": {"required": False, "pattern": re.compile(r"^\d{5}$")},
    "ville": {"required": False, "max_length": 255},
    "telephone": {"required": False, "max_length": 30},
    "email": {"required": False, "pattern": re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")},
    "site_web": {"required": False, "max_length": 500},
    # Pas de max_length : notes peut légitimement combiner plusieurs colonnes
    # sources lors d'un import CSV (voir _process_job / NOTES_FIELD).
    "notes": {"required": False},
}

# Champ recevant, en cas d'import CSV, la combinaison de toutes les colonnes
# sources qui lui sont mappées (contrairement aux autres champs, où seule la
# dernière colonne mappée l'emporte — concaténer plusieurs numéros de
# téléphone ou e-mails dans le même champ n'aurait pas de sens, alors que
# regrouper plusieurs informations complémentaires dans les notes en a un).
NOTES_FIELD = "notes"

# Caractères déclenchant une formule dans Excel/Sheets à l'ouverture d'un export.
CSV_INJECTION_LEAD_CHARS = ("=", "+", "-", "@")

CHUNK_SIZE = 500  # lignes traitées par lot avant mise à jour de la progression


def _sniff_delimiter(text):
    """Détecte automatiquement le séparateur du CSV (virgule, point-virgule
    ou tabulation). Nécessaire car les exports Excel en français utilisent
    le point-virgule par défaut (la virgule étant déjà le séparateur
    décimal) — sans cette détection, csv.reader utilisait toujours la
    virgule et lisait chaque ligne entière comme une seule colonne, ce qui
    faisait échouer silencieusement le mapping (toutes les colonnes sauf la
    première restaient vides à l'import).
    Repli sur la virgule (comportement Python standard) si la détection
    échoue, par exemple sur un fichier à une seule colonne."""
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        return dialect.delimiter
    except csv.Error:
        return ","


def sanitize_cell(value):
    """Neutralise une valeur pouvant être interprétée comme une formule
    par un tableur (protection contre l'injection CSV)."""
    if value is None:
        return value
    value = value.strip()
    if value.startswith(CSV_INJECTION_LEAD_CHARS):
        return "'" + value
    return value


def validate_row(mapped_row):
    """Valide une ligne déjà mappée sur les champs prospect.
    Retourne (is_blocking_error, list_of_messages)."""
    messages = []
    blocking = False

    nom = mapped_row.get("nom_entreprise", "")
    if not nom:
        return True, ["nom_entreprise manquant (obligatoire)"]

    for field, value in mapped_row.items():
        if field not in PROSPECT_FIELDS or not value:
            continue
        rules = PROSPECT_FIELDS[field]
        max_len = rules.get("max_length")
        if max_len and len(value) > max_len:
            messages.append(f"{field} dépasse {max_len} caractères, tronqué")
            mapped_row[field] = value[:max_len]
        pattern = rules.get("pattern")
        if pattern and not pattern.match(value):
            messages.append(f"{field} au format inattendu ({value!r}), importé tel quel")

    return blocking, messages


def parse_preview(file_bytes, max_sample_rows=5):
    """Lit l'en-tête et un échantillon de lignes pour construire l'interface de mapping."""
    text = file_bytes.decode("utf-8-sig", errors="replace")
    delimiter = _sniff_delimiter(text)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = list(reader)
    if not rows:
        raise ValueError("Fichier CSV vide")
    header = [h.strip() for h in rows[0]]
    sample_rows = rows[1:1 + max_sample_rows]
    total_rows = len(rows) - 1
    return header, sample_rows, total_rows


def create_import_job(workspace_id, filename, file_bytes, header, total_rows):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO import_jobs (workspace_id, filename, status, raw_content, total_rows)
                VALUES (%s, %s, 'mapping', %s, %s)
                RETURNING id
                """,
                (workspace_id, filename, file_bytes, total_rows),
            )
            job_id = cur.fetchone()[0]
        conn.commit()
        return job_id
    finally:
        conn.close()


def get_job(job_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, workspace_id, filename, status, mapping, total_rows,
                       processed_rows, imported_count, error_count, duplicate_count,
                       created_at, started_at, finished_at
                FROM import_jobs WHERE id = %s
                """,
                (job_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            cols = ["id", "workspace_id", "filename", "status", "mapping", "total_rows",
                    "processed_rows", "imported_count", "error_count", "duplicate_count",
                    "created_at", "started_at", "finished_at"]
            return dict(zip(cols, row))
    finally:
        conn.close()


def get_job_errors(job_id, page=1, page_size=100):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT row_number, severity, message, raw_row
                FROM import_errors
                WHERE job_id = %s
                ORDER BY row_number
                LIMIT %s OFFSET %s
                """,
                (job_id, page_size, (page - 1) * page_size),
            )
            rows = cur.fetchall()
            return [
                {"row_number": r[0], "severity": r[1], "message": r[2], "raw_row": r[3]}
                for r in rows
            ]
    finally:
        conn.close()


def start_import(job_id, mapping):
    """Valide le mapping, marque le job comme prêt, et lance le traitement en arrière-plan."""
    if "nom_entreprise" not in mapping.values():
        raise ValueError("Le champ 'nom_entreprise' doit être associé à une colonne du fichier.")

    unknown_targets = set(mapping.values()) - set(PROSPECT_FIELDS.keys())
    if unknown_targets:
        raise ValueError(f"Champs cibles inconnus : {', '.join(unknown_targets)}")

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE import_jobs
                SET mapping = %s, status = 'pending', started_at = now()
                WHERE id = %s AND status = 'mapping'
                RETURNING id
                """,
                (json.dumps(mapping), job_id),
            )
            updated = cur.fetchone()
        conn.commit()
        if not updated:
            raise ValueError("Ce job a déjà été démarré ou n'existe pas.")
    finally:
        conn.close()

    thread = threading.Thread(target=_process_job, args=(job_id, mapping), daemon=True)
    thread.start()


def _find_duplicate(conn, workspace_id, mapped):
    """Recherche un prospect déjà en base pour cet espace de travail : d'abord
    par SIRET (le plus fiable), sinon par nom d'entreprise + ville (correspondance
    insensible à la casse). Renvoie l'id du doublon trouvé, ou None."""
    siret = mapped.get("siret")
    if siret:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM prospects WHERE workspace_id = %s AND siret = %s LIMIT 1",
                (workspace_id, siret),
            )
            row = cur.fetchone()
            if row:
                return row[0]

    nom = mapped.get("nom_entreprise")
    ville = mapped.get("ville")
    if nom and ville:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id FROM prospects
                WHERE workspace_id = %s AND lower(nom_entreprise) = lower(%s) AND lower(ville) = lower(%s)
                LIMIT 1
                """,
                (workspace_id, nom, ville),
            )
            row = cur.fetchone()
            if row:
                return row[0]
    return None


def _build_mapped_row(header, raw_row, field_to_indexes):
    """Construit le dict {champ_prospect: valeur} pour une ligne.

    Pour la plupart des champs, si plusieurs colonnes du fichier ont été
    mappées par erreur (ou par choix) sur la même cible, seule la dernière
    valeur non vide est conservée — concaténer par exemple deux numéros de
    téléphone n'aurait pas de sens.

    Exception : le champ `notes`, où combiner plusieurs colonnes sources a
    du sens (ex: Secteur, Action, Accroche personnalisée réunis en un seul
    endroit). Chaque valeur non vide est alors préfixée du nom de sa colonne
    d'origine et les lignes sont jointes par un saut de ligne, dans l'ordre
    des colonnes du fichier."""
    mapped = {}
    for field, indexes in field_to_indexes.items():
        if field == NOTES_FIELD:
            parts = []
            for idx in indexes:
                if idx >= len(raw_row):
                    continue
                value = sanitize_cell(raw_row[idx])
                if value:
                    parts.append(f"{header[idx]}: {value}")
            if parts:
                mapped[field] = "\n".join(parts)
        else:
            for idx in indexes:
                if idx >= len(raw_row):
                    continue
                value = sanitize_cell(raw_row[idx])
                if value:
                    mapped[field] = value  # la dernière colonne non vide l'emporte
    return mapped


def _process_job(job_id, mapping):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT workspace_id, raw_content FROM import_jobs WHERE id = %s", (job_id,))
            workspace_id, raw_content = cur.fetchone()
            cur.execute("UPDATE import_jobs SET status = 'processing' WHERE id = %s", (job_id,))
        conn.commit()

        text = bytes(raw_content).decode("utf-8-sig", errors="replace")
        delimiter = _sniff_delimiter(text)
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        rows = list(reader)
        header = [h.strip() for h in rows[0]]
        data_rows = rows[1:]

        # index des colonnes source -> champ cible, regroupées par champ pour
        # gérer le cas où plusieurs colonnes visent la même cible (ordre des
        # colonnes du fichier préservé, utile pour le champ notes ci-dessous).
        field_to_indexes = {}
        for idx, col_name in enumerate(header):
            target_field = mapping.get(col_name)
            if target_field:
                field_to_indexes.setdefault(target_field, []).append(idx)

        processed = imported = errors = duplicates = 0

        for row_number, raw_row in enumerate(data_rows, start=1):
            mapped = _build_mapped_row(header, raw_row, field_to_indexes)

            is_blocking, messages = validate_row(mapped)

            if is_blocking:
                _log_error(conn, job_id, row_number, "error", "; ".join(messages), raw_row)
                errors += 1
            elif _find_duplicate(conn, workspace_id, mapped) is not None:
                _log_error(
                    conn, job_id, row_number, "warning",
                    "Doublon détecté (SIRET ou nom+ville déjà en base) — ligne ignorée.", raw_row,
                )
                duplicates += 1
            else:
                _insert_prospect(conn, workspace_id, mapped)
                imported += 1
                if messages:
                    _log_error(conn, job_id, row_number, "warning", "; ".join(messages), raw_row)
                    errors += 1

            processed += 1

            if processed % CHUNK_SIZE == 0:
                _update_progress(conn, job_id, processed, imported, errors, duplicates)

        _update_progress(conn, job_id, processed, imported, errors, duplicates)

        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE import_jobs
                SET status = 'done', finished_at = now(), raw_content = NULL
                WHERE id = %s
                """,
                (job_id,),
            )
        conn.commit()

    except Exception as exc:  # noqa: BLE001 - on veut logguer toute erreur inattendue du job
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE import_jobs
                SET status = 'failed', finished_at = now(), raw_content = NULL
                WHERE id = %s
                """,
                (job_id,),
            )
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO import_errors (job_id, row_number, severity, message)
                VALUES (%s, 0, 'error', %s)
                """,
                (job_id, f"Échec du traitement : {exc}"),
            )
        conn.commit()
    finally:
        conn.close()


def _insert_prospect(conn, workspace_id, mapped):
    fields = list(mapped.keys())
    values = [mapped[f] or None for f in fields]
    columns_sql = ", ".join(fields + ["workspace_id", "source"])
    placeholders = ", ".join(["%s"] * (len(fields) + 2))
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO prospects ({columns_sql}) VALUES ({placeholders})",
            values + [workspace_id, "import_csv"],
        )
    conn.commit()


def _log_error(conn, job_id, row_number, severity, message, raw_row):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO import_errors (job_id, row_number, severity, message, raw_row)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (job_id, row_number, severity, message, json.dumps(raw_row)),
        )
    conn.commit()


def _update_progress(conn, job_id, processed, imported, errors, duplicates=0):
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE import_jobs
            SET processed_rows = %s, imported_count = %s, error_count = %s, duplicate_count = %s
            WHERE id = %s
            """,
            (processed, imported, errors, duplicates, job_id),
        )
    conn.commit()
