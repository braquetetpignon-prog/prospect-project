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
    "contact_nom": ["dirigeant", "gerant", "gérant", "gérante", "responsable", "contact", "propriétaire", "proprietaire", "réalisateur", "realisateur", "référent", "referent"],
    "description": ["description", "activite", "activité", "notes", "note"],
}

# Ligne "champ : valeur" — accepte :, -, – ou — comme séparateur (les IA varient).
# Le séparateur est capturé séparément : un ":" est un signal fort d'intention
# "champ : valeur" (cf. plus bas), alors qu'un "-" est ambigu (peut aussi apparaître
# dans un nom d'entreprise composé, ex. "Boulangerie-Pâtisserie Noailles").
LABEL_VALUE = re.compile(r"^(?P<label>[^:：\-–—]{2,40})\s*(?P<sep>[:：\-–—])\s*(?P<value>.+)$")

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


# Valeur encore entre crochets, jamais remplacée (ex: "[Nom de l'entreprise]") — signe
# que l'utilisateur a collé un PROMPT (le sien ou un modèle suggéré par l'app) au lieu
# de la réponse que l'IA lui a renvoyée après l'avoir utilisé.
PLACEHOLDER_VALUE = re.compile(r"^\[.{1,80}\]$")


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


def _drop_unfilled_placeholders(entry):
    """Retire les champs dont la valeur est encore un espace réservé entre
    crochets (jamais remplacé) — mieux vaut un champ vide qu'une donnée
    du type "[adresse complète]" reprise telle quelle."""
    return {k: v for k, v in entry.items() if not (isinstance(v, str) and PLACEHOLDER_VALUE.match(v.strip()))}


def looks_like_unfilled_prompt(text):
    """Heuristique : le texte contient plusieurs espaces réservés entre crochets
    jamais remplacés (typiquement le prompt lui-même, collé par erreur à la
    place de la réponse de l'IA). Sert uniquement à afficher un message d'erreur
    plus utile que "aucune entreprise reconnue"."""
    return len(re.findall(r"\[[^\[\]\n]{2,60}\]", text or "")) >= 3


# Tous les libellés de champs reconnus, triés du plus long au plus court (évite
# qu'une alternative courte comme "site" ne masque "site internet"/"site web").
_ALL_LABEL_SYNONYMS = sorted(
    {syn for synonyms in FIELD_SYNONYMS.values() for syn in synonyms},
    key=len,
    reverse=True,
)

# Repère un espace juste avant un libellé reconnu suivi (après espaces éventuels)
# d'un ":" — c'est-à-dire un nouveau champ qui démarre au milieu d'une ligne au
# lieu d'être sur sa propre ligne.
_INLINE_LABEL_BOUNDARY = re.compile(
    r"[ \t]+(?=(?:" + "|".join(re.escape(s) for s in _ALL_LABEL_SYNONYMS) + r")\s*[:：])",
    re.IGNORECASE,
)


def _split_inline_labels(text):
    """Certains outils IA (ou le copier-coller depuis leur interface) aplatissent
    la mise en forme et livrent tous les champs d'une fiche sur une seule ligne :
    'Entreprise X Activité : ... Adresse : ... Téléphone : ...' au lieu d'un champ
    par ligne. On réinsère un saut de ligne avant chaque libellé reconnu pour que
    l'algorithme ligne-par-ligne ci-dessous s'applique normalement dans les deux cas.
    """
    return _INLINE_LABEL_BOUNDARY.sub("\n", text)


def parse_pasted_text(text):
    """Retourne une liste de dicts (mêmes clés que csv_import.PROSPECT_FIELDS)."""
    if not text or not text.strip():
        return []

    text = _split_inline_labels(text)

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

        # Motif "label : valeur" avec un vrai deux-points mais un label non reconnu
        # (Horaires, Zone d'intervention, Dirigeant non couvert, etc.) : c'est une
        # métadonnée qu'on ne stocke pas, pas un nom d'entreprise — on l'ignore
        # plutôt que de créer une fausse fiche avec "Horaires" comme nom.
        if lv and lv.group("sep") in (":", "："):
            continue

        # Si le motif matche avec un tiret mais que le label n'est pas reconnu (ex: un tiret
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

    entries = [_drop_unfilled_placeholders(e) for e in entries]
    return [e for e in entries if e.get("nom_entreprise")]
