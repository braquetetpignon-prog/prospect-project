"""
Recherche automatique de prospects (Option 2 bis) — API officielle
Recherche d'Entreprises (data.gouv.fr / recherche-entreprises.api.gouv.fr),
gratuite, sans clé, sans quota interne à gérer.

Principe : aucune donnée n'est jamais inventée. En cas d'erreur ou
d'indisponibilité de l'API, aucun résultat fictif n'est généré — l'erreur
est simplement remontée à l'utilisateur. Le registre officiel ne contient
ni téléphone ni e-mail (ce n'est pas une donnée publique du répertoire
Sirene) : ces champs restent vides, à compléter manuellement.

Filtrage par forme juridique : fait côté serveur ClickProspect (pas par un
paramètre de l'API, dont le support n'est pas garanti pour ce filtre) en
utilisant le champ nature_juridique renvoyé par chaque résultat.
"""
import re

import requests

SEARCH_URL = "https://recherche-entreprises.api.gouv.fr/search"
REQUEST_TIMEOUT = 15
MAX_PAGES_PER_FORM = 6  # garde-fou : jusqu'à 6*25 = 150 résultats scannés par forme
PER_PAGE = 25

# Codes nature_juridique (nomenclature Insee) regroupés par forme juridique usuelle.
LEGAL_FORM_GROUPS = {
    "sarl_eurl": {"label": "SARL / EURL", "codes": ["5498", "5499"]},
    "sas_sasu": {"label": "SAS / SASU", "codes": ["5710", "5720"]},
    "association": {"label": "Associations", "codes": ["9220"]},
}

NAF_CODE_PATTERN = re.compile(r"^\d{2}\.?\d{2}[A-Z]$", re.IGNORECASE)


class OfficialSearchError(Exception):
    pass


def _build_secteur_param(secteur):
    """Devine si le texte saisi est un code NAF (ex: 5610A ou 56.10A) ou un
    texte libre (ex: 'boulangerie'), et retourne les bons paramètres de requête."""
    secteur = (secteur or "").strip()
    if not secteur:
        return {}
    if NAF_CODE_PATTERN.match(secteur):
        normalized = secteur.upper().replace(".", "")
        code_naf = f"{normalized[:2]}.{normalized[2:]}"
        return {"code_naf": code_naf}
    return {"q": secteur}


def _fetch_page(zone, secteur_params, page):
    params = {
        "code_postal": zone,
        "etat_administratif": "A",
        "per_page": PER_PAGE,
        "page": page,
        **secteur_params,
    }
    try:
        resp = requests.get(SEARCH_URL, params=params, timeout=REQUEST_TIMEOUT)
    except Exception as exc:
        raise OfficialSearchError(f"Impossible de contacter le registre officiel : {exc}") from exc

    if resp.status_code >= 400:
        raise OfficialSearchError(f"Erreur du registre officiel ({resp.status_code}) : {resp.text[:200]}")

    try:
        return resp.json()
    except ValueError as exc:
        raise OfficialSearchError("Réponse inattendue du registre officiel.") from exc


def _to_prospect_fields(result):
    siege = result.get("siege") or {}
    return {
        "nom_entreprise": result.get("nom_complet") or result.get("nom_raison_sociale"),
        "siren": result.get("siren"),
        "siret": siege.get("siret"),
        "naf_code": siege.get("activite_principale"),
        "adresse": siege.get("adresse"),
        "code_postal": siege.get("code_postal"),
        "ville": siege.get("libelle_commune"),
    }


def _normalize_for_match(text):
    """Réduit une chaîne à ses lettres/chiffres en minuscule pour comparer
    deux noms d'entreprise ou deux communes sans se faire piéger par la
    casse, les accents-espaces ou la ponctuation (SARL, tirets, points...)."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def enrich_by_name(nom_entreprise, ville=None):
    """Tente de retrouver, dans le registre officiel, la fiche correspondant
    à une entreprise proposée par ailleurs (ex: par l'IA) en cherchant par
    nom (et ville si connue). Sert à compléter SIRET/adresse sans jamais les
    inventer : si aucune correspondance suffisamment fiable n'est trouvée
    (nom proche + ville cohérente le cas échéant), retourne None. Ne lève
    jamais d'exception : une indisponibilité de l'API ne doit pas faire
    échouer la recherche IA elle-même, juste laisser ce champ vide.
    """
    nom_entreprise = (nom_entreprise or "").strip()
    if not nom_entreprise:
        return None

    params = {"q": nom_entreprise, "etat_administratif": "A", "per_page": 5, "page": 1}
    try:
        resp = requests.get(SEARCH_URL, params=params, timeout=REQUEST_TIMEOUT)
        if resp.status_code >= 400:
            return None
        data = resp.json()
    except Exception:
        return None

    results = data.get("results") or []
    if not results:
        return None

    target_name = _normalize_for_match(nom_entreprise)
    target_ville = _normalize_for_match(ville) if ville else None
    if not target_name:
        return None

    for result in results:
        candidate_name = _normalize_for_match(result.get("nom_complet") or result.get("nom_raison_sociale"))
        if not candidate_name:
            continue
        # Correspondance de nom seulement si l'un contient l'autre (évite les
        # faux positifs du type "Martin" qui matcherait n'importe quel "Martin ...").
        if target_name not in candidate_name and candidate_name not in target_name:
            continue

        siege = result.get("siege") or {}
        if target_ville:
            candidate_ville = _normalize_for_match(siege.get("libelle_commune"))
            # Ville connue des deux côtés mais différente : trop risqué de
            # retenir automatiquement (homonymes dans des villes différentes).
            if candidate_ville and candidate_ville != target_ville:
                continue

        fields = _to_prospect_fields(result)
        return fields

    return None


def search_by_legal_forms(zone, secteur, forms_with_quantities):
    """forms_with_quantities : dict {"sarl_eurl": 20, "sas_sasu": 20, ...}
    Retourne {"results": [...], "counts": {"sarl_eurl": 14, ...}} — le compte
    réel peut être inférieur à la quantité demandée si le registre n'a pas
    assez de résultats correspondants (jamais de donnée inventée pour combler)."""
    zone = (zone or "").strip()
    if not zone:
        raise OfficialSearchError("La zone géographique est requise (ex: code postal).")
    if not forms_with_quantities:
        raise OfficialSearchError("Sélectionne au moins une forme juridique.")

    secteur_params = _build_secteur_param(secteur)
    all_results = []
    counts = {}

    for form_key, quantity in forms_with_quantities.items():
        group = LEGAL_FORM_GROUPS.get(form_key)
        if not group or quantity <= 0:
            continue

        matched = []
        page = 1
        total_pages = None
        while len(matched) < quantity and page <= MAX_PAGES_PER_FORM and (total_pages is None or page <= total_pages):
            data = _fetch_page(zone, secteur_params, page)
            total_pages = data.get("total_pages", 1)
            for result in data.get("results", []):
                if result.get("nature_juridique") in group["codes"]:
                    matched.append(result)
                    if len(matched) >= quantity:
                        break
            page += 1

        counts[form_key] = len(matched)
        for result in matched[:quantity]:
            fields = _to_prospect_fields(result)
            fields["forme_juridique"] = group["label"]
            all_results.append(fields)

    return {"results": all_results, "counts": counts}
