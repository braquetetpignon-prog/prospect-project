"""
Analyse d'un texte collé par l'utilisateur depuis un outil IA externe
(Gemini, ChatGPT, Copilot...) — aucune clé API, aucun coût, purement local.

Format attendu (tolérant) : une liste à puces, une entreprise par puce de
premier niveau, avec des sous-puces "champ : valeur" pour les détails.

Exemple :
    * Boulangerie du Marché
      * Adresse : 12 rue de la Gare, Niort
      * Téléphone : 05 49 00 00 00
      * Site internet : boulangerie-marche.fr

    - Plomberie Dupont
      - Ville : La Rochelle
      - Email : contact@plomberie-dupont.fr
"""
import re

FIELD_SYNONYMS = {
    "nom_entreprise": ["nom de l'entreprise", "nom entreprise", "entreprise", "societe", "société", "company", "nom"],
    "adresse": ["adresse", "address"],
    "ville": ["ville", "city"],
    "code_postal": ["code postal", "cp"],
    "telephone": ["telephone", "téléphone", "tel", "phone"],
    "email": ["email", "e-mail", "mail", "courriel"],
    "site_web": ["site internet", "site web", "site", "website", "url"],
    "description": ["description", "activite", "activité", "notes", "note"],
}

# Regex d'une ligne à puce, avec indentation capturée pour détecter le niveau.
BULLET_LINE = re.compile(r"^(?P<indent>[ \t]*)[\*\-•▪●]\s*(?P<rest>.+?)\s*$")
LABEL_VALUE = re.compile(r"^(?P<label>[^:：]{2,40})\s*[:：]\s*(?P<value>.+)$")


def _match_field(label):
    norm = label.strip().lower()
    for field, synonyms in FIELD_SYNONYMS.items():
        if norm in synonyms or any(norm.startswith(s) for s in synonyms):
            return field
    return None


def parse_pasted_text(text):
    """Retourne une liste de dicts (mêmes clés que csv_import.PROSPECT_FIELDS)."""
    if not text or not text.strip():
        return []

    lines = [line for line in text.splitlines() if line.strip()]
    entries = []
    current = None
    base_indent = None

    for raw_line in lines:
        m = BULLET_LINE.match(raw_line)
        if not m:
            continue  # ligne sans puce : ignorée (texte d'accompagnement, etc.)

        indent = len(m.group("indent").expandtabs())
        rest = m.group("rest")

        if base_indent is None:
            base_indent = indent

        is_top_level = indent <= base_indent

        if is_top_level:
            if current:
                entries.append(current)
            current = {}
            lv = LABEL_VALUE.match(rest)
            if lv and _match_field(lv.group("label")) == "nom_entreprise":
                current["nom_entreprise"] = lv.group("value").strip()
            else:
                current["nom_entreprise"] = rest.strip()
        else:
            if current is None:
                continue
            lv = LABEL_VALUE.match(rest)
            if not lv:
                continue
            field = _match_field(lv.group("label"))
            if field and field != "nom_entreprise":
                current[field] = lv.group("value").strip()

    if current:
        entries.append(current)

    # Nettoyage : on ignore les entrées sans nom exploitable
    return [e for e in entries if e.get("nom_entreprise")]
