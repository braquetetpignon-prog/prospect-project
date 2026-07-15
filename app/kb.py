"""
Base de connaissances (wiki interne) : procédures courtes rédigées par le
superadmin, consultées par les utilisateurs via un panneau latéral ouvert
depuis une icône d'aide contextuelle (ex: à côté du bloc SMTP dans
Paramètres). Totalement distinct de l'assistant d'aide en ligne
(app/assistant.py, IA conversationnelle) — ici c'est du contenu fixe,
rédigé à la main, jamais généré.

Édition (création/modification/suppression) réservée au superadmin depuis
/supadmin. Lecture ouverte à tout utilisateur connecté (login_required),
quel que soit son rôle ou son espace de travail — le contenu n'est pas
sensible et ne dépend pas d'un workspace.
"""
from app.db import get_db


class KbError(Exception):
    pass


_COLUMNS = ["id", "slug", "title", "content", "display_order", "created_at", "updated_at"]


def _row_to_dict(row):
    return dict(zip(_COLUMNS, row))


def list_articles():
    """Liste complète, triée pour l'affichage (panneau supadmin ou sommaire
    utilisateur)."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM kb_articles ORDER BY display_order, title"
            )
            rows = cur.fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def get_article_by_slug(slug):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM kb_articles WHERE slug = %s", (slug,)
            )
            row = cur.fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def get_article(article_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM kb_articles WHERE id = %s", (article_id,)
            )
            row = cur.fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def _normalize_slug(slug):
    slug = (slug or "").strip().lower()
    if not slug:
        raise KbError("L'identifiant (slug) est requis.")
    if not all(c.isalnum() or c == "-" for c in slug):
        raise KbError("L'identifiant ne peut contenir que des lettres, chiffres et tirets.")
    return slug


def create_article(slug, title, content, display_order=0):
    slug = _normalize_slug(slug)
    title = (title or "").strip()
    content = (content or "").strip()
    if not title:
        raise KbError("Le titre est requis.")
    if not content:
        raise KbError("Le contenu est requis.")

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM kb_articles WHERE slug = %s", (slug,))
            if cur.fetchone():
                raise KbError(f"L'identifiant « {slug} » est déjà utilisé.")
            cur.execute(
                """
                INSERT INTO kb_articles (slug, title, content, display_order)
                VALUES (%s, %s, %s, %s) RETURNING id
                """,
                (slug, title, content, display_order or 0),
            )
            new_id = cur.fetchone()[0]
        conn.commit()
        return new_id
    finally:
        conn.close()


def update_article(article_id, slug, title, content, display_order=0):
    slug = _normalize_slug(slug)
    title = (title or "").strip()
    content = (content or "").strip()
    if not title:
        raise KbError("Le titre est requis.")
    if not content:
        raise KbError("Le contenu est requis.")

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM kb_articles WHERE slug = %s AND id != %s", (slug, article_id)
            )
            if cur.fetchone():
                raise KbError(f"L'identifiant « {slug} » est déjà utilisé par un autre article.")
            cur.execute(
                """
                UPDATE kb_articles
                SET slug = %s, title = %s, content = %s, display_order = %s, updated_at = now()
                WHERE id = %s
                RETURNING id
                """,
                (slug, title, content, display_order or 0, article_id),
            )
            updated = cur.fetchone()
        conn.commit()
        if not updated:
            raise KbError("Article introuvable.")
    finally:
        conn.close()


def delete_article(article_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM kb_articles WHERE id = %s RETURNING id", (article_id,))
            deleted = cur.fetchone()
        conn.commit()
        if not deleted:
            raise KbError("Article introuvable.")
    finally:
        conn.close()
