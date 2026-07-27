"""Tenant resolution middleware (CLAUDE.md I6).

`request.workspace` and `request.membership` are DERIVED here — never read from a request
body, query param, or header. For the dashboard that source is the session's selected
membership, verified against the DB on every request. Switching workspaces goes only
through accounts.services.set_selected_workspace, which authorizes membership before
writing the session.

Widget (Phase 3) and public-KB (Phase 9) resolution are separate paths; this is written
as a dispatcher so they slot in without touching the dashboard path.
"""

import logging

log = logging.getLogger("core.middleware")

# Prefixes that never need a workspace. Anonymous/public surfaces must pass untouched.
EXEMPT_PREFIXES = (
    "/healthz",
    "/admin/",
    "/static/",
    "/login",
    "/signup",
    "/logout",
    "/invite/",
)


class TenantMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Always present, so any view/permission can read them unconditionally.
        request.workspace = None
        request.membership = None

        if not request.path.startswith(EXEMPT_PREFIXES):
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
