"""
Monitoring VPS pour /supadmin : disque, RAM, CPU, état de la base de
données, bande passante. Échantillonné périodiquement (voir
scheduler.py::_loop, qui appelle maybe_collect_sample() à chaque tour —
la collecte réelle ne se déclenche qu'une fois toutes les ~5 minutes,
via une garde basée sur l'état en base) et stocké dans
vps_metrics_history pour permettre un historique et le calcul des heures
de pointe/creuses.

Limite importante à connaître : les métriques disque/RAM/CPU/réseau sont
lues via psutil depuis l'intérieur du conteneur applicatif. Tant qu'un seul
service occupe le VPS (le cas aujourd'hui), c'est une bonne approximation
de l'état réel de la machine. Si d'autres conteneurs sont ajoutés sur le
même VPS, ces chiffres ne refléteront plus que la part de ce conteneur, pas
la machine entière — à revoir à ce moment-là (ex: agent de monitoring au
niveau de l'hôte plutôt que dans un conteneur applicatif).
"""
import os
import time
from datetime import datetime, timedelta, timezone

import psutil

from app.db import get_db

# Chemin surveillé pour l'usage disque : la racine du conteneur par défaut,
# qui reflète le volume principal du VPS dans le schéma actuel (1 VPS = 1
# service). Surchargeable via VPS_MONITORING_DISK_PATH si le volume de
# données significatif se trouve ailleurs (ex: un point de montage dédié).
_DISK_PATH = os.environ.get("VPS_MONITORING_DISK_PATH", "/")

_SAMPLE_INTERVAL_SECONDS = 5 * 60

# Compteurs réseau cumulés lus au tour précédent, pour calculer un débit
# (delta / temps écoulé) plutôt qu'un total depuis le démarrage du
# conteneur, qui ne dit rien sur la charge actuelle. Volontairement en
# mémoire (par worker) : un delta réseau n'a de sens que rapporté au trafic
# vu par CE worker depuis SON dernier calcul, pas une valeur partageable
# entre workers.
_last_net_counters = {"bytes_sent": None, "bytes_recv": None, "at": None}

DEFAULT_THRESHOLDS = {
    "vps_alert_disk_pct": "85",
    "vps_alert_ram_pct": "85",
    "vps_alert_cpu_pct": "90",
}


def maybe_collect_sample():
    """Appelée depuis le planificateur de fond (toutes les ~30s comme le
    reste) — ne fait réellement une collecte que toutes les 5 minutes.
    Vérifie l'état réel en base (pas seulement un minuteur en mémoire) :
    avec plusieurs workers gunicorn, un minuteur purement local produirait
    un échantillon en double par worker toutes les 5 minutes (même limite
    déjà connue sur le reste du planificateur, voir scheduler.py). Ne lève
    jamais d'exception : un souci de collecte ne doit jamais interrompre le
    reste des tâches de fond."""
    try:
        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT sampled_at FROM vps_metrics_history ORDER BY sampled_at DESC LIMIT 1")
                row = cur.fetchone()
        finally:
            conn.close()
        if row:
            age = (datetime.now(timezone.utc) - row[0]).total_seconds()
            if age < _SAMPLE_INTERVAL_SECONDS:
                return
        _collect_and_store()
    except Exception:
        pass


def _collect_and_store():
    disk = psutil.disk_usage(_DISK_PATH)
    ram = psutil.virtual_memory()
    cpu_pct = psutil.cpu_percent(interval=0.3)

    bandwidth_mbps = None
    net = psutil.net_io_counters()
    now = time.time()
    if _last_net_counters["at"] is not None:
        elapsed = now - _last_net_counters["at"]
        if elapsed > 0:
            delta_bytes = (net.bytes_sent - _last_net_counters["bytes_sent"]) + \
                          (net.bytes_recv - _last_net_counters["bytes_recv"])
            bandwidth_mbps = round((delta_bytes * 8 / 1_000_000) / elapsed, 3)
    _last_net_counters["bytes_sent"] = net.bytes_sent
    _last_net_counters["bytes_recv"] = net.bytes_recv
    _last_net_counters["at"] = now

    db_size_mb, db_connections, db_status = _db_health()

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO vps_metrics_history
                    (disk_used_pct, disk_used_gb, disk_total_gb,
                     ram_used_pct, ram_used_gb, ram_total_gb, cpu_pct,
                     db_size_mb, db_connections, db_status,
                     net_bytes_sent, net_bytes_recv, bandwidth_mbps)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    disk.percent, round(disk.used / 1e9, 2), round(disk.total / 1e9, 2),
                    ram.percent, round(ram.used / 1e9, 2), round(ram.total / 1e9, 2), cpu_pct,
                    db_size_mb, db_connections, db_status,
                    net.bytes_sent, net.bytes_recv, bandwidth_mbps,
                ),
            )
        conn.commit()
    finally:
        conn.close()

    _maybe_alert(disk.percent, ram.percent, cpu_pct)


def _db_health():
    """État de la base : taille, nombre de connexions actives, statut. Ne
    fait jamais planter l'appelant si la base est justement le problème —
    retourne un statut 'down' plutôt que de lever une exception."""
    try:
        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_database_size(current_database())")
                size_mb = round(cur.fetchone()[0] / 1e6, 1)
                cur.execute("SELECT count(*) FROM pg_stat_activity")
                connections = cur.fetchone()[0]
            return size_mb, connections, "ok"
        finally:
            conn.close()
    except Exception:
        return None, None, "down"


