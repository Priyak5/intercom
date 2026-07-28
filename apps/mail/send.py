"""Outbound SMTP for agent replies to email conversations.

Called from `apps.inbox.services.post_message` after commit, as a side-effect (I3: the
DB is truth, SMTP is best-effort). Explicit Message-ID / In-Reply-To / References
headers make the reply thread correctly in the customer's mail client. Reply-To carries
a plus-address token so the customer's next reply comes back with routing info even if
they strip References (path 2 fallback).

Hard-timeout the SMTP socket so a slow provider can't hang the agent's dashboard
request (I8-adjacent). On failure: mark delivery_state=FAILED and log — no inline
retry (documented in README Known Limitations).
"""

import logging
import smtplib
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from django.conf import settings

from apps.inbox.models import DeliveryState, Message, SenderType
from apps.mail import addressing

log = logging.getLogger("mail.send")

# Cap the References chain length so headers don't grow unbounded on very long threads.
_MAX_REFERENCES = 20


def _build_message_id(*, conversation, message: Message) -> str:
    domain = settings.MAIL_DOMAIN or "localhost"
    # Deterministic, unique per Message (uuid pk). RFC-legal angle-bracketed form.
    return f"<c{conversation.id.hex}.m{message.id.hex}@{domain}>"


def _build_references(*, conversation, message: Message) -> tuple[str, list[str]]:
    """Return (in_reply_to_id, references_chain) for the reply headers.

    in_reply_to: the *last customer message*'s email_message_id (falls back to any
                 prior message with a Message-Id if the conversation started here).
    references: prior email_message_ids in-order, capped to _MAX_REFERENCES.
    """
    prior_ids = list(
        conversation.messages.exclude(id=message.id)
        .exclude(email_message_id="")
        .order_by("seq")
        .values_list("email_message_id", flat=True)
    )
    prior_ids = prior_ids[-_MAX_REFERENCES:]
    in_reply_to = ""
    # Pick the newest inbound (CONTACT) message for In-Reply-To.
    newest_inbound = (
        conversation.messages.filter(sender_type=SenderType.CONTACT)
        .exclude(email_message_id="")
        .exclude(id=message.id)
        .order_by("-seq")
        .values_list("email_message_id", flat=True)
        .first()
    )
    if newest_inbound:
        in_reply_to = newest_inbound
    elif prior_ids:
        in_reply_to = prior_ids[-1]
    return in_reply_to, prior_ids


def send_reply(*, message: Message) -> None:
    """Deliver an agent's Message over SMTP as a threaded email. Idempotent-ish: we
    stamp `message.email_message_id` BEFORE dispatch so a retry would collide on the
    unique-per-conv index instead of double-sending.
    """
    conversation = message.conversation
    contact = conversation.contact
    workspace = conversation.workspace
    if not contact.email:
        log.warning("smtp_skip_no_contact_email conv=%s msg=%s", conversation.id, message.id)
        message.delivery_state = DeliveryState.FAILED
        message.save(update_fields=["delivery_state", "updated_at"])
        return

    if not (settings.SMTP_HOST and settings.SMTP_USER):
        log.warning("smtp_skip_not_configured conv=%s msg=%s", conversation.id, message.id)
        message.delivery_state = DeliveryState.FAILED
        message.save(update_fields=["delivery_state", "updated_at"])
        return

    # 1. Build headers + set our own Message-Id on the persisted row *before* SMTP.
    our_mid = _build_message_id(conversation=conversation, message=message)
    if not message.email_message_id:
        message.email_message_id = our_mid
        message.save(update_fields=["email_message_id", "updated_at"])
    in_reply_to, references = _build_references(conversation=conversation, message=message)

    # 2. Plus-address Reply-To so the customer's reply threads even if they strip
    # References. Uses the workspace's per-tenant hmac_secret (Phase 1).
    local, _, domain = (settings.MAIL_FROM or "support@localhost").partition("@")
    plus_reply_to = addressing.encode(
        local=local or "support",
        domain=settings.MAIL_DOMAIN or domain or "localhost",
        workspace_hmac_secret=workspace.hmac_secret,
        conversation_id=conversation.id,
    )

    subject = conversation.subject or "(no subject)"
    if not subject.lower().startswith(("re:", "fwd:", "fw:")):
        subject = f"Re: {subject}"

    msg = EmailMessage()
    msg["From"] = settings.MAIL_FROM
    msg["To"] = contact.email
    msg["Reply-To"] = plus_reply_to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = message.email_message_id or make_msgid()
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = " ".join(references)
    msg.set_content(message.body_text or "")

    # 3. SMTP with a hard timeout — must not hang the agent's request.
    try:
        with smtplib.SMTP(
            settings.SMTP_HOST, settings.SMTP_PORT, timeout=settings.SMTP_TIMEOUT
        ) as s:
            s.ehlo()
            s.starttls()
            s.ehlo()
            s.login(settings.SMTP_USER, settings.SMTP_PASS)
            s.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        message.delivery_state = DeliveryState.FAILED
        message.save(update_fields=["delivery_state", "updated_at"])
        log.warning(
            "smtp_send_failed conv=%s msg=%s mid=%s error=%r",
            conversation.id, message.id, message.email_message_id, exc,
        )
        return

    message.delivery_state = DeliveryState.SENT
    message.save(update_fields=["delivery_state", "updated_at"])
    log.info(
        "smtp_send_ok conv=%s msg=%s mid=%s to=%s",
        conversation.id, message.id, message.email_message_id, contact.email,
    )
