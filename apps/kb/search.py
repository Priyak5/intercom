"""FTS5-backed KB search.

SQLite FTS5's MATCH accepts a mini-query language (`AND`, `OR`, `NOT`, `NEAR`,
`"phrase"`, `col:term`, etc.); untrusted user input can trip syntax errors or hit
columns unintentionally. We tokenise the query and re-quote each token so the user's
input is treated as literal words joined by an implicit AND. SQL injection is already
foreclosed by parameter binding; this is UX + robustness.

Ranking is `bm25(kb_article_fts)` — lower is better. Column weights (title, body_text,
category) can be tuned via the `bm25()` optional args; defaults (1,1,1) already prefer
matches spanning multiple fields, and title being the shortest field wins per-token
relevance naturally. If needed we can pass `bm25(kb_article_fts, 10.0, 1.0, 2.0)` to
boost title.
"""

import logging
import re

from django.db import connection

from apps.kb.models import Article

log = logging.getLogger("kb.search")

# Strip FTS operators; keep word chars, spaces, dashes.
_TOKEN_RE = re.compile(r"[^\w\s-]+", re.UNICODE)


def _prepare_match(q: str) -> str:
    """Turn a free-text query into a safe FTS5 MATCH expression.

    "how do I reset OR *" -> '"how" "do" "I" "reset"' — each token quoted, joined
    with implicit AND. Returns "" if nothing usable is left (caller should short-circuit).
    """
    if not q:
        return ""
    cleaned = _TOKEN_RE.sub(" ", q).strip()
    tokens = [t for t in cleaned.split() if t]
    if not tokens:
        return ""
    # Wrap each in double-quotes so FTS treats it as a literal (also disables col: parsing).
    return " ".join(f'"{t}"' for t in tokens)


def search(*, workspace, q: str, published_only: bool = True, limit: int = 20) -> list[Article]:
    """Return Articles ranked by BM25, workspace-scoped. Empty query → empty list.

    Uses `bm25(kb_article_fts, 10.0, 1.0, 3.0)` so title matches beat body-only matches
    (10:1) and category-name matches sit in between (3:1).
    """
    match = _prepare_match(q)
    if not match:
        return []
    sql = """
        SELECT a.id
        FROM kb_article a
        JOIN kb_article_fts f ON f.rowid = a.rowid
        WHERE a.workspace_id = %s
          AND kb_article_fts MATCH %s
          {published}
        ORDER BY bm25(kb_article_fts, 10.0, 1.0, 3.0)
        LIMIT %s
    """.format(published="AND a.is_published = 1" if published_only else "")
    # SQLite stores UUIDField as 32-char hex (no dashes). The ORM in-fills for you;
    # raw cursor.execute does NOT — passing a UUID object here raises `type 'UUID'
    # is not supported`. Pass the .hex representation explicitly.
    with connection.cursor() as cur:
        cur.execute(sql, [workspace.id.hex, match, limit])
        ranked_ids = [row[0] for row in cur.fetchall()]
    if not ranked_ids:
        return []
    # Preserve rank order. Django parses either UUID form into the same UUID object.
    by_id = {
        a.id.hex: a
        for a in Article.objects.filter(id__in=ranked_ids).select_related("category")
    }
    return [by_id[rid] for rid in ranked_ids if rid in by_id]


def snippet(article: Article, q: str, max_chars: int = 160) -> str:
    """Cheap excerpt: first N chars of body_text with the first query term highlighted
    via `<mark>` tags (safe — body_text has no HTML). Falls back to a leading slice.
    """
    body = article.body_text or ""
    if not body:
        return ""
    match = _prepare_match(q).split()
    lead = body[:max_chars].strip()
    if match:
        first = match[0].strip('"').lower()
        idx = body.lower().find(first)
        if idx >= 0:
            start = max(0, idx - 40)
            lead = body[start : start + max_chars].strip()
    return lead + ("…" if len(body) > len(lead) else "")
