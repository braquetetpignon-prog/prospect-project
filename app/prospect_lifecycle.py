"""
Cycle de vie RGPD des prospects "recalés" (spec technique §6, écart documenté
et non résolu jusqu'ici — voir contexte-reprise v6 §2).

Deux voies amènent un prospect au statut "recale" (voir aussi
app/prospects.py::update_statut, qui gère toujours le marquage manuel) :
  - manuel : l'utilisateur marque directement une fiche depuis l'app.
             recale_source reste NULL dans ce cas.
  - automatique : détection hebdomadaire, via l'API BODACC, d'une procédure
             collective (sauvegarde/redressement/liquidation) ou d'une
             radiation au RCS. recale_source = 'bodacc'. L'utilisateur garde
             la main : statut_avant_recalage permet de restaurer le statut
             précédent via cancel_automatic_recalage() en cas d'erreur.

Dans les deux cas, recale_at déclenche le même compte à rebours avant purge.

Purge (principe de minimisation RGPD, cf. spec §6) :
  1. À recale_at + RECALE_RETENTION_DAYS (7 jours) : le prospect est supprimé
     définitivement de `prospects` (cascade SQL existante sur
     prospect_activity, campaign_sends, rendez_vous...). Seul le nom
     d'entreprise (ou le SIRET si le nom est vide, ce qui ne devrait jamais
     arriver puisque nom_entreprise est NOT NULL — gardé par prudence) est
     conservé dans `prospect_archives`. Rien d'autre : pas d'adresse, pas de
     contact, pas de motif.
  2. Cette entrée d'archive est elle-même supprimée définitivement
     ARCHIVE_RETENTION_DAYS (1 jour) après sa création.

Appelé depuis :
  - app/lifecycle.py::run_daily_maintenance() pour purge_recale_prospects()
    et purge_expired_archives() (quotidien, même porte anti-doublon que le
    reste de la maintenance).
  - app/scheduler.py pour run_weekly_bodacc_check(), avec sa propre porte
    anti-doublon interne (1x/semaine) — même pattern que
    app/regulatory_watch.py (_already_ran_today / _mark_ran_today).
"""
from datetime import datetime, timedelta, timezone

import requests

from app.db import get_db
from app.app_logging import logger

RECALE_RETENTION_DAYS = 7
ARCHIVE_RETENTION_DAYS = 1
BODACC_CHECK_INTERVAL_DAYS = 7
_BODACC_LAST_RUN_KEY = "prospect_lifecycle_last_bodacc_check_at"

BODACC_SEARCH_URL = (
    "https://bodacc-datadila.opendatasoft.com/api/explore/v2.1/catalog/"
    "datasets/annonces-commerciales/records"
)
BODACC_REQUEST_TIMEOUT = 15
BODACC_RESULTS_PER_SIREN = 10
# Seule cette famille d'avis (+ le champ radiationaurcs, vérifié séparément)
# déclenche un recalage automatique — jamais les ventes/cessions, créations,
# modifications diverses, etc. Un recalage automatique doit rester
# strictement justifié.
BODACC_TRIGGER_FAMILIES = ("Procédures collectives",)


class ProspectLifecycleError(Exception):
    pass


# ---------------------------------------------------------------------
# Purge des prospects recalés + de leur archive
# ---------------------------------------------------------------------

def purge_recale_prospects():
    """Supprime définitivement tout prospect recalé (statut = 'recale')
    depuis plus de RECALE_RETENTION_DAYS jours, après avoir archivé le
    strict minimum. Retourne le nombre de prospects purgés."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=RECALE_RETENTION_DAYS)

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, nom_entreprise, siret FROM prospects
                WHERE statut = 'recale' AND recale_at IS NOT NULL AND recale_at < %s
                """,
                (cutoff,),
            )
            candidates = cur.fetchall()
    finally:
        conn.close()

    purged = 0
    for prospect_id, nom_entreprise, siret in candidates:
        archive_data = (nom_entreprise or siret or "").strip()

        conn = get_db()
        try:
            with conn.cursor() as cur:
                if archive_data:
                    cur.execute(
                        "INSERT INTO prospect_archives (archive_data) VALUES (%s)",
                        (archive_data,),
                    )
                cur.execute("DELETE FROM prospects WHERE id = %s", (prospect_id,))
            conn.commit()
            purged += 1
        except Exception:
            conn.rollback()
            logger.exception("Échec de la purge du prospect %s", prospect_id)
        finally:
            conn.close()

    return purged


def purge_expired_archives():
    """Supprime les entrées d'archive créées il y a plus
    d'ARCHIVE_RETENTION_DAYS jour(s). Retourne le nombre d'entrées purgées."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=ARCHIVE_RETENTION_DAYS)
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM prospect_archives WHERE created_at < %s RETURNING id",
                (cutoff,),
            )
            deleted = cur.fetchall()
        conn.commit()
        return len(deleted)
    finally:
        conn.close()


# ---------------------------------------------------------------------
# Détection automatique via BODACC (procédures collectives / radiations)
# ---------------------------------------------------------------------

def _bodacc_check_due():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM app_settings WHERE key = %s", (_BODACC_LAST_RUN_KEY,))
            row = cur.fetchone()
    finally:
        conn.close()

    if not row or not row[0]:
        return True
    try:
        last_run = datetime.fromisoformat(row[0])
    except ValueError:
        return True
    return datetime.now(timezone.utc) - last_run >= timedelta(days=BODACC_CHECK_INTERVAL_DAYS)


def _bodacc_mark_checked():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app_settings (key, value, updated_at) VALUES (%s, %s, now())
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
                """,
                (_BODACC_LAST_RUN_KEY, datetime.now(timezone.utc).isoformat()),
            )
        conn.commit()
    finally:
        conn.close()


