"""Public KB routes — mounted at /kb/<slug>/. Tenant is derived from the slug by
TenantMiddleware; views 404 if it didn't resolve.
"""

from django.urls import path

from apps.kb import views

urlpatterns = [
    path("", views.public_index, name="kb_public_index"),
    path("search", views.public_search, name="kb_public_search"),
    path("c/<slug:category_slug>/", views.public_category, name="kb_public_category"),
    path("a/<slug:article_slug>/", views.public_article, name="kb_public_article"),
]
