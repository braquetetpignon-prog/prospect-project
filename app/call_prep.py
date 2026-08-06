"""
Aide à la préparation du premier appel prospect ("Préparer mon appel").

Génère un support d'appel (accroche, pitch express, questions ouvertes,
traitement d'objections) adapté à 2 axes croisés : le secteur d'activité du
prospect (naf_code -> libellé lisible via naf_codes, cf. naf_search.py) et le
type de contact (premier_contact / rappel / proposition_particuliere).

HISTORIQUE — 6 août 2026 : première version basée sur un appel Gemini
(même pattern que ia_search.py). Abandonnée le jour même après plusieurs
timeouts réseau vers generativelanguage.googleapis.com constatés sur
l'ensemble du site (pas spécifique à cette fonctionnalité). Remplacée par
des phrases types locales, sans aucun appel réseau : plus rapide, jamais
en panne, zéro coût, et le contenu reste maîtrisé (basé sur la structure
"5 piliers d'un appel réussi" fournie par Alexis : objectif, profil flash,
accroche, questions, traitement des objections).

Choix volontaire : pas de nouveau champ sur la fiche prospect. Le seul champ
"métier" disponible aujourd'hui est naf_code (code officiel INSEE, déjà
présent sur prospects) — on le résout en libellé humain via la table
naf_codes existante plutôt que d'ajouter un champ dédié.

Cache par workspace, conservé même sans coût de génération : la génération
étant déterministe (même secteur + même type de contact = même texte), le
cache ne sert plus à économiser un appel, mais à garantir que tous les
utilisateurs d'un même workspace voient exactement le même texte pour un
même secteur — cohérence de discours dans l'équipe plutôt qu'optimisation
de coût. Le quota quotidien (Gemini) a disparu avec l'appel réseau : plus de
raison de limiter une génération locale instantanée.
"""
import json

from app.db import get_db

TYPES_CONTACT = ("premier_contact", "rappel", "proposition_particuliere")
TYPE_CONTACT_LABELS = {
    "premier_contact": "premier contact",
    "rappel": "rappel",
    "proposition_particuliere": "proposition particulière",
}

DISCLAIMER_VERSION = 1  # incrémenter si le texte du bandeau/consentement change


class CallPrepError(Exception):
    """Erreur métier — message destiné à être renvoyé tel quel à l'utilisateur."""


class ConsentRequired(CallPrepError):
    pass


def get_naf_label(naf_code):
    if not naf_code:
        return None
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT label FROM naf_codes WHERE code = %s", (naf_code,))
            row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _normalize_secteur_key(naf_code):
    """Clé de cache stable : le code NAF si connu, sinon un fallback textuel —
    jamais None (deux NULL ne s'égalent jamais en SQL, ce qui casserait la
    contrainte UNIQUE et empêcherait toute réutilisation entre prospects sans NAF)."""
    return (naf_code or "").strip() or "non_renseigne"


# --- Consentement (case à cocher, une fois par utilisateur) ---------------

def has_consented(user_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM call_prep_consent WHERE user_id = %s AND disclaimer_version = %s",
                (user_id, DISCLAIMER_VERSION),
            )
            return cur.fetchone() is not None
    finally:
        conn.close()


def record_consent(user_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO call_prep_consent (user_id, disclaimer_version)
                VALUES (%s, %s)
                ON CONFLICT (user_id, disclaimer_version) DO NOTHING
                """,
                (user_id, DISCLAIMER_VERSION),
            )
        conn.commit()
    finally:
        conn.close()


# --- Cache partagé par workspace (secteur, type_contact) -------------------
# Conservé pour la cohérence de discours dans l'équipe (voir docstring),
# plus pour une raison de coût.

def _get_cached(workspace_id, secteur_key, type_contact):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, texte_genere FROM call_prep_cache
                WHERE workspace_id = %s AND secteur_key = %s AND type_contact = %s
                """,
                (workspace_id, secteur_key, type_contact),
            )
            row = cur.fetchone()
        if not row:
            return None
        cache_id, texte = row
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE call_prep_cache SET usage_count = usage_count + 1 WHERE id = %s",
                (cache_id,),
            )
        conn.commit()
        return texte
    finally:
        conn.close()


