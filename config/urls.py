"""Root URL configuration.

Thin: routes to app view modules. `/healthz` is wired directly because it must work
even before any feature app has URLs.
"""

from django.contrib import admin
from django.urls import include, path

from apps.core.health import healthz

urlpatterns = [
    path("healthz", healthz, name="healthz"),
    path("admin/", admin.site.urls),
    path("api/", include("apps.accounts.api_urls")),
    path("api/", include("apps.inbox.api_urls")),
    # KB admin (session-authed) is /kb/admin/…; public KB is /kb/<slug>/…. Middleware
    # picks the right resolver based on the path (see apps/core/middleware.py).
    path("kb/", include("apps.kb.urls")),
    path("kb/<slug:slug>/", include("apps.kb.public_urls")),
    path("", include("apps.inbox.urls")),
    path("", include("apps.accounts.urls")),
]
