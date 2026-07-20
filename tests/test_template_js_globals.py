"""
Régression du bug corrigé pendant le chantier 5 (contexte v5, section 3 et
5) : `settings.html` référençait `statusLabels[rule.statut]`, une variable
JS qui n'existe que dans `prospects.html` — pas dans `base.html`, donc pas
réellement globale. Le panneau Automatisations plantait silencieusement
au chargement ("Impossible de charger les automatisations."), repéré
seulement par un test manuel en prod.

Ce test ne remplace pas un vrai test JS (aucun moteur JS dans ce projet
Python). Une première version, plus générale, repérait TOUT identifiant
utilisé dans une page mais déclaré seulement dans une autre — elle
attrapait bien le bug, mais avec des dizaines de faux positifs (`id`,
`role`, `type`... des noms courants réutilisés sans lien réel d'une page
à l'autre). Cette version cible précisément le motif du vrai bug : une
constante "table de correspondance" (ex. `const X = { cle: valeur, ... }`)
utilisée ailleurs via un accès par crochet (`X[...]`) — c'est ce style
d'usage, spécifique aux dictionnaires de labels, qui a produit le
plantage silencieux. Un identifiant générique utilisé sans ce style
d'accès n'est pas retenu.
"""
import re
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent / "app"

# Longueur minimale pour un nom de constante : élimine les faux positifs
# style boucle/paramètre à une ou deux lettres (i, n, a, d, r, x, y...).
MIN_NAME_LENGTH = 4


def _lookup_table_declarations(text):
    """Constantes déclarées comme table de correspondance au niveau du
    fichier : `const NOM = { ... }` (objet littéral), le style exact du
    bug `statusLabels`/`AUTOMATION_STATUT_LABELS`."""
    names = set()
    for match in re.finditer(r"\bconst\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*\{", text):
        name = match.group(1)
        if len(name) >= MIN_NAME_LENGTH:
            names.add(name)
    return names


def _lookup_style_usages(text, name):
    """Vrai si `name` est utilisé comme une table de correspondance dans ce
    texte : accès par crochet (`name[...]`) — le style d'usage propre aux
    dictionnaires de labels, pas un simple mot qui apparaît par coïncidence
    (ex. dans une chaîne, un commentaire, ou comme nom de variable locale
    sans rapport)."""
    return re.search(rf"\b{re.escape(name)}\s*\[", text) is not None


def test_tables_de_correspondance_reellement_definies_dans_base():
    base_text = (APP_DIR / "base.html").read_text(encoding="utf-8")
    base_tables = _lookup_table_declarations(base_text)

    templates = [t for t in sorted(APP_DIR.glob("*.html")) if t.name != "base.html"]
    per_template_text = {t.name: t.read_text(encoding="utf-8") for t in templates}
    per_template_tables = {name: _lookup_table_declarations(text) for name, text in per_template_text.items()}

    declared_in = {}
    for name, tables in per_template_tables.items():
        for table in tables:
            declared_in.setdefault(table, set()).add(name)

    suspects = {}
    for name, text in per_template_text.items():
        local_tables = per_template_tables[name]
        for table, declaring_files in declared_in.items():
            if table in base_tables:
                continue
            if name in declaring_files or table in local_tables:
                continue
            if _lookup_style_usages(text, table):
                suspects.setdefault(name, {})[table] = sorted(declaring_files)

    assert not suspects, (
        "Ces pages utilisent, en accès par crochet (table[...]), des tables de "
        "correspondance qui ne sont déclarées ni localement ni dans base.html, "
        "mais seulement dans une AUTRE page — exactement le bug `statusLabels` "
        f"du chantier 5 : {suspects}. Définir la table localement, ou la faire "
        "remonter dans base.html si elle doit vraiment être partagée entre pages."
    )
