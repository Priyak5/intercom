"""Knowledge base: Category + Article, per-workspace unique slugs.

`body_html` is stored already sanitised by the service layer (I7). `body_text` is a
plain-text derivation used only by FTS5 (kept in a real column so the FTS triggers
in migration 0002 can mirror it cheaply). Slugs are unique WITHIN a workspace — two
workspaces can each have a `pricing` article without collision (I6 tenancy).

The row's `id` (BaseModel UUID) is the FK target from FTS content='kb_article'; the
FTS virtual table uses SQLite's implicit rowid to link rows.
"""

from django.db import models

from apps.core.models import BaseModel


class Category(BaseModel):
    workspace = models.ForeignKey(
        "accounts.Workspace", on_delete=models.CASCADE, related_name="kb_categories"
    )
    name = models.CharField(max_length=128)
    slug = models.SlugField(max_length=128)
    position = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["workspace", "slug"], name="uniq_kb_category_slug"),
        ]
        indexes = [models.Index(fields=["workspace", "position"])]

    def __str__(self) -> str:
        return f"{self.workspace_id}:{self.slug}"


class Article(BaseModel):
    workspace = models.ForeignKey(
        "accounts.Workspace", on_delete=models.CASCADE, related_name="kb_articles"
    )
    category = models.ForeignKey(
        "kb.Category",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="articles",
    )
    title = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255)
    body_html = models.TextField(blank=True)
    body_text = models.TextField(blank=True)  # tag-stripped; drives FTS5 index
    is_published = models.BooleanField(default=False)
    published_at = models.DateTimeField(null=True, blank=True)
    author = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="kb_articles",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["workspace", "slug"], name="uniq_kb_article_slug"),
        ]
        indexes = [
            models.Index(fields=["workspace", "is_published", "-published_at"]),
            models.Index(fields=["workspace", "category"]),
        ]

    def __str__(self) -> str:
        return f"{self.workspace_id}:{self.slug}"
