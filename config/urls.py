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
    path("", include("apps.inbox.urls")),
    path("", include("apps.accounts.urls")),
]
