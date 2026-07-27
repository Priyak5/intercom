"""Minimal custom User — reserved in Phase 0 so the first migration creates
`accounts_user`, not `auth_user` (switching AUTH_USER_MODEL after the initial migrate
is unsupported). Workspace/Membership/Invite/Domain and all auth flows are Phase 1.
"""

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone

from apps.core.models import BaseModel


class Role(models.TextChoices):
    ADMIN = "admin", "Admin"
    AGENT = "agent", "Agent"


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, email, password, **extra):
        if not email:
            raise ValueError("Users must have an email address")
        user = self.model(email=self.normalize_email(email), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra):
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra)

    def create_superuser(self, email, password=None, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        if extra.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True")
        if extra.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True")
        return self._create_user(email, password, **extra)


class User(BaseModel, AbstractBaseUser, PermissionsMixin):
    email = models.EmailField(unique=True)
    name = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: list[str] = []

    def __str__(self) -> str:
        return self.email


class Workspace(BaseModel):
    """A tenant. Every tenant-owned row carries a workspace FK; all tenancy derives
    from this (I6). `public_key` is safe to embed in customer pages; `hmac_secret`
    never leaves the server. Both, plus `slug`, are generated in the service layer.
    """

    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=64, unique=True)
    public_key = models.CharField(max_length=48, unique=True, editable=False)
    hmac_secret = models.CharField(max_length=88, editable=False)

    def __str__(self) -> str:
        return self.slug


class Membership(BaseModel):
    """Links a User to a Workspace with a role. The reader/writer set for a workspace."""

    user = models.ForeignKey(
        "accounts.User", on_delete=models.CASCADE, related_name="memberships"
    )
    workspace = models.ForeignKey(
        "accounts.Workspace", on_delete=models.CASCADE, related_name="memberships"
    )
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.AGENT)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "workspace"], name="uniq_membership_user_workspace"
            )
        ]
        indexes = [models.Index(fields=["workspace", "role"])]

    def __str__(self) -> str:
        return f"{self.user_id}@{self.workspace_id}:{self.role}"


class Invite(BaseModel):
    """A pending workspace invitation. The stored row (not a stateless signed blob) is
    authoritative: `accepted_at` makes the token single-use, `expires_at` sets its TTL.
    """

    workspace = models.ForeignKey(
        "accounts.Workspace", on_delete=models.CASCADE, related_name="invites"
    )
    email = models.EmailField()
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.AGENT)
    token = models.CharField(max_length=64, unique=True, editable=False)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    invited_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        related_name="sent_invites",
    )

    class Meta:
        constraints = [
            # At most one pending (unaccepted) invite per email per workspace.
            models.UniqueConstraint(
                fields=["workspace", "email"],
                condition=models.Q(accepted_at__isnull=True),
                name="uniq_pending_invite_per_email",
            )
        ]
        indexes = [models.Index(fields=["workspace"])]

    @property
    def is_valid(self) -> bool:
        return self.accepted_at is None and self.expires_at > timezone.now()

    def __str__(self) -> str:
        return f"invite<{self.email}@{self.workspace_id}>"
