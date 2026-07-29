"""Phase 9 custom domains. Tests are offline — the DNS verifier is a stub
(services.verify_domain immediately flips verified_at); production wiring is
documented in the service's docstring + README.

The invariants guarded here:
  - `create_domain` normalises + rejects garbage + fails hard on duplicates
  - `verify_domain` stamps `verified_at`
  - Middleware routes Host → verified Domain → workspace, and specifically
    NEVER routes via a Domain row on well-known dashboard hosts
"""

from __future__ import annotations

import pytest
from django.test import RequestFactory

from apps.accounts import services
from apps.accounts.models import Domain
from apps.core.exceptions import ServiceError, SlugCollision, ValidationError
from apps.core.middleware import TenantMiddleware

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _allow_all_hosts(settings):
    """Middleware calls `request.get_host()` which validates against ALLOWED_HOSTS.
    Production runs behind Railway with `ALLOWED_HOSTS=['*']`; tests mirror that
    so custom-domain hostnames don't trigger `DisallowedHost`.
    """
    settings.ALLOWED_HOSTS = ["*"]


# --- create_domain ----------------------------------------------------------


def test_create_domain_normalises_and_generates_token(admin_a):
    d = services.create_domain(workspace=admin_a.workspace, hostname="  HELP.example.COM ")
    assert d.hostname == "help.example.com"
    assert d.verify_token  # non-empty
    assert d.verified_at is None


def test_create_domain_rejects_missing_or_bare_word(admin_a):
    for bad in ["", "   ", "localhost"]:
        with pytest.raises(ValidationError):
            services.create_domain(workspace=admin_a.workspace, hostname=bad)


def test_create_domain_duplicate_hostname_raises(admin_a, admin_b):
    services.create_domain(workspace=admin_a.workspace, hostname="help.example.com")
    with pytest.raises(SlugCollision):
        # Even a different workspace can't claim the same hostname — one host
        # points to one workspace.
        services.create_domain(workspace=admin_b.workspace, hostname="help.example.com")


# --- verify_domain (stub) --------------------------------------------------


def test_verify_domain_stub_marks_verified(admin_a):
    d = services.create_domain(workspace=admin_a.workspace, hostname="help.a.example")
    assert d.verified_at is None
    services.verify_domain(domain=d)
    d.refresh_from_db()
    assert d.verified_at is not None


# --- middleware routing ----------------------------------------------------


def _run_middleware(host: str, path: str = "/"):
    """Build a bare Request through TenantMiddleware and return the request
    object after middleware annotates it. Uses a no-op get_response so we
    inspect the state without hitting a real view.
    """
    rf = RequestFactory()
    request = rf.get(path, HTTP_HOST=host)
    # AnonymousUser so _resolve_dashboard bails out cleanly.
    from django.contrib.auth.models import AnonymousUser

    request.user = AnonymousUser()

    def _noop(_req):
        from django.http import HttpResponse

        return HttpResponse("ok")

    TenantMiddleware(_noop)(request)
    return request


def test_middleware_host_resolves_verified_domain(admin_a):
    d = services.create_domain(workspace=admin_a.workspace, hostname="help.a.example")
    services.verify_domain(domain=d)
    request = _run_middleware(host="help.a.example", path="/")
    assert request.workspace is not None
    assert request.workspace.id == admin_a.workspace.id
    assert request.is_custom_domain is True


def test_middleware_ignores_unverified_domain(admin_a):
    # Create but don't verify.
    services.create_domain(workspace=admin_a.workspace, hostname="unverified.example")
    request = _run_middleware(host="unverified.example", path="/")
    assert request.workspace is None
    assert request.is_custom_domain is False


def test_middleware_ignores_unknown_host(admin_a):
    request = _run_middleware(host="nobody.example", path="/")
    assert request.workspace is None
    assert request.is_custom_domain is False


def test_middleware_never_hijacks_dashboard_hosts(admin_a):
    """A verified Domain row for `localhost` (or an ALLOWED_HOSTS entry) must
    NOT be honoured — otherwise anyone with dashboard access could add a
    Domain(hostname=localhost) and shadow the login page. I6 tenancy.

    `create_domain` normally rejects bare-word hostnames; we bypass the service
    here to construct the exact edge case we want to guard against.
    """
    from django.utils import timezone

    Domain.objects.create(
        workspace=admin_a.workspace,
        hostname="localhost",
        verify_token="test-token",
        verified_at=timezone.now(),
    )
    request = _run_middleware(host="localhost", path="/")
    assert request.workspace is None
    assert request.is_custom_domain is False


def test_middleware_host_wins_over_path_slug(admin_a, admin_b):
    """When both Host resolves AND path has a slug prefix, Host wins (custom
    domain always beats the fallback). This means a customer's own domain can
    never accidentally serve another workspace's content just because the path
    contains a slug for a different workspace.
    """
    d = services.create_domain(workspace=admin_a.workspace, hostname="help.a.example")
    services.verify_domain(domain=d)
    request = _run_middleware(host="help.a.example", path=f"/kb/{admin_b.workspace.slug}/")
    assert request.workspace.id == admin_a.workspace.id
    assert request.is_custom_domain is True