def _query_bodacc_for_siren(siren):
    """Interroge l'API BODACC pour un SIREN donné. Retourne le dict de
    l'annonce la plus récente pertinente (procédure collective ou
    radiation), ou None si aucune annonce de ce type n'est trouvée. Ne
    lève jamais d'exception métier : une indisponibilité de l'API ne doit
    jamais interrompre la vérification hebdomadaire dans son ensemble —
    seul ce SIREN est ignoré pour ce passage (retenté la semaine suivante)."""
    params = {
        "where": f'registre="{siren}"',
        "order_by": "dateparution desc",
        "limit": BODACC_RESULTS_PER_SIREN,
    }
    try:
        resp = requests.get(BODACC_SEARCH_URL, params=params, timeout=BODACC_REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        logger.exception("Échec de la requête BODACC pour le SIREN %s", siren)
        return None

    for record in data.get("results", []):
        famille = record.get("familleavis_lib") or ""
        if famille in BODACC_TRIGGER_FAMILIES or record.get("radiationaurcs"):
            return record
    return None


def _build_motif(record):
    date_parution = record.get("dateparution") or ""
    if record.get("radiationaurcs"):
        return f"Radiation au RCS (BODACC, {date_parution})"
    famille = record.get("familleavis_lib") or ""
    type_avis = record.get("typeavis_lib") or ""
    return f"{famille} — {type_avis} (BODACC, {date_parution})".strip(" —")


def check_bodacc_radiations():
    """Fait le travail réel (pas de porte anti-doublon ici, voir
    run_weekly_bodacc_check pour ça). Vérifie chaque prospect actif (ni
    recale, ni client) ayant un SIREN renseigné ; marque automatiquement
    'recale' ceux pour lesquels le BODACC signale une procédure collective
    ou une radiation. Retourne la liste des prospect_id marqués."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, workspace_id, siren, statut FROM prospects
                WHERE siren IS NOT NULL AND siren != '' AND statut NOT IN ('recale', 'client')
                """
            )
            candidates = cur.fetchall()
    finally:
        conn.close()

    flagged = []
    for prospect_id, workspace_id, siren, statut_avant in candidates:
        record = _query_bodacc_for_siren(siren)
        if not record:
            continue

        motif = _build_motif(record)
        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE prospects
                    SET statut = 'recale', motif_recalage = %s, recale_at = now(),
                        recale_source = 'bodacc', statut_avant_recalage = %s, updated_at = now()
                    WHERE id = %s
                    """,
                    (motif, statut_avant, prospect_id),
                )
            conn.commit()
        finally:
            conn.close()

        try:
            from app import activity
            activity.log_event(
                prospect_id, workspace_id, "statut_change",
                f"Statut changé en « recale » (détection automatique BODACC : {motif}).",
                user_id=None,
            )
        except Exception:
            logger.exception("Échec de la journalisation d'activité pour le prospect %s", prospect_id)

        flagged.append(prospect_id)

    return flagged


def run_weekly_bodacc_check():
    """Point d'entrée appelé depuis le planificateur — porte anti-doublon
    (1x/semaine) avant de faire le travail réel."""
    if not _bodacc_check_due():
        return
    try:
        check_bodacc_radiations()
    finally:
        # Posée même en cas d'erreur partielle (déjà encaissée SIREN par
        # SIREN dans _query_bodacc_for_siren) — sinon un sous-ensemble de
        # SIREN en échec ferait rescanner tous les prospects en boucle à
        # chaque passage du planificateur (toutes les 30s) au lieu d'une
        # fois par semaine.
        _bodacc_mark_checked()


# ---------------------------------------------------------------------
# Annulation d'un marquage automatique (l'utilisateur garde la main)
# ---------------------------------------------------------------------

def cancel_automatic_recalage(prospect_id, workspace_id, user_id=None):
    """Annule un marquage 'recale' déclenché automatiquement par BODACC —
    jamais un marquage manuel : dans ce cas l'utilisateur repasse
    simplement par le changement de statut normal
    (app/prospects.py::update_statut). Restaure le statut précédent et
    efface les traces du recalage automatique."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT recale_source, statut_avant_recalage FROM prospects WHERE id = %s AND workspace_id = %s",
                (prospect_id, workspace_id),
            )
            row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        raise ProspectLifecycleError("Prospect introuvable dans cet espace de travail.")

    recale_source, statut_avant = row
    if recale_source != "bodacc":
        raise ProspectLifecycleError("Ce prospect n'a pas été recalé automatiquement.")

    statut_restaure = statut_avant or "nouveau"

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE prospects
                SET statut = %s, motif_recalage = NULL, recale_at = NULL,
                    recale_source = NULL, statut_avant_recalage = NULL, updated_at = now()
                WHERE id = %s AND workspace_id = %s
                """,
                (statut_restaure, prospect_id, workspace_id),
            )
        conn.commit()
    finally:
        conn.close()

    try:
        from app import activity
        activity.log_event(
            prospect_id, workspace_id, "statut_change",
            f"Marquage automatique BODACC annulé, statut restauré à « {statut_restaure} ».",
            user_id=user_id,
        )
    except Exception:
        logger.exception("Échec de la journalisation d'activité pour le prospect %s", prospect_id)

    return statut_restaure
