"""
Analyse d'un texte collé par l'utilisateur depuis un outil IA externe
(Gemini, ChatGPT, Copilot...) — aucune clé API, aucun coût, purement local.

Algorithme volontairement tolérant, pensé pour coller aux formats réels que
produisent ces outils (qui varient beaucoup et ne suivent jamais exactement
le format suggéré) :
- toute ligne qui ressemble à "champ : valeur" (champ reconnu) alimente la
  fiche en cours ;
- toute autre ligne non vide démarre une nouvelle fiche (son texte, nettoyé
  des puces/numéros/gras markdown, devient le nom de l'entreprise).

Gère donc aussi bien :
    * Boulangerie du Marché              1. **Boulangerie du Marché**
      * Adresse : ...                       - Adresse : ...
                                          **Le Fournil Niortais**
    Boulangerie du Marché                Adresse: ...
    Adresse: ...
"""
import re

FIELD_SYNONYMS = {
    "nom_entreprise": ["nom de l'entreprise", "nom entreprise", "entreprise", "societe", "société", "company", "nom"],
    "adresse": ["adresse", "address"],
    "ville": ["ville", "city", "commune"],
    "code_postal": ["code postal", "cp"],
    "telephone": ["telephone", "téléphone", "tel", "phone"],
    "email": ["email", "e-mail", "mail", "courriel"],
    "site_web": ["site internet", "site web", "site", "website", "url"],
    "description": ["description", "activite", "activité", "notes", "note"],
}

# Ligne "champ : valeur" — accepte :, -, – ou — comme séparateur (les IA varient).
LABEL_VALUE = re.compile(r"^(?P<label>[^:：\-–—]{2,40})\s*[:：\-–—]\s*(?P<value>.+)$")

# Puce de liste isolée (*, -, •...) en tête de ligne, à retirer avant de tenter la
# détection "label : valeur" — sinon le tiret de la puce casse la regex ci-dessus.
LEADING_BULLET = re.compile(r"^[\*•▪●]\s+|^-\s+(?=\S)")

# Préfixes à retirer d'une ligne avant de l'utiliser comme nom d'entreprise :
# puces, numérotation ("1.", "1)", "1 -"), gras/italique markdown.
LEADING_MARKER = re.compile(r"^[\s]*(?:[\*\-•▪●]|\d{1,3}[.)])\s*")
MARKDOWN_EMPHASIS = re.compile(r"[*_]{1,3}")

# Lignes d'intro/outro à ignorer (jamais un nom d'entreprise valable).
SKIP_PATTERNS = re.compile(
    r"^(voici|liste des|résultats?|voila|here (is|are)|based on|je (n'ai|te propose))",
    re.IGNORECASE,
)


def _match_field(label):
    norm = label.strip().lower()
    for field, synonyms in FIELD_SYNONYMS.items():
        if norm in synonyms or any(norm.startswith(s) for s in synonyms):
            return field
    return None


def _clean_name(text):
    text = LEADING_MARKER.sub("", text)
    text = MARKDOWN_EMPHASIS.sub("", text)
    return text.strip(" :\u2022-–—")


def _looks_like_intro(text):
    if text.endswith(":"):
        return True
    if len(text) > 100:
        return True
    if "?" in text or "!" in text:
        return True
    if SKIP_PATTERNS.match(text.strip()):
        return True
    return False


def parse_pasted_text(text):
    """Retourne une liste de dicts (mêmes clés que csv_import.PROSPECT_FIELDS)."""
    if not text or not text.strip():
        return []

    entries = []
    current = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        # Une ligne "champ : valeur" dont le champ est reconnu alimente la fiche en cours.
        # On retire d'abord une éventuelle puce de liste (le tiret d'une puce casserait sinon la regex).
        lv = LABEL_VALUE.match(LEADING_BULLET.sub("", line))
        field = _match_field(lv.group("label")) if lv else None

        if lv and field:
            if field == "nom_entreprise":
                if current:
                    entries.append(current)
                current = {"nom_entreprise": _clean_name(lv.group("value"))}
                continue
            if current is not None:
                current[field] = lv.group("value").strip()
            continue
        # Si le motif "label : valeur" matche mais que le label n'est pas reconnu (ex: un tiret
        # dans un nom d'entreprise comme "Boulangerie-Pâtisserie Noailles"), on ne le jette pas :
        # on retombe sur le traitement "nouvelle fiche" ci-dessous, avec la ligne complète.

        # Sinon : nouvelle fiche potentielle, sauf si ça ressemble à une phrase d'intro/outro.
        candidate = _clean_name(line)
        if not candidate or _looks_like_intro(line):
            continue
        if current:
            entries.append(current)
        current = {"nom_entreprise": candidate}

    if current:
        entries.append(current)

    return [e for e in entries if e.get("nom_entreprise")]
