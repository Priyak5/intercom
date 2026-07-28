"""Phase 6 KB sanitisation (CLAUDE.md I7). Bleach runs on write; templates render
body_html with |safe. The invariant this file guards: no matter what the WYSIWYG
posts, the stored + rendered HTML has no `<script>`, no `on*` event attributes,
no `javascript:` URLs.
"""

import re

import pytest

from apps.kb import services as kb_services

pytestmark = pytest.mark.django_db


def _load_client_for(user):
    from rest_framework.test import APIClient

    c = APIClient()
    c.force_login(user)
    return c


XSS_PAYLOADS = [
    "<script>alert('pwn')</script><p>ok</p>",
    "<img src=x onerror=alert('x')>",
    "<a href=\"javascript:alert(1)\">click me</a>",
    "<p onmouseover=alert('m')>hover</p>",
    "<iframe src=\"data:text/html,<script>alert(1)</script>\"></iframe>",
    "<svg><script>alert(1)</script></svg>",
    "<style>body { background: url('javascript:alert(1)') }</style>",
    "<a href=\"http://evil.example.com/?q=<script>alert(1)</script>\">bad link</a>",
]


@pytest.mark.parametrize("payload", XSS_PAYLOADS)
def test_bleach_neutralises_payload(admin_a, payload):
    """The security property is that no dangerous *executable HTML* survives: no
    `<script>`, `<iframe>`, `<style>` tags, no `on*=` handlers, no `javascript:` URL
    attributes. Bleach's `strip=True` leaves inner text of stripped tags — that's
    OK because it renders as inert text, not code.
    """
    article = kb_services.create_article(
        workspace=admin_a.workspace, author=admin_a.user,
        title="Attack surface",
        body_html=payload,
    )
    kb_services.publish_article(article=article)
    body = article.body_html.lower()
    # No dangerous elements survive.
    for banned_tag in ("<script", "<iframe", "<style", "<svg"):
        assert banned_tag not in body, f"{banned_tag} tag survived sanitisation of {payload!r}"
    # No inline event handlers.
    assert not re.search(r"\son\w+\s*=", body), f"event handler survived {payload!r}"
    # No javascript:/data: URLs in *attributes* (surviving text is fine).
    assert not re.search(r'(?:href|src)\s*=\s*"?\s*javascript:', body)
    assert not re.search(r'(?:href|src)\s*=\s*"?\s*data:', body)


def test_public_rendering_has_no_script_tag(admin_a, client):
    """End-to-end regression guard: an XSS-shaped article rendered through the
    public template contains no `<script>` tag markup. Stripped inner text is safe.
    """
    article = kb_services.create_article(
        workspace=admin_a.workspace, author=admin_a.user,
        title="Rendered attack",
        body_html="<script>window.pwned = true</script><p>ok content</p>",
    )
    kb_services.publish_article(article=article)
    url = f"/kb/{admin_a.workspace.slug}/a/{article.slug}/"
    resp = client.get(url)
    assert resp.status_code == 200
    text = resp.content.decode("utf-8", errors="replace").lower()
    assert "ok content" in text  # legitimate content survives
    assert "<script" not in text  # the tag does not


def test_linkify_produces_allowlisted_anchor(admin_a):
    article = kb_services.create_article(
        workspace=admin_a.workspace, author=admin_a.user,
        title="Bare URLs",
        body_html="<p>See https://example.com/help for more.</p>",
    )
    assert 'href="https://example.com/help"' in article.body_html
    assert "<script" not in article.body_html


def test_body_text_is_tag_free(admin_a):
    """body_text (the FTS-indexed derivation) must contain no HTML — otherwise angle-
    bracket-heavy content would tokenise strangely and inflate the index size.
    """
    article = kb_services.create_article(
        workspace=admin_a.workspace, author=admin_a.user,
        title="Formatting",
        body_html="<p>Hello <strong>world</strong> — <em>welcome</em>.</p>",
    )
    assert "<" not in article.body_text
    assert "hello world" in article.body_text.lower()
