"""KB service layer — the only writer of Category / Article (I5).

Every Article write flows through `sanitise_html` (I7) before hitting the DB, and
the FTS5 triggers in migration 0002 keep the search index in step automatically.
"""

import logging
import re
import secrets
from html import unescape

import bleach
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.text import slugify

from apps.core import exceptions as exc
from apps.kb.models import Article, Category

log = logging.getLogger("kb.services")


# --- sanitisation allowlist -------------------------------------------------
#
# Tight enough for a WYSIWYG output, generous enough to preserve authored formatting.
# `<span class>` supports Quill's inline styling classes; `<img>` allows remote URLs
# only (no data: URIs, no javascript:).

ALLOWED_TAGS = {
    "p", "br", "strong", "em", "u", "s", "a", "ul", "ol", "li",
    "blockquote", "code", "pre", "h1", "h2", "h3", "h4", "hr",
    "img", "span",
}
ALLOWED_ATTRS = {
    "a": ["href", "title", "rel", "target"],
    "img": ["src", "alt", "title"],
    "span": ["class"],
}
ALLOWED_PROTOCOLS = ["http", "https", "mailto"]

_TAG_RE = re.compile(r"<[^>]+>")
_MULTI_WS_RE = re.compile(r"\s+")


def sanitise_html(html: str) -> str:
    """bleach.clean + linkify with the KB allowlist. Never trust the WYSIWYG output —
    the CDN-loaded Quill runs in the customer's browser; treat its DOM like raw
    user input.
    """
    cleaned = bleach.clean(
        html or "",
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRS,
        protocols=ALLOWED_PROTOCOLS,
        strip=True,
        strip_comments=True,
    )
    return bleach.linkify(cleaned)


def html_to_text(html: str) -> str:
    """Strip tags for the FTS-indexed `body_text` column. Not a general-purpose HTML
    renderer — just enough to give FTS5 tokenisable words.
    """
    if not html:
        return ""
    text = _TAG_RE.sub(" ", html)
    return _MULTI_WS_RE.sub(" ", unescape(text)).strip()


# --- slugs ------------------------------------------------------------------

def _unique_slug(*, model, workspace, base: str, exclude_id=None) -> str:
    """Slug-with-collision-suffix loop. Mirrors accounts.services._create_workspace."""
    base = (slugify(base) or "item")[:240]
    qs = model.objects.filter(workspace=workspace, slug=base)
    if exclude_id is not None:
        qs = qs.exclude(id=exclude_id)
    if not qs.exists():
        return base
    for _ in range(6):
        candidate = f"{base}-{secrets.token_hex(2)}"
        qs2 = model.objects.filter(workspace=workspace, slug=candidate)
        if exclude_id is not None:
            qs2 = qs2.exclude(id=exclude_id)
        if not qs2.exists():
            return candidate
    raise exc.SlugCollision("Could not allocate a unique slug.")


# --- Category writers -------------------------------------------------------

def create_category(*, workspace, name: str, slug: str | None = None, position: int = 0) -> Category:
    name = (name or "").strip()
    if not name:
        raise exc.ValidationError("Category name is required.")
    slug = _unique_slug(model=Category, workspace=workspace, base=slug or name)
    try:
        with transaction.atomic():
            cat = Category.objects.create(
                workspace=workspace, name=name, slug=slug, position=position
            )
    except IntegrityError:
        raise exc.SlugCollision("A category with that slug already exists.")
    log.info("kb_category_created workspace_id=%s id=%s slug=%s", workspace.id, cat.id, cat.slug)
    return cat


def update_category(
    *, category: Category, name: str | None = None, slug: str | None = None,
    position: int | None = None,
) -> Category:
    fields: list[str] = []
    if name is not None:
        category.name = name.strip()
        fields.append("name")
    if slug is not None:
        category.slug = _unique_slug(
            model=Category, workspace=category.workspace, base=slug, exclude_id=category.id
        )
        fields.append("slug")
    if position is not None:
        category.position = position
        fields.append("position")
    if fields:
        fields.append("updated_at")
        category.save(update_fields=fields)
    return category


def delete_category(*, category: Category) -> None:
    # ON DELETE SET NULL on Article.category — no orphaning; articles become uncategorised.
    log.info("kb_category_deleted workspace_id=%s id=%s", category.workspace_id, category.id)
    category.delete()


# --- Article writers --------------------------------------------------------

def create_article(
    *, workspace, author, title: str, body_html: str = "",
    category: Category | None = None, slug: str | None = None,
) -> Article:
    title = (title or "").strip()
    if not title:
        raise exc.ValidationError("Article title is required.")
    if category is not None and category.workspace_id != workspace.id:
        raise exc.ValidationError("Category belongs to another workspace.")
    body_html = sanitise_html(body_html)
    body_text = html_to_text(body_html)
    slug = _unique_slug(model=Article, workspace=workspace, base=slug or title)
    try:
        with transaction.atomic():
            art = Article.objects.create(
                workspace=workspace,
                author=author,
                title=title,
                slug=slug,
                body_html=body_html,
                body_text=body_text,
                category=category,
                is_published=False,
            )
    except IntegrityError:
        raise exc.SlugCollision("An article with that slug already exists.")
    log.info("kb_article_created workspace_id=%s id=%s slug=%s", workspace.id, art.id, art.slug)
    return art


def update_article(
    *, article: Article, title: str | None = None, body_html: str | None = None,
    category: Category | None | object = ...,  # sentinel so caller can set None explicitly
    slug: str | None = None,
) -> Article:
    fields: list[str] = []
    if title is not None:
        article.title = title.strip()
        fields.append("title")
    if body_html is not None:
        article.body_html = sanitise_html(body_html)
        article.body_text = html_to_text(article.body_html)
        fields.extend(["body_html", "body_text"])
    if category is not ...:
        if category is not None and category.workspace_id != article.workspace_id:
            raise exc.ValidationError("Category belongs to another workspace.")
        article.category = category
        fields.append("category")
    if slug is not None:
        article.slug = _unique_slug(
            model=Article, workspace=article.workspace, base=slug, exclude_id=article.id
        )
        fields.append("slug")
    if fields:
        fields.append("updated_at")
        article.save(update_fields=fields)
    return article


def publish_article(*, article: Article) -> Article:
    fields = ["is_published", "updated_at"]
    article.is_published = True
    if article.published_at is None:  # stamp only on FIRST publish
        article.published_at = timezone.now()
        fields.insert(1, "published_at")
    article.save(update_fields=fields)
    log.info(
        "kb_article_published workspace_id=%s id=%s slug=%s",
        article.workspace_id, article.id, article.slug,
    )
    return article


def unpublish_article(*, article: Article) -> Article:
    if article.is_published:
        article.is_published = False
        article.save(update_fields=["is_published", "updated_at"])
        log.info("kb_article_unpublished workspace_id=%s id=%s", article.workspace_id, article.id)
    return article


def delete_article(*, article: Article) -> None:
    log.info("kb_article_deleted workspace_id=%s id=%s", article.workspace_id, article.id)
    article.delete()
