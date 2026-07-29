"""Accounts service layer — the only writer of User/Workspace/Membership/Invite (I5).

Every function is keyword-only, typed, returns domain objects, raises typed errors from
apps.core.exceptions, and logs one structured line on success. Views/consumers are thin
adapters over these.
"""

import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify

from apps.accounts.models import Domain, Invite, Membership, Role, Workspace
from apps.core import exceptions as exc

User = get_user_model()
log = logging.getLogger("accounts.services")


def _norm(email: str) -> str:
    return email.strip().lower()


def _create_workspace(name: str) -> Workspace:
    """Create a Workspace with a unique slug and server-generated keys. Each attempt runs
    in its own savepoint so an IntegrityError doesn't poison the caller's transaction.
    """
    base = slugify(name)[:48] or "workspace"
    for attempt in range(6):
        slug = base if attempt == 0 else f"{base}-{secrets.token_hex(3)}"
        try:
            with transaction.atomic():  # savepoint
                return Workspace.objects.create(
                    name=name,
                    slug=slug,
                    public_key="pk_" + secrets.token_urlsafe(24),
                    hmac_secret=secrets.token_urlsafe(48),
                )
        except IntegrityError:
            continue
    raise exc.SlugCollision("Could not allocate a unique workspace slug.")


def sign_up(*, email: str, password: str, name: str, workspace_name: str) -> Membership:
    """Atomically create User + Workspace + admin Membership. Returns the admin Membership."""
    email = _norm(email)
    if User.objects.filter(email__iexact=email).exists():
        raise exc.EmailAlreadyExists("A user with that email already exists.")
    try:
        validate_password(password, User(email=email, name=name))
    except DjangoValidationError as e:
        raise exc.ValidationError(" ".join(e.messages))
    try:
        with transaction.atomic():
            user = User.objects.create_user(email=email, password=password, name=name)
            workspace = _create_workspace(workspace_name)
            membership = Membership.objects.create(
                user=user, workspace=workspace, role=Role.ADMIN
            )
    except IntegrityError:
        raise exc.EmailAlreadyExists("A user with that email already exists.")
    log.info(
        "signup_ok user_id=%s workspace_id=%s slug=%s",
        user.id, workspace.id, workspace.slug,
    )
    return membership


def _send_invite_email(invite: Invite) -> None:
    url = f"{settings.BASE_URL}{reverse('invite_accept', args=[invite.token])}"
    # fail_silently: the link is also surfaced in the UI, so mail must never 500 a request.
    send_mail(
        subject=f"You're invited to {invite.workspace.name}",
        message=f"Accept your invitation: {url}",
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[invite.email],
        fail_silently=True,
    )


def create_invite(*, workspace: Workspace, inviter: User, email: str, role: str) -> Invite:
    """Admin creates (or refreshes an expired) invite. Returns the Invite; the caller
    builds/surfaces the accept link. Never logs the token or the invitee email.
    """
    email = _norm(email)
    if role not in Role.values:
        raise exc.RoleInvalid(f"Invalid role: {role!r}")
    if Membership.objects.filter(workspace=workspace, user__email__iexact=email).exists():
        raise exc.AlreadyMember("That person is already a member of this workspace.")

    token = secrets.token_urlsafe(32)
    expires_at = timezone.now() + timedelta(hours=settings.INVITE_TTL_HOURS)
    with transaction.atomic():
        pending = (
            Invite.objects.select_for_update()
            .filter(workspace=workspace, email=email, accepted_at__isnull=True)
            .first()
        )
        if pending and pending.is_valid:
            raise exc.InviteAlreadyPending("An invite is already pending for that email.")
        if pending:  # expired pending row: refresh in place (keeps the partial-unique key)
            pending.token = token
            pending.role = role
            pending.expires_at = expires_at
            pending.invited_by = inviter
            pending.save(
                update_fields=["token", "role", "expires_at", "invited_by", "updated_at"]
            )
            invite = pending
        else:
            invite = Invite.objects.create(
                workspace=workspace,
                email=email,
                role=role,
                token=token,
                expires_at=expires_at,
                invited_by=inviter,
            )
    _send_invite_email(invite)
    log.info(
        "invite_created workspace_id=%s role=%s invite_id=%s", workspace.id, role, invite.id
    )
    return invite


def accept_invite(*, token: str, password: str, name: str = "") -> Membership:
    """Accept an invite: create-or-attach the user, create Membership, mark single-use."""
    with transaction.atomic():
        try:
            invite = Invite.objects.select_for_update().select_related("workspace").get(token=token)
        except Invite.DoesNotExist:
            raise exc.InviteNotFound("Invite not found.")
        if invite.accepted_at is not None:
            raise exc.InviteAlreadyUsed("This invite has already been used.")
        if invite.expires_at <= timezone.now():
            raise exc.InviteExpired("This invite has expired.")

        user = User.objects.filter(email__iexact=invite.email).first()
        if user is None:
            if not password:
                raise exc.ValidationError("A password is required to accept this invite.")
            try:
                validate_password(password, User(email=invite.email, name=name))
            except DjangoValidationError as e:
                raise exc.ValidationError(" ".join(e.messages))
            user = User.objects.create_user(
                email=invite.email, password=password, name=name
            )

        membership, _created = Membership.objects.get_or_create(
            user=user, workspace=invite.workspace, defaults={"role": invite.role}
        )
        invite.accepted_at = timezone.now()
        invite.save(update_fields=["accepted_at", "updated_at"])
    log.info(
        "invite_accepted workspace_id=%s user_id=%s role=%s",
        invite.workspace_id, user.id, invite.role,
    )
    return membership


