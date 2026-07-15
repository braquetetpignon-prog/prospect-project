"""
Journalisation applicative minimale — écrit sur stdout, récupéré par les
logs Docker/Coolify (aucune infra de logs externe nécessaire pour l'instant).

Avant ce module, rien n'était jamais journalisé : une tâche de fond qui
échoue en boucle, une tentative d'intrusion, une erreur serveur — rien de
tout ça ne laissait de trace consultable après coup. Ce module comble ce
manque a minima : tentatives de connexion bloquées, exceptions non gérées,
échecs des tâches planifiées (envois de campagnes, recherches IA, maintenance).

Usage : `from app.app_logging import logger` puis `logger.warning(...)`,
`logger.exception(...)`, etc.
"""
import logging
import sys

logger = logging.getLogger("clickprospect")
logger.setLevel(logging.INFO)

if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    ))
    logger.addHandler(handler)
    logger.propagate = False
