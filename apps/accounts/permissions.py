"""DRF permissions built on the workspace TenantMiddleware attaches (I6).

`request.membership` is only ever a membership of `request.user` (the middleware never
attaches a workspace the user isn't in), so these classes are the per-view enforcement
point for tenancy + roles.
"""

from rest_framework.permissions import BasePermission

from apps.accounts.models import Role


class IsWorkspaceMember(BasePermission):
    message = "You must be a member of a workspace."

    def has_permission(self, request, view):
        return bool(getattr(request, "membership", None))


class IsWorkspaceAdmin(BasePermission):
    message = "Admin role required."

    def has_permission(self, request, view):
        membership = getattr(request, "membership", None)
        return bool(membership and membership.role == Role.ADMIN)
