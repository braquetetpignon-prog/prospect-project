"""
Chiffrement des identifiants SMTP avant écriture en base.

Utilise Fernet (chiffrement symétrique, cryptography.io) avec une clé lue
depuis la variable d'environnement SMTP_ENCRYPTION_KEY. Cette clé est un
secret d'application (pas un identifiant personnel) — à générer une seule
fois et à stocker dans Coolify, jamais dans le code ni committée sur GitHub.

Pour générer une clé valide :
    python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""
import os

from cryptography.fernet import Fernet, InvalidToken

_ENCRYPTION_KEY = os.environ.get("SMTP_ENCRYPTION_KEY")


class EncryptionNotConfigured(Exception):
    pass


def _get_fernet():
    if not _ENCRYPTION_KEY:
        raise EncryptionNotConfigured(
            "SMTP_ENCRYPTION_KEY n'est pas configurée sur le serveur."
        )
    return Fernet(_ENCRYPTION_KEY.encode())


def encrypt(plain_text):
    return _get_fernet().encrypt(plain_text.encode()).decode()


def decrypt(encrypted_text):
    try:
        return _get_fernet().decrypt(encrypted_text.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("Impossible de déchiffrer (clé invalide ou donnée corrompue).") from exc
