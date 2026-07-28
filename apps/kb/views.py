"""Dashboard admin views (agents author articles) + public KB views (visitors read).

Two view families live here to keep the app self-contained. `apps/kb/urls.py` wires
the admin routes; `apps/kb/public_urls.py` wires the public ones. Middleware sets
`request.workspace` for both — via session Membership on admin, via URL slug on public.
"""

import logging

from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from apps.core import exceptions as exc
from apps.kb import search as kb_search
from apps.kb import services
from apps.kb.models import Article, Category

log = logging.getLogger("kb.views")


# --- dashboard admin --------------------------------------------------------

@login_required
def admin_list(request):
    if request.membership is None:
        return redirect("dashboard")
    articles = (
        Article.objects.filter(workspace=request.workspace)
        .select_related("category", "author")
        .order_by("-updated_at")
    )
    categories = Category.objects.filter(workspace=request.workspace).order_by("position", "name")
    return render(request, "kb_admin/list.html", {"articles": articles, "categories": categories})


@login_required
@require_http_methods(["GET", "POST"])
def admin_new(request):
    if request.membership is None:
        return redirect("dashboard")
    categories = Category.objects.filter(workspace=request.workspace).order_by("position", "name")
    if request.method == "POST":
        title = request.POST.get("title", "")
        body_html = request.POST.get("body_html", "")
        category_id = request.POST.get("category") or None
        category = None
        if category_id:
            category = Category.objects.filter(workspace=request.workspace, id=category_id).first()
        try:
            article = services.create_article(
                workspace=request.workspace,
                author=request.user,
                title=title,
                body_html=body_html,
                category=category,
            )
        except exc.ServiceError as e:
            return render(
                request, "kb_admin/edit.html",
                {"error": e.detail, "categories": categories,
                 "form": {"title": title, "body_html": body_html, "category_id": category_id}},
                status=400,
            )
        if request.POST.get("action") == "publish":
            services.publish_article(article=article)
        return redirect("kb_admin_edit", article_id=article.id)
    return render(request, "kb_admin/edit.html", {"categories": categories, "article": None})


@login_required
@require_http_methods(["GET", "POST"])
def admin_edit(request, article_id):
    if request.membership is None:
        return redirect("dashboard")
    article = get_object_or_404(
        Article.objects.select_related("category"),
        id=article_id,
        workspace=request.workspace,
    )
    categories = Category.objects.filter(workspace=request.workspace).order_by("position", "name")
    if request.method == "POST":
        title = request.POST.get("title", "")
        body_html = request.POST.get("body_html", "")
        category_id = request.POST.get("category") or None
        category = (
            Category.objects.filter(workspace=request.workspace, id=category_id).first()
            if category_id else None
        )
        try:
            services.update_article(
                article=article, title=title, body_html=body_html, category=category,
            )
        except exc.ServiceError as e:
            return render(
                request, "kb_admin/edit.html",
                {"article": article, "categories": categories, "error": e.detail},
                status=400,
            )
        action = request.POST.get("action")
        if action == "publish":
            services.publish_article(article=article)
        elif action == "unpublish":
            services.unpublish_article(article=article)
        return redirect("kb_admin_edit", article_id=article.id)
    return render(request, "kb_admin/edit.html", {"article": article, "categories": categories})


@login_required
@require_http_methods(["POST"])
def admin_delete(request, article_id):
    if request.membership is None:
        return redirect("dashboard")
    article = get_object_or_404(Article, id=article_id, workspace=request.workspace)
    services.delete_article(article=article)
    return redirect("kb_admin_list")


@login_required
@require_http_methods(["GET", "POST"])
def admin_categories(request):
    if request.membership is None:
        return redirect("dashboard")
    if request.method == "POST":
        try:
            services.create_category(workspace=request.workspace, name=request.POST.get("name", ""))
        except exc.ServiceError as e:
            categories = Category.objects.filter(workspace=request.workspace).order_by("position", "name")
            return render(
                request, "kb_admin/categories.html",
                {"categories": categories, "error": e.detail}, status=400,
            )
        return redirect("kb_admin_categories")
    categories = Category.objects.filter(workspace=request.workspace).order_by("position", "name")
    return render(request, "kb_admin/categories.html", {"categories": categories})


@login_required
@require_http_methods(["POST"])
def admin_category_delete(request, category_id):
    if request.membership is None:
        return redirect("dashboard")
    cat = get_object_or_404(Category, id=category_id, workspace=request.workspace)
    services.delete_category(category=cat)
    return redirect("kb_admin_categories")


# --- public KB --------------------------------------------------------------
#
# `request.workspace` is set by TenantMiddleware._resolve_public_kb from the URL slug.
# If None: the slug didn't match any workspace → 404.

def _require_public_workspace(request):
    if request.workspace is None:
        raise Http404("Unknown workspace.")
    return request.workspace


def public_index(request, slug):
    ws = _require_public_workspace(request)
    categories = ws.kb_categories.order_by("position", "name")
    articles = (
        Article.objects.filter(workspace=ws, is_published=True)
        .select_related("category")
        .order_by("-published_at")[:20]
    )
    return render(
        request, "public_kb/index.html",
        {"workspace": ws, "categories": categories, "articles": articles},
    )


def public_category(request, slug, category_slug):
    ws = _require_public_workspace(request)
    category = get_object_or_404(Category, workspace=ws, slug=category_slug)
    articles = (
        Article.objects.filter(workspace=ws, category=category, is_published=True)
        .order_by("-published_at")
    )
    return render(
        request, "public_kb/category.html",
        {"workspace": ws, "category": category, "articles": articles},
    )


def public_article(request, slug, article_slug):
    ws = _require_public_workspace(request)
    article = get_object_or_404(
        Article.objects.select_related("category"),
        workspace=ws, slug=article_slug, is_published=True,
    )
    return render(
        request, "public_kb/article.html", {"workspace": ws, "article": article},
    )


def public_search(request, slug):
    ws = _require_public_workspace(request)
    q = (request.GET.get("q") or "").strip()
    articles = kb_search.search(workspace=ws, q=q, published_only=True, limit=25) if q else []
    results = [{"article": a, "snippet": kb_search.snippet(a, q)} for a in articles]
    return render(
        request, "public_kb/search.html",
        {"workspace": ws, "q": q, "results": results},
    )
