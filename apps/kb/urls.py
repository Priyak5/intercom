"""KB dashboard admin routes — mounted at /kb/admin/. Authenticated dashboard views."""

from django.urls import path

from apps.kb import views

urlpatterns = [
    path("admin/", views.admin_list, name="kb_admin_list"),
    path("admin/new/", views.admin_new, name="kb_admin_new"),
    path("admin/<uuid:article_id>/", views.admin_edit, name="kb_admin_edit"),
    path("admin/<uuid:article_id>/delete", views.admin_delete, name="kb_admin_delete"),
    path("admin/categories/", views.admin_categories, name="kb_admin_categories"),
    path(
        "admin/categories/<uuid:category_id>/delete",
        views.admin_category_delete,
        name="kb_admin_category_delete",
    ),
]
