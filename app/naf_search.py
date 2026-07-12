"""
Recherche de codes NAF à partir d'une phrase en langage courant.

Utilise la nomenclature officielle INSEE (732 codes, stockée en base, cf. schema.sql)
et la recherche floue PostgreSQL (pg_trgm + unaccent) : pas d'appel réseau, pas de
consommation du quota IA, résultat instantané.
"""
from app.db import get_db


def search_naf_codes(query, limit=10):
    query = (query or "").strip()
    if not query:
        return []

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH candidates AS (
                    -- correspondance via un synonyme de métier courant (priorité haute)
                    SELECT nc.code, nc.label,
                           GREATEST(similarity(s.term_normalized, immutable_unaccent(lower(%(q)s))), 0.6) AS score
                    FROM naf_synonyms s
                    JOIN naf_codes nc ON nc.code = s.code
                    WHERE s.term_normalized %% immutable_unaccent(lower(%(q)s))
                       OR s.term_normalized ILIKE '%%' || immutable_unaccent(lower(%(q)s)) || '%%'

                    UNION ALL

                    -- correspondance directe sur le libellé officiel INSEE
                    SELECT nc.code, nc.label,
                           similarity(nc.label_normalized, immutable_unaccent(lower(%(q)s))) AS score
                    FROM naf_codes nc
                    WHERE nc.label_normalized %% immutable_unaccent(lower(%(q)s))
                       OR nc.label_normalized ILIKE '%%' || immutable_unaccent(lower(%(q)s)) || '%%'
                )
                SELECT code, label, MAX(score) AS score
                FROM candidates
                GROUP BY code, label
                ORDER BY score DESC
                LIMIT %(limit)s
                """,
                {"q": query, "limit": limit},
            )
            rows = cur.fetchall()
        return [
            {"code": r[0], "label": r[1], "score": round(float(r[2]), 3)}
            for r in rows
        ]
    finally:
        conn.close()