def change_role(
    *, workspace: Workspace, actor: Membership, target_membership_id: str, new_role: str
) -> Membership:
    """Admin changes a member's role. Fetch is scoped to `workspace` (IDOR → 404)."""
    if new_role not in Role.values:
        raise exc.RoleInvalid(f"Invalid role: {new_role!r}")
    with transaction.atomic():
        try:
            target = Membership.objects.select_for_update().get(
                id=target_membership_id, workspace=workspace
            )
        except (Membership.DoesNotExist, ValueError, DjangoValidationError):
            raise exc.MembershipNotFound("Member not found.")
        if target.id == actor.id and new_role != Role.ADMIN:
            raise exc.SelfDemoteError("You cannot remove your own admin role.")
        if target.role == Role.ADMIN and new_role != Role.ADMIN:
            if Membership.objects.filter(workspace=workspace, role=Role.ADMIN).count() <= 1:
                raise exc.LastAdminError("A workspace must keep at least one admin.")
        target.role = new_role
        target.save(update_fields=["role", "updated_at"])
    log.info(
        "role_changed workspace_id=%s membership_id=%s new=%s by=%s",
        workspace.id, target.id, new_role, actor.id,
    )
    return target


def remove_member(
    *, workspace: Workspace, actor: Membership, target_membership_id: str
) -> None:
    """Admin removes a member. Scoped to `workspace`; guards self-removal and last-admin."""
    with transaction.atomic():
        try:
            target = Membership.objects.select_for_update().get(
                id=target_membership_id, workspace=workspace
            )
        except (Membership.DoesNotExist, ValueError, DjangoValidationError):
            raise exc.MembershipNotFound("Member not found.")
        if target.id == actor.id:
            raise exc.SelfRemoveError("You cannot remove yourself.")
        if target.role == Role.ADMIN:
            if Membership.objects.filter(workspace=workspace, role=Role.ADMIN).count() <= 1:
                raise exc.LastAdminError("A workspace must keep at least one admin.")
        target.delete()
    log.info(
        "member_removed workspace_id=%s membership_id=%s by=%s",
        workspace.id, target_membership_id, actor.id,
    )


def set_selected_workspace(*, request, workspace_id: str) -> Membership:
    """Switch the session's active workspace — authorizing membership FIRST (I6)."""
    try:
        membership = Membership.objects.select_related("workspace").get(
            user=request.user, workspace_id=workspace_id
        )
    except (Membership.DoesNotExist, ValueError, DjangoValidationError):
        raise exc.MembershipNotFound("You are not a member of that workspace.")
    request.session["workspace_id"] = str(membership.workspace_id)
    log.info(
        "workspace_switched user_id=%s workspace_id=%s",
        request.user.id, membership.workspace_id,
    )
    return membership


def list_members(*, workspace: Workspace):
    """Members of a workspace (read helper; always workspace-scoped, I6)."""
    return workspace.memberships.select_related("user").order_by("created_at")


# --- custom domains (Phase 9) ---------------------------------------------

def _norm_host(hostname: str) -> str:
    """Lowercase + strip. No punycoding / IDN handling — POC scope."""
    return (hostname or "").strip().lower().rstrip(".")


def create_domain(*, workspace: Workspace, hostname: str) -> Domain:
    """Bind a hostname to a workspace in the pending state. Unique across all
    workspaces (a hostname points to exactly one workspace). Returns the Domain;
    verification is a separate step (see `verify_domain`).
    """
    host = _norm_host(hostname)
    if not host or "." not in host:
        raise exc.ValidationError("Enter a hostname like help.example.com.")
    try:
        with transaction.atomic():
            domain = Domain.objects.create(
                workspace=workspace,
                hostname=host,
                verify_token=secrets.token_urlsafe(24),
            )
    except IntegrityError:
        raise exc.SlugCollision("That hostname is already registered.")
    log.info(
        "domain_created workspace_id=%s domain_id=%s hostname=%s",
        workspace.id, domain.id, domain.hostname,
    )
    return domain


def verify_domain(*, domain: Domain) -> Domain:
    """Mark the domain verified.

    STUB (Phase 9 shortcut, called out in the assignment brief):
    A production implementation would use `dnspython` to run TWO checks and
    require both to pass before flipping `verified_at`:

      1. `dns.resolver.resolve(domain.hostname, "CNAME")` returns a record
         whose canonical target equals `settings.BASE_HOST` — proves the
         operator's DNS points traffic at us.
      2. `dns.resolver.resolve(f"_verify.{domain.hostname}", "TXT")` returns
         a chunk equal to `domain.verify_token` — proves ownership of the DNS
         zone (an attacker can't create the TXT record on a domain they don't
         control).

    Both checks would be wrapped in a short timeout (~4s) so a slow DNS
    resolver can't hang the request. Failures would populate a `verify_error`
    column and leave `verified_at=None`.

    We stub it here to keep the POC dependency-light and demoable without a
    purchased domain — the README documents the production path in full.
    """
    domain.verified_at = timezone.now()
    domain.save(update_fields=["verified_at", "updated_at"])
    log.info(
        "domain_verified workspace_id=%s domain_id=%s hostname=%s (stub)",
        domain.workspace_id, domain.id, domain.hostname,
    )
    return domain


def delete_domain(*, domain: Domain) -> None:
    log.info(
        "domain_deleted workspace_id=%s domain_id=%s hostname=%s",
        domain.workspace_id, domain.id, domain.hostname,
    )
    domain.delete()


def list_domains(*, workspace: Workspace):
    return workspace.domains.order_by("created_at")
