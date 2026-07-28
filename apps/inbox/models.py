"""Inbox domain: Contact, Conversation, Message.

Ordering is by the server-assigned `Conversation.last_seq` counter (I2), never by
timestamps. `Message` carries the two uniqueness constraints that make ordering and
idempotency loud on violation (I2/I4). Email/threading fields are declared now but only
used in Phase 4.
"""

import uuid

from django.db import models

from apps.core.models import BaseModel


class Channel(models.TextChoices):
    CHAT = "chat", "Chat"
    EMAIL = "email", "Email"


class ConversationStatus(models.TextChoices):
    OPEN = "open", "Open"
    SNOOZED = "snoozed", "Snoozed"
    RESOLVED = "resolved", "Resolved"


class SenderType(models.TextChoices):
    CONTACT = "contact", "Contact"
    AGENT = "agent", "Agent"
    SYSTEM = "system", "System"


class DeliveryState(models.TextChoices):
    QUEUED = "queued", "Queued"
    SENT = "sent", "Sent"
    FAILED = "failed", "Failed"


class Contact(BaseModel):
    """A person on the customer side of a conversation. Anonymous widget visitors have
    an empty email; the partial unique index lets many of those coexist per workspace.
    """

    workspace = models.ForeignKey(
        "accounts.Workspace", on_delete=models.CASCADE, related_name="contacts"
    )
    email = models.EmailField(blank=True)
    name = models.CharField(max_length=255, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    current_page = models.CharField(max_length=1024, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "email"],
                condition=models.Q(email__gt=""),
                name="uniq_contact_workspace_email",
            )
        ]
        indexes = [models.Index(fields=["workspace", "email"])]

    def __str__(self) -> str:
        return self.email or f"anon:{self.id}"


class Conversation(BaseModel):
    workspace = models.ForeignKey(
        "accounts.Workspace", on_delete=models.CASCADE, related_name="conversations"
    )
    contact = models.ForeignKey(
        "inbox.Contact", on_delete=models.CASCADE, related_name="conversations"
    )
    channel = models.CharField(max_length=8, choices=Channel.choices, default=Channel.CHAT)
    status = models.CharField(
        max_length=8, choices=ConversationStatus.choices, default=ConversationStatus.OPEN
    )
    assignee = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_conversations",
    )
    subject = models.CharField(max_length=512, blank=True)

    # Ordering counter — allocated only via the atomic UPDATE ... RETURNING (I2).
    last_seq = models.PositiveIntegerField(default=0)
    agent_last_read_seq = models.PositiveIntegerField(default=0)
    contact_last_read_seq = models.PositiveIntegerField(default=0)

    snoozed_until = models.DateTimeField(null=True, blank=True)
    last_message_at = models.DateTimeField(null=True, blank=True)
    first_response_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    # AI summary (Phase 7); declared here so the schema is stable.
    summary = models.TextField(blank=True)
    summary_upto_seq = models.PositiveIntegerField(default=0)

    class Meta:
        indexes = [
            models.Index(fields=["workspace", "status", "-last_message_at"]),
            models.Index(fields=["workspace", "assignee", "-last_message_at"]),
            # Phase 5: channel filter on the unified inbox list.
            models.Index(fields=["workspace", "channel", "-last_message_at"]),
            # Phase 5: snoozer scans (status=snoozed AND snoozed_until <= now) every 60s.
            models.Index(fields=["status", "snoozed_until"], name="idx_conv_snooze_expiry"),
        ]

    def __str__(self) -> str:
        return f"conv:{self.id}"


class Message(BaseModel):
    conversation = models.ForeignKey(
        "inbox.Conversation", on_delete=models.CASCADE, related_name="messages"
    )
    seq = models.PositiveIntegerField()
    sender_type = models.CharField(max_length=8, choices=SenderType.choices)
    sender_user = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="messages",
    )
    body_text = models.TextField()
    body_html = models.TextField(blank=True)
    client_msg_id = models.UUIDField(default=uuid.uuid4)

    # Email threading (Phase 4).
    email_message_id = models.CharField(max_length=998, blank=True)
    email_in_reply_to = models.CharField(max_length=998, blank=True)
    delivery_state = models.CharField(
        max_length=8, choices=DeliveryState.choices, default=DeliveryState.QUEUED
    )
    raw_mime = models.TextField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["conversation", "seq"], name="uniq_message_conv_seq"),
            models.UniqueConstraint(
                fields=["conversation", "client_msg_id"], name="uniq_message_conv_client_id"
            ),
        ]
        indexes = [
            models.Index(fields=["conversation", "seq"]),
            models.Index(fields=["email_message_id"]),
        ]

    def __str__(self) -> str:
        return f"msg:{self.conversation_id}#{self.seq}"
