"""
Planificateur en arrière-plan : appelle périodiquement sending.process_due_sends()
pour dispatcher les envois planifiés arrivés à échéance.

Démarré une fois par worker gunicorn (à l'import du module). Sûr en cas de
plusieurs workers en parallèle grâce à FOR UPDATE SKIP LOCKED côté DB — chaque
envoi n'est traité qu'une seule fois, peu importe combien de workers tournent.
"""
import os
import threading
import time

from app import sending
from app import ia_search

SCHEDULER_INTERVAL_SECONDS = int(os.environ.get("SCHEDULER_INTERVAL_SECONDS", "30"))
_started = False
_lock = threading.Lock()


def _loop():
    while True:
        try:
            sending.process_due_sends()
        except Exception:
            # On ne laisse jamais le planificateur mourir sur une erreur ponctuelle
            # (ex: base momentanément indisponible) — il réessaiera au prochain tour.
            pass
        try:
            ia_search.run_due_scheduled_searches()
        except Exception:
            pass
        time.sleep(SCHEDULER_INTERVAL_SECONDS)


def start():
    global _started
    with _lock:
        if _started:
            return
        thread = threading.Thread(target=_loop, daemon=True)
        thread.start()
        _started = True