def _store_cache(workspace_id, secteur_key, type_contact, texte_genere, user_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO call_prep_cache (workspace_id, secteur_key, type_contact, texte_genere, created_by)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (workspace_id, secteur_key, type_contact) DO NOTHING
                """,
                (workspace_id, secteur_key, type_contact, json.dumps(texte_genere), user_id),
            )
        conn.commit()
    finally:
        conn.close()


# --- Phrases types (5 piliers) — aucun appel réseau, résultat déterministe -

def _build_template(secteur_label, type_contact):
    secteur = secteur_label or "votre secteur d'activité"

    if type_contact == "premier_contact":
        return {
            "accroche": (
                f"Bonjour [Prénom], c'est [Votre nom] de [Votre société]. "
                f"Je vous contacte car nous accompagnons des entreprises du secteur "
                f"« {secteur} » comme la vôtre, et je me permets un rapide appel à ce sujet."
            ),
            "pitch": (
                f"En quelques mots, nous aidons les entreprises du secteur « {secteur} » "
                f"à gagner du temps sur leur gestion commerciale au quotidien. "
                f"Je ne vous prends que quelques minutes pour comprendre votre situation."
            ),
            "questions": [
                "Comment gérez-vous ce sujet au quotidien actuellement ?",
                "Quel est votre plus grand défi sur ce point pour ce trimestre ?",
                "Qu'est-ce qui vous ferait dire que ça vaut le coup d'en reparler ?",
            ],
            "objections": [
                {
                    "objection": "Je n'ai pas le temps là",
                    "reponse": (
                        "Je comprends tout à fait, vous êtes en plein travail. "
                        "C'est justement pour ça que je vous propose un échange de 10 min "
                        "plutôt que de vous retenir maintenant — mardi ou jeudi, quel jour vous convient le mieux ?"
                    ),
                },
                {
                    "objection": "Ça ne m'intéresse pas",
                    "reponse": (
                        "Pas de souci, je respecte ça. Est-ce que je peux juste vous demander "
                        "ce qui manquerait pour que ce soit pertinent pour vous, pour ne pas vous solliciter à tort une prochaine fois ?"
                    ),
                },
            ],
        }

    if type_contact == "rappel":
        return {
            "accroche": (
                f"Bonjour [Prénom], c'est [Votre nom], on s'était parlé il y a quelque temps "
                f"au sujet de votre activité dans le secteur « {secteur} ». "
                f"Je me permets de reprendre contact pour faire le point."
            ),
            "pitch": (
                f"Je voulais savoir où vous en étiez de votre réflexion, et si votre "
                f"situation avait évolué depuis notre dernier échange."
            ),
            "questions": [
                "Où en êtes-vous depuis notre dernier échange ?",
                "Qu'est-ce qui a changé de votre côté depuis ?",
                "Qu'est-ce qui vous aiderait à avancer sur ce sujet maintenant ?",
            ],
            "objections": [
                {
                    "objection": "On n'a pas encore décidé",
                    "reponse": (
                        "C'est normal, ce genre de décision prend du temps. "
                        "Qu'est-ce qui vous aiderait à trancher — un point précis que je peux clarifier ?"
                    ),
                },
                {
                    "objection": "On est parti sur autre chose",
                    "reponse": (
                        "D'accord, merci de me le dire clairement. "
                        "Est-ce que je peux vous recontacter dans quelques mois si votre situation évolue ?"
                    ),
                },
            ],
        }

    # proposition_particuliere
    return {
        "accroche": (
            f"Bonjour [Prénom], c'est [Votre nom]. Je vous appelle avec une proposition "
            f"précise, pensée pour une entreprise du secteur « {secteur} » comme la vôtre."
        ),
        "pitch": (
            f"Voici ce que je vous propose concrètement : [détail de l'offre à adapter]. "
            f"C'est pensé pour répondre aux besoins courants du secteur « {secteur} »."
        ),
        "questions": [
            "Qu'est-ce qui compte le plus pour vous dans une proposition comme celle-ci ?",
            "Y a-t-il un point qui vous ferait hésiter sur cette offre telle que je viens de la présenter ?",
        ],
        "objections": [
            {
                "objection": "C'est trop cher",
                "reponse": (
                    "Je comprends que ce soit un point important. "
                    "Qu'est-ce qui vous semble juste par rapport à ce que ça vous apporterait concrètement ?"
                ),
            },
            {
                "objection": "Je dois comparer avec d'autres",
                "reponse": (
                    "C'est tout à fait légitime. Qu'est-ce qui compterait le plus dans votre comparaison, "
                    "pour que je vous donne les bons éléments ?"
                ),
            },
        ],
    }


# --- Orchestration -----------------------------------------------------

def prepare_call(workspace_id, user_id, prospect, type_contact):
    """`prospect` : dict déjà chargé par l'appelant (voir prospects.get_prospect),
    déjà vérifié appartenir au bon workspace — cette fonction ne recharge pas
    le prospect elle-même (même convention que document_send.send_document)."""
    if type_contact not in TYPES_CONTACT:
        raise CallPrepError(f"Type de contact invalide : {type_contact}")

    if not has_consented(user_id):
        raise ConsentRequired(
            "Vous devez d'abord valider l'avertissement d'usage avant de générer un support d'appel."
        )

    naf_code = prospect.get("naf_code")
    secteur_label = get_naf_label(naf_code) or "secteur non renseigné"
    secteur_key = _normalize_secteur_key(naf_code)

    cached = _get_cached(workspace_id, secteur_key, type_contact)
    if cached is not None:
        return {"texte": json.loads(cached) if isinstance(cached, str) else cached, "source": "cache"}

    texte = _build_template(secteur_label, type_contact)
    _store_cache(workspace_id, secteur_key, type_contact, texte, user_id)

    return {"texte": texte, "source": "generation"}
