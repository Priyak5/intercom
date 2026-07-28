"""Inbox service layer — the only writers of Contact/Conversation/Message (I5).

`post_message` is the single write path for every message (agent REST, widget REST, and
in Phase 4 the IMAP poller). Ordering comes from the atomic seq counter (I2); sends are
idempotent on `client_msg_id` (I4). Broadcasts are wired in P6 (`realtime.broadcast`).
"""

import logging
import uuid

from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from apps.core import exceptions as exc
from apps.inbox import realtime
from apps.inbox.models import (
    Channel,
    Conversation,
    ConversationStatus,
    DeliveryState,
    Message,
    SenderType,
)

log = logging.getLogger("inbox.services")


def _email_fields(email_meta) -> dict:
    if not email_meta:
        return {}
    return {
        "email_message_id": email_meta.get("email_message_id", ""),
        "email_in_reply_to": email_meta.get("email_in_reply_to", ""),
        "raw_mime": email_meta.get("raw_mime"),
        "delivery_state": email_meta.get("delivery_state", DeliveryState.QUEUED),
    }


def _touch_conversation_denorms(conversation: Conversation, msg: Message) -> None:
    """Update last_message_at (+ first_response_at on the first agent reply). Uses
    update_fields so it never clobbers the raw-SQL-incremented last_seq.
    """
    fields = ["last_message_at", "updated_at"]
    conversation.last_message_at = msg.created_at
    if conversation.first_response_at is None and msg.sender_type == SenderType.AGENT:
        conversation.first_response_at = msg.created_at
        fields.append("first_response_at")
    conversation.save(update_fields=fields)


def get_or_create_conversation(
    *, workspace, contact, channel=Channel.CHAT, subject: str = ""
) -> Conversation:
    """Return the contact's current non-resolved conversation on this channel, or make one."""
    conv = (
        Conversation.objects.filter(workspace=workspace, contact=contact, channel=channel)
        .exclude(status=ConversationStatus.RESOLVED)
        .order_by("-created_at")
        .first()
    )
    if conv is not None:
        return conv
    conv = Conversation.objects.create(
        workspace=workspace, contact=contact, channel=channel, subject=subject
    )
    log.info(
        "conversation_created workspace_id=%s conv_id=%s channel=%s",
        workspace.id, conv.id, channel,
    )
    return conv


def post_message(
    *,
    conversation: Conversation,
    sender_type: str,
    body_text: str,
    client_msg_id,
    sender_user=None,
    body_html: str = "",
    email_meta=None,
) -> Message:
    """Persist a message with the next per-conversation seq. Idempotent on client_msg_id."""
    # 1. Idempotency fast path (retry / double-tap) — no new seq, no new row (I4).
    existing = Message.objects.filter(
        conversation=conversation, client_msg_id=client_msg_id
    ).first()
    if existing is not None:
        return existing

    try:
        with transaction.atomic():
            # 2. Atomic seq allocation — one statement, never SELECT max (I2).
            with connection.cursor() as cur:
                # SQLite stores UUIDField as 32-char hex without dashes; match that form.
                cur.execute(
                    "UPDATE inbox_conversation SET last_seq = last_seq + 1 "
                    "WHERE id = %s RETURNING last_seq",
                    [uuid.UUID(str(conversation.id)).hex],
                )
                seq = cur.fetchone()[0]
            conversation.last_seq = seq  # keep the in-memory object consistent for envelopes
            # 3. Insert with that seq.
            msg = Message.objects.create(
                conversation=conversation,
                seq=seq,
                sender_type=sender_type,
                sender_user=sender_user,
                body_text=body_text,
                body_html=body_html,
                client_msg_id=client_msg_id,
                **_email_fields(email_meta),
            )
            # 4. Denorms (same txn).
            _touch_conversation_denorms(conversation, msg)
    except IntegrityError:
        # Concurrent duplicate on unique(conversation, client_msg_id): return the winner
        # (I4). A unique(conversation, seq) violation instead means a real seq bug — re-raise.
        dup = Message.objects.filter(
            conversation=conversation, client_msg_id=client_msg_id
        ).first()
        if dup is not None:
            return dup
        raise

    # 5. Broadcast post-commit, outside the txn (never lets fanout roll back a write).
    realtime.broadcast(
        realtime.conv_group(conversation.id), realtime.message_created_envelope(msg)
    )
    realtime.broadcast(
        realtime.ws_group(conversation.workspace_id),
        realtime.conversation_updated_envelope(conversation),
    )
    log.info("message_posted conv_id=%s seq=%s sender=%s", conversation.id, seq, sender_type)
    return msg


def assign(*, conversation: Conversation, assignee, actor) -> Conversation:
    """Assign (or unassign, assignee=None) a conversation; records a system audit message."""
    if assignee is not None:
        from apps.accounts.models import Membership

        if not Membership.objects.filter(
            user=assignee, workspace=conversation.workspace
        ).exists():
            raise exc.ValidationError("Assignee is not a member of this workspace.")
    conversation.assignee = assignee
    conversation.save(update_fields=["assignee", "updated_at"])
    label = assignee.email if assignee else "no one"
    post_message(
        conversation=conversation,
        sender_type=SenderType.SYSTEM,
        body_text=f"Assigned to {label}",
        client_msg_id=uuid.uuid4(),
        sender_user=actor,
    )
    log.info(
        "conversation_assigned conv_id=%s assignee=%s by=%s",
        conversation.id, getattr(assignee, "id", None), getattr(actor, "id", None),
    )
    return conversation


def set_status(*, conversation: Conversation, status: str, actor) -> Conversation:
    if status not in ConversationStatus.values:
        raise exc.ValidationError(f"Invalid status: {status!r}")
    conversation.status = status
    fields = ["status", "updated_at"]
    if status == ConversationStatus.RESOLVED:
        conversation.resolved_at = timezone.now()
        fields.append("resolved_at")
    conversation.save(update_fields=fields)
    post_message(
        conversation=conversation,
        sender_type=SenderType.SYSTEM,
        body_text=f"Conversation {status}",
        client_msg_id=uuid.uuid4(),
        sender_user=actor,
    )
    log.info(
        "conversation_status conv_id=%s status=%s by=%s",
        conversation.id, status, getattr(actor, "id", None),
    )
    return conversation


def mark_read(*, conversation: Conversation, reader: str, upto_seq: int) -> Conversation:
    """Advance a side's read cursor monotonically. `reader` is 'agent' or 'contact'."""
    field = {"agent": "agent_last_read_seq", "contact": "contact_last_read_seq"}.get(reader)
    if field is None:
        raise exc.ValidationError(f"Invalid reader: {reader!r}")
    current = getattr(conversation, field)
    new = max(current, int(upto_seq))
    if new != current:
        setattr(conversation, field, new)
        conversation.save(update_fields=[field, "updated_at"])
        realtime.broadcast(
            realtime.conv_group(conversation.id),
            realtime.read_updated_envelope(conversation, reader),
        )
    log.info("read_marked conv_id=%s reader=%s upto=%s", conversation.id, reader, new)
    return conversation
