"""Tenant resolution middleware (CLAUDE.md I6).

`request.workspace` and `request.membership` are DERIVED here — never read from a request
body, query param, or header. Sources, by request type:
  - Custom domain (Phase 9): the `Host` header → verified `Domain` row → workspace.
    Tried FIRST so a custom hostname beats the path-based resolver.
  - Public KB (Phase 6): the `<slug>` segment of `/kb/<slug>/...` — no membership required.
  - Dashboard: session's selected `workspace_id`, verified via Membership on every request.

Switching workspaces goes only through `accounts.services.set_selected_workspace`, which
authorises membership before writing the session. A `workspace_id` in a request body or
query param is always ignored.

Widget requests (Phase 3) resolve their tenancy from a signed visitor token INSIDE the
view — not here — because widget endpoints set `authentication_classes = []` and don't
depend on `request.workspace`.
"""

import logging

from django.conf import settings

log = logging.getLogger("core.middleware")

# Prefixes that need NEITHER a dashboard workspace NOR a KB workspace.
EXEMPT_PREFIXES = (
    "/healthz",
    "/admin/",
    "/static/",
    "/login",
    "/signup",
    "/logout",
    "/invite/",
)

# Public-KB path pattern: /kb/<slug>/...  (but NOT /kb/admin/... — that's the dashboard).
PUBLIC_KB_PREFIX = "/kb/"
PUBLIC_KB_ADMIN_PREFIX = "/kb/admin"


def _dashboard_hosts() -> set[str]:
    """Hostnames that always route to the dashboard — never to a custom Domain lookup.

    Includes localhost / 127.0.0.1 (dev) and everything the operator declared in
    `ALLOWED_HOSTS` (minus the `*` wildcard). Anything else is fair game for the
    custom-domain branch.
    """
    hosts = {"localhost", "127.0.0.1"}
    for h in getattr(settings, "ALLOWED_HOSTS", []) or []:
        if h and h != "*":
            hosts.add(h.lower().lstrip("."))
    return hosts


class TenantMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Always present, so any view/permission can read them unconditionally.
        request.workspace = None
        request.membership = None
        # True when Host resolved to a custom Domain — views use this to switch
        # `/` to the KB index instead of the dashboard login.
        request.is_custom_domain = False

        path = request.path
        if not path.startswith(EXEMPT_PREFIXES):
            # Try Host-based routing first (custom domains); fall through to
            # path-based public-KB, then dashboard.
            if not self._resolve_public_kb_by_host(request):
                if path.startswith(PUBLIC_KB_PREFIX) and not path.startswith(PUBLIC_KB_ADMIN_PREFIX):
                    self._resolve_public_kb(request)
                else:
                    self._resolve_dashboard(request)

        return self.get_response(request)

    def _resolve_dashboard(self, request):
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return  # permission classes / @login_required handle rejection

        # Import here to avoid app-registry issues at import time.
        from apps.accounts.models import Membership

        session_ws = request.session.get("workspace_id")
        membership = None
        if session_ws:
            membership = (
                Membership.objects.select_related("workspace")
                .filter(user=user, workspace_id=session_ws)
                .first()
            )
            if membership is None:
                # Stale pointer (user removed, workspace gone): drop it and re-resolve.
                request.session.pop("workspace_id", None)

        if membership is None:
            membership = (
                Membership.objects.select_related("workspace")
                .filter(user=user)
                .order_by("created_at")
                .first()
            )
            if membership is not None:
                request.session["workspace_id"] = str(membership.workspace_id)

        if membership is not None:
            request.workspace = membership.workspace
            request.membership = membership

    def _resolve_public_kb(self, request):
        """Read `<slug>` from `/kb/<slug>/...` and set request.workspace.

        No membership is required — this is the public-readable KB. Views must still
        check `request.workspace is not None` and `article.is_published` before serving.
        """
        from apps.accounts.models import Workspace

        # /kb/<slug>/... → ["", "kb", "<slug>", ...]
        parts = request.path.split("/", 3)
        if len(parts) < 3 or not parts[2]:
            return
        slug = parts[2]
        ws = Workspace.objects.filter(slug=slug).first()
        if ws is not None:
            request.workspace = ws

    def _resolve_public_kb_by_host(self, request) -> bool:
        """Phase 9: map the incoming `Host` header to a verified `Domain` row.

        Skips well-known dashboard hosts (localhost, ALLOWED_HOSTS) so a custom
        Domain row can never shadow the dashboard on the app's own hostname.
        Returns True when the host resolved to a workspace, so the caller skips
        the path-based fallbacks.
        """
        host = request.get_host().split(":", 1)[0].lower()
        if host in _dashboard_hosts():
            return False
        from apps.accounts.models import Domain

        d = (
            Domain.objects.filter(hostname=host, verified_at__isnull=False)
            .select_related("workspace")
            .first()
        )
        if d is None:
            return False
        request.workspace = d.workspace
        request.is_custom_domain = True
        return True
