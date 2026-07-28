"""Phase 6 KB FTS5 search: workspace isolation, BM25 ranking, trigger sync, and
robustness against FTS special-char query strings.
"""

import pytest

from apps.kb import services as kb_services
from apps.kb.search import search

pytestmark = pytest.mark.django_db


@pytest.fixture
def _seed_a(admin_a):
    ws = admin_a.workspace
    a1 = kb_services.create_article(
        workspace=ws, author=admin_a.user,
        title="Billing FAQ",
        body_html="<p>Everything you need about billing and invoices.</p>",
    )
    kb_services.publish_article(article=a1)
    a2 = kb_services.create_article(
        workspace=ws, author=admin_a.user,
        title="Getting started",
        body_html="<p>Sign up and start chatting. Not related to billing.</p>",
    )
    kb_services.publish_article(article=a2)
    a3 = kb_services.create_article(
        workspace=ws, author=admin_a.user,
        title="How to reset your password",
        body_html="<p>Follow these steps.</p>",
    )
    kb_services.publish_article(article=a3)
    # Draft — must never surface for published_only.
    a4 = kb_services.create_article(
        workspace=ws, author=admin_a.user,
        title="Secret billing changes coming soon",
        body_html="<p>Internal draft.</p>",
    )
    return {"ws": ws, "a1": a1, "a2": a2, "a3": a3, "a4": a4}


def test_published_only_excludes_drafts(_seed_a):
    hits = search(workspace=_seed_a["ws"], q="billing", published_only=True)
    titles = [a.title for a in hits]
    assert "Billing FAQ" in titles
    assert "Secret billing changes coming soon" not in titles


def test_bm25_ranking_prefers_title_matches(_seed_a):
    """`Billing FAQ` has the term in the title; `Getting started` only in body.
    The bm25() weights (10, 1, 3) put title-match first.
    """
    hits = search(workspace=_seed_a["ws"], q="billing", published_only=True)
    assert hits[0].title == "Billing FAQ"


def test_cross_workspace_isolation(admin_a, admin_b, _seed_a):
    """B's search must not surface A's articles even though A owns 'billing' content."""
    kb_services.create_article(
        workspace=admin_b.workspace, author=admin_b.user,
        title="B billing article",
        body_html="<p>B only</p>",
    )
    hits_a = search(workspace=admin_a.workspace, q="billing", published_only=False)
    hits_b = search(workspace=admin_b.workspace, q="billing", published_only=False)
    a_ids = {a.id for a in hits_a}
    b_ids = {a.id for a in hits_b}
    assert a_ids.isdisjoint(b_ids)


def test_fts_special_chars_do_not_crash(_seed_a):
    """Users typing FTS-syntax noise ('AND *' etc.) must never 500 the endpoint."""
    for q in ["billing OR *", "\"quotes", "AND ()", "NEAR/5 password"]:
        # Should return a list (possibly empty), not raise.
        hits = search(workspace=_seed_a["ws"], q=q)
        assert isinstance(hits, list)


def test_empty_query_returns_empty(_seed_a):
    assert search(workspace=_seed_a["ws"], q="") == []
    assert search(workspace=_seed_a["ws"], q="    ") == []


def test_update_syncs_fts_index(_seed_a):
    """Rename an article; the FTS trigger updates the mirror row so the new title matches."""
    art = _seed_a["a3"]
    kb_services.update_article(article=art, title="How to reset your entire universe")
    hits = search(workspace=_seed_a["ws"], q="universe", published_only=True)
    assert art.id in {a.id for a in hits}


def test_delete_removes_from_fts_index(_seed_a):
    art = _seed_a["a1"]
    kb_services.delete_article(article=art)
    hits = search(workspace=_seed_a["ws"], q="Billing FAQ", published_only=False)
    assert art.id not in {a.id for a in hits}