def _maybe_alert(disk_pct, ram_pct, cpu_pct):
    """Envoie un e-mail aux administrateurs si un seuil réglable est
    dépassé. Une seule alerte par heure et par métrique (évite le
    matraquage si un seuil reste dépassé plusieurs cycles de suite)."""
    from app import system_mail

    if not system_mail.is_configured():
        return

    thresholds = get_thresholds()
    breaches = []
    if disk_pct >= thresholds["vps_alert_disk_pct"]:
        breaches.append(("disque", disk_pct, thresholds["vps_alert_disk_pct"]))
    if ram_pct >= thresholds["vps_alert_ram_pct"]:
        breaches.append(("RAM", ram_pct, thresholds["vps_alert_ram_pct"]))
    if cpu_pct >= thresholds["vps_alert_cpu_pct"]:
        breaches.append(("CPU", cpu_pct, thresholds["vps_alert_cpu_pct"]))
    if not breaches:
        return

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT value FROM app_settings WHERE key = 'vps_last_alert_sent_at'"
            )
            row = cur.fetchone()
    finally:
        conn.close()

    if row and row[0]:
        last_sent = datetime.fromisoformat(row[0])
        if datetime.now(timezone.utc) - last_sent < timedelta(hours=1):
            return

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT email FROM superadmins WHERE role = 'administrateur' AND is_active")
            admin_emails = [r[0] for r in cur.fetchall()]
    finally:
        conn.close()

    lines = "\n".join(f"- {label} : {val:.0f}% (seuil {seuil}%)" for label, val, seuil in breaches)
    body = f"Seuil(s) d'alerte VPS dépassé(s) :\n\n{lines}\n\nVoir /supadmin pour le détail."
    for email in admin_emails:
        try:
            system_mail.send_system_email(email, "Alerte VPS ClickProspect — seuil dépassé", body)
        except system_mail.SystemMailError:
            pass

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app_settings (key, value, updated_at) VALUES ('vps_last_alert_sent_at', %s, now())
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
                """,
                (datetime.now(timezone.utc).isoformat(),),
            )
        conn.commit()
    finally:
        conn.close()


def get_thresholds():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT key, value FROM app_settings WHERE key = ANY(%s)",
                (list(DEFAULT_THRESHOLDS.keys()),),
            )
            stored = dict(cur.fetchall())
    finally:
        conn.close()
    return {
        key: float(stored.get(key, default))
        for key, default in DEFAULT_THRESHOLDS.items()
    }


def set_thresholds(values):
    """values : dict parmi les clés de DEFAULT_THRESHOLDS, valeurs en
    pourcentage (0-100). Ignore silencieusement toute clé inconnue."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            for key in DEFAULT_THRESHOLDS:
                if key in values:
                    pct = float(values[key])
                    if not (0 < pct <= 100):
                        continue
                    cur.execute(
                        """
                        INSERT INTO app_settings (key, value, updated_at) VALUES (%s, %s, now())
                        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
                        """,
                        (key, str(pct)),
                    )
        conn.commit()
    finally:
        conn.close()


def get_latest_snapshot():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT sampled_at, disk_used_pct, disk_used_gb, disk_total_gb,
                       ram_used_pct, ram_used_gb, ram_total_gb, cpu_pct,
                       db_size_mb, db_connections, db_status, bandwidth_mbps
                FROM vps_metrics_history ORDER BY sampled_at DESC LIMIT 1
                """
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return None
    keys = ["sampled_at", "disk_used_pct", "disk_used_gb", "disk_total_gb",
            "ram_used_pct", "ram_used_gb", "ram_total_gb", "cpu_pct",
            "db_size_mb", "db_connections", "db_status", "bandwidth_mbps"]
    return dict(zip(keys, row))


def get_history(hours=48):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT sampled_at, disk_used_pct, ram_used_pct, cpu_pct, bandwidth_mbps
                FROM vps_metrics_history
                WHERE sampled_at > now() - (%s || ' hours')::interval
                ORDER BY sampled_at
                """,
                (hours,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    return [
        {"sampled_at": r[0], "disk_used_pct": r[1], "ram_used_pct": r[2],
         "cpu_pct": r[3], "bandwidth_mbps": r[4]}
        for r in rows
    ]


def get_peak_hours(days=14):
    """Heure de la journée (0-23) la plus chargée et la plus creuse, en
    moyenne sur les derniers jours d'historique — d'après la bande passante
    échantillonnée. Retourne None tant qu'il n'y a pas assez de données
    (minimum 24h d'historique nécessaire pour un résultat qui a du sens)."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT EXTRACT(HOUR FROM sampled_at)::int AS heure, AVG(bandwidth_mbps) AS moyenne
                FROM vps_metrics_history
                WHERE sampled_at > now() - (%s || ' days')::interval
                  AND bandwidth_mbps IS NOT NULL
                GROUP BY heure
                ORDER BY heure
                """,
                (days,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    if len(rows) < 12:  # au moins une demi-journée de créneaux distincts
        return None

    heure_haute = max(rows, key=lambda r: r[1])
    heure_basse = min(rows, key=lambda r: r[1])
    return {
        "heure_haute": {"heure": heure_haute[0], "bandwidth_mbps": round(float(heure_haute[1]), 3)},
        "heure_basse": {"heure": heure_basse[0], "bandwidth_mbps": round(float(heure_basse[1]), 3)},
    }
