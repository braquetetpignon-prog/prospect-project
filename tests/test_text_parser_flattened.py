"""
Deux corrections couvertes ici :
1. Le texte collé depuis certains outils IA (Gemini notamment), quand
   plusieurs résultats sont copiés d'un coup plutôt qu'un par un, arrive
   totalement aplati (aucun saut de ligne, aucun espace même entre la fin
   d'une valeur et le libellé suivant). Le découpage doit fonctionner
   quand même.
2. Régression introduite en corrigeant le point 1 : des synonymes courts
   ("phone", "mail") sont des sous-chaînes de mots plus longs ("Téléphone",
   "Email") — sans exiger une majuscule au début du libellé détecté, le
   texte se coupait en plein milieu de ces mots.
"""
from app import text_parser


def test_texte_normal_un_par_un_fonctionne_toujours():
    texte = """Look Cycle International
Activité : Fabrication de bicyclettes et d'articles de sport
Adresse : 41 boulevard Camille Dagonneau
Ville : Varennes-Vauzelles
Code postal : 58640
Téléphone : 03 86 71 63 00
Email : contact@lookcycle.fr
Site web : https://www.lookcycle.com
Dirigeant : LOOK CYCLE HOLDING"""

    resultats = text_parser.parse_pasted_text(texte)
    assert len(resultats) == 1
    r = resultats[0]
    assert r["nom_entreprise"] == "Look Cycle International"
    assert r["telephone"] == "03 86 71 63 00"
    assert r["email"] == "contact@lookcycle.fr"


def test_texte_totalement_aplati_avec_nom_labellise():
    """Le nouveau prompt suggéré ajoute un libellé 'Nom :' explicite —
    avec lui, même un texte totalement aplati (zéro saut de ligne, zéro
    espace entre les champs) doit se découper correctement en plusieurs
    fiches distinctes."""
    texte = (
        "Nom : Look Cycle InternationalActivité : Fabrication de cyclesAdresse : "
        "41 boulevard Camille DagonneauVille : Varennes-VauzellesCode postal : 58640"
        "Téléphone : 03 86 71 63 00Email : contact@lookcycle.frSite web : "
        "https://www.lookcycle.comDirigeant : LOOK CYCLE HOLDING"
        "Nom : Financière MoustacheActivité : Conception de vélos électriquesAdresse : "
        "5 allée 2Ville : Thaon-les-VosgesCode postal : 88150"
        "Téléphone : 03 29 37 58 65Email :Site web : https://moustachebikes.com"
        "Dirigeant :"
    )
    resultats = text_parser.parse_pasted_text(texte)
    assert len(resultats) == 2
    assert resultats[0]["nom_entreprise"] == "Look Cycle International"
    assert resultats[0]["telephone"] == "03 86 71 63 00"
    assert resultats[0]["contact_nom"] == "LOOK CYCLE HOLDING"
    assert resultats[1]["nom_entreprise"] == "Financière Moustache"
    assert resultats[1]["site_web"] == "https://moustachebikes.com"


def test_ne_coupe_pas_en_plein_milieu_de_telephone_ou_email():
    """Régression : 'phone' et 'mail' sont des sous-chaînes de 'Téléphone'
    et 'Email' — ne doivent jamais produire de fiches fantômes 'Télé' ou 'E'."""
    texte = (
        "Nom : TestActivité : ConseilAdresse : 1 rue TestVille : ParisCode postal : 75000"
        "Téléphone : 0102030405Email : test@exemple.frSite web : https://exemple.fr"
        "Dirigeant : Jean Dupont"
    )
    resultats = text_parser.parse_pasted_text(texte)
    assert len(resultats) == 1
    r = resultats[0]
    assert r["nom_entreprise"] == "Test"
    assert r["telephone"] == "0102030405"
    assert r["email"] == "test@exemple.fr"
    # Ni "Télé" ni "E" ne doivent apparaître comme noms d'entreprise ailleurs
    assert not any(v == "Télé" for v in r.values())


def test_ancien_format_sans_libelle_nom_reste_documente_comme_limite():
    """Sans libellé devant le nom (ancien format du prompt, ou texte déjà
    copié avant cette correction), il est impossible de savoir de façon
    fiable où une fiche se termine et où la suivante commence dans un texte
    totalement aplati — comportement dégradé mais pas de crash."""
    texte = (
        "Look Cycle InternationalActivité : Fabrication de cyclesAdresse : "
        "41 boulevard Camille DagonneauDirigeant : LOOK CYCLE HOLDING"
        "Financière MoustacheActivité : Conception de vélos électriquesDirigeant :"
    )
    resultats = text_parser.parse_pasted_text(texte)
    # Ne plante pas, renvoie au moins quelque chose de exploitable (même si
    # imparfait) plutôt qu'une erreur "aucune entreprise reconnue".
    assert len(resultats) >= 1
