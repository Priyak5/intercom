"""Server-rendered dashboard/auth pages. Thin: read the form, call a service, render.

JSON/team-management lives in api.py; these handle signup/login/logout/invite-accept,
the dashboard shell, and the workspace switcher (all session + form-CSRF).
"""

import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login as auth_login
from django.contrib.auth import logout as auth_logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import redirect_to_login
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from apps.accounts import services
from apps.accounts.models import Domain, Invite, Role
from apps.core.exceptions import ServiceError

log = logging.getLogger("accounts.views")

_BACKEND = "django.contrib.auth.backends.ModelBackend"


@require_http_methods(["GET", "POST"])
def signup(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    if request.method == "POST":
        try:
            membership = services.sign_up(
                email=request.POST.get("email", ""),
                password=request.POST.get("password", ""),
                name=request.POST.get("name", ""),
                workspace_name=request.POST.get("workspace_name", ""),
            )
        except ServiceError as e:
            messages.error(request, e.detail)
            return render(request, "accounts/signup.html", {"form": request.POST})
        auth_login(request, membership.user, backend=_BACKEND)
        return redirect("dashboard")
    return render(request, "accounts/signup.html", {})


@require_http_methods(["GET", "POST"])
def login(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    if request.method == "POST":
        from django.contrib.auth import authenticate

        user = authenticate(
            request,
            username=request.POST.get("email", "").strip().lower(),
            password=request.POST.get("password", ""),
        )
        if user is None:
            messages.error(request, "Invalid email or password.")
            return render(request, "accounts/login.html", {"form": request.POST})
        auth_login(request, user)
        return redirect(request.GET.get("next") or "dashboard")
    return render(request, "accounts/login.html", {})


@require_POST
def logout(request):
    auth_logout(request)
    return redirect("login")


def dashboard(request):
    # A visitor hitting the root of a custom domain (help.acme.com/) should land
    # on that workspace's public KB, not on the dashboard login. Phase 9.
    if getattr(request, "is_custom_domain", False) and request.workspace is not None:
        return redirect("kb_public_index", slug=request.workspace.slug)
    if not request.user.is_authenticated:
        return redirect_to_login(request.get_full_path())
    return render(request, "dashboard.html", {})


@login_required
def team(request):
    if request.membership is None:
        messages.error(request, "No workspace selected.")
        return redirect("dashboard")
    return render(
        request,
        "accounts/team.html",
        {
            "members": services.list_members(workspace=request.workspace),
            "is_admin": request.membership.role == Role.ADMIN,
            "roles": Role.choices,
        },
    )


@require_POST
@login_required
def workspace_switch(request):
    try:
        services.set_selected_workspace(
            request=request, workspace_id=request.POST.get("workspace_id", "")
        )
    except ServiceError as e:
        messages.error(request, e.detail)
    return redirect(request.META.get("HTTP_REFERER") or "dashboard")


# --- custom domains (Phase 9) ---------------------------------------------
#
# Admin-only. Cross-workspace requests 404 (workspace-scoped fetches, I6). The
# `verify` action calls a stubbed service; production DNS verification is the
# tradeoff documented in README + services.verify_domain's docstring.


def _require_admin(request):
    if request.membership is None or request.membership.role != Role.ADMIN:
        messages.error(request, "Admin role required.")
        return redirect("dashboard")
    return None


@login_required
def domains(request):
    denied = _require_admin(request)
    if denied is not None:
        return denied
    return render(
        request,
        "accounts/domains.html",
        {
            "domains": services.list_domains(workspace=request.workspace),
            "base_host": settings.BASE_HOST,
        },
    )


@require_POST
@login_required
def domain_add(request):
    denied = _require_admin(request)
    if denied is not None:
        return denied
    try:
        services.create_domain(
            workspace=request.workspace,
            hostname=request.POST.get("hostname", ""),
        )
    except ServiceError as e:
        messages.error(request, e.detail)
    return redirect("domains")


@require_POST
@login_required
def domain_verify(request, domain_id):
    denied = _require_admin(request)
    if denied is not None:
        return denied
    domain = get_object_or_404(Domain, id=domain_id, workspace=request.workspace)
    services.verify_domain(domain=domain)
    return redirect("domains")


@require_POST
@login_required
def domain_delete(request, domain_id):
    denied = _require_admin(request)
    if denied is not None:
        return denied
    domain = get_object_or_404(Domain, id=domain_id, workspace=request.workspace)
    services.delete_domain(domain=domain)
    return redirect("domains")


@require_http_methods(["GET", "POST"])
def invite_accept(request, token):
    invite = (
        Invite.objects.select_related("workspace").filter(token=token).first()
    )
    if invite is None or not invite.is_valid:
        return render(request, "accounts/invite_accept.html", {"invalid": True})

    if request.method == "POST":
        try:
            membership = services.accept_invite(
                token=token,
                password=request.POST.get("password", ""),
                name=request.POST.get("name", ""),
            )
        except ServiceError as e:
            messages.error(request, e.detail)
            return render(request, "accounts/invite_accept.html", {"invite": invite})
        auth_login(request, membership.user, backend=_BACKEND)
        return redirect("dashboard")

    return render(request, "accounts/invite_accept.html", {"invite": invite})
