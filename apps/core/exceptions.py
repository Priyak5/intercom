"""Typed service-layer exceptions and the DRF handler that maps them to status codes.

Services raise these (framework-agnostic, so the same service can be called from a
consumer or poller — I5). Thin DRF views just call a service; `drf_exception_handler`
turns a raised ServiceError into the right HTTP status. Template views catch ServiceError
locally and re-render with a message.
"""

import logging

from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_default_handler

log = logging.getLogger("core.exceptions")


class ServiceError(Exception):
    """Base for all domain errors raised by the service layer."""

    status_code = 400
    code = "error"

    def __init__(self, detail: str = ""):
        self.detail = detail or self.__class__.__name__
        super().__init__(self.detail)


class ValidationError(ServiceError):
    status_code = 400
    code = "validation_error"


class PermissionDeniedError(ServiceError):
    status_code = 403
    code = "permission_denied"


class NotFoundError(ServiceError):
    status_code = 404
    code = "not_found"


class ConflictError(ServiceError):
    status_code = 409
    code = "conflict"


# --- concrete domain errors -------------------------------------------------

class EmailAlreadyExists(ConflictError):
    code = "email_exists"


class SlugCollision(ConflictError):
    code = "slug_collision"


class MembershipNotFound(NotFoundError):
    code = "membership_not_found"


class InviteNotFound(NotFoundError):
    code = "invite_not_found"


class InviteExpired(ValidationError):
    code = "invite_expired"


class InviteAlreadyUsed(ConflictError):
    code = "invite_used"


class InviteAlreadyPending(ConflictError):
    code = "invite_pending"


class AlreadyMember(ConflictError):
    code = "already_member"


class RoleInvalid(ValidationError):
    code = "role_invalid"


class LastAdminError(ConflictError):
    code = "last_admin"


class SelfDemoteError(ValidationError):
    code = "self_demote"


class SelfRemoveError(ValidationError):
    code = "self_remove"


class WorkspaceRequired(PermissionDeniedError):
    code = "workspace_required"


def drf_exception_handler(exc, context):
    """DRF EXCEPTION_HANDLER: ServiceError -> {error, detail} at its status_code;
    everything else falls through to DRF's default handling.
    """
    if isinstance(exc, ServiceError):
        log.warning("service_error code=%s status=%s", exc.code, exc.status_code)
        return Response({"error": exc.code, "detail": exc.detail}, status=exc.status_code)
    return drf_default_handler(exc, context)
