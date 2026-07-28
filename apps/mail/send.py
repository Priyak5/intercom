"""Outbound email for agent replies to email conversations.

Called from `apps.inbox.services.post_message` after commit, as a side-effect (I3: the
DB is truth, delivery is best-effort). Explicit Message-ID / In-Reply-To / References
headers make the reply thread correctly in the customer's mail client. Reply-To carries
a plus-address token so the customer's next reply comes back with routing info even if
they strip References (path 2 fallback).

Provider selection: RESEND_API_KEY (HTTPS API, works on Railway/Fly/Render) is
preferred. When empty, falls back to smtplib against SMTP_* — kept for local dev and
for hosts that permit SMTP. Either path marks `delivery_state=SENT` on success and
`FAILED` on any error, and logs one structured line — no inline retry (documented in
README Known Limitations).
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


def _mark_failed(message: Message) -> None:
    message.delivery_state = DeliveryState.FAILED
    message.save(update_fields=["delivery_state", "updated_at"])


def _mark_sent(message: Message) -> None:
    message.delivery_state = DeliveryState.SENT
    message.save(update_fields=["delivery_state", "updated_at"])


# --- Resend HTTPS path ------------------------------------------------------


def _send_via_resend(*, message: Message, from_addr: str, to_addr: str, reply_to: str,
                     subject: str, in_reply_to: str, references: list[str]) -> bool:
    """Deliver via Resend's HTTPS API. Returns True on success. Threading headers ride
    on the `headers` field of the payload — Resend passes them through verbatim.
    """
    import resend

    resend.api_key = settings.RESEND_API_KEY
    headers: dict[str, str] = {}
    if message.email_message_id:
        headers["Message-ID"] = message.email_message_id
    if in_reply_to:
        headers["In-Reply-To"] = in_reply_to
    if references:
        headers["References"] = " ".join(references)
    payload = {
        "from": from_addr,
        "to": [to_addr],
        "reply_to": reply_to,
        "subject": subject,
        "text": message.body_text or "",
        "headers": headers,
    }
    try:
        result = resend.Emails.send(payload)
    except Exception as exc:  # noqa: BLE001 — Resend SDK exposes many exception classes.
        log.warning(
            "resend_send_failed conv=%s msg=%s error=%r",
            message.conversation_id, message.id, exc,
        )
        return False
    log.info(
        "resend_send_ok conv=%s msg=%s mid=%s provider_id=%s to=%s",
        message.conversation_id, message.id, message.email_message_id,
        (result or {}).get("id") if isinstance(result, dict) else getattr(result, "id", None),
        to_addr,
    )
    return True


# --- smtplib fallback -------------------------------------------------------


def _send_via_smtp(*, message: Message, from_addr: str, to_addr: str, reply_to: str,
                   subject: str, in_reply_to: str, references: list[str]) -> bool:
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Reply-To"] = reply_to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = message.email_message_id or make_msgid()
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = " ".join(references)
    msg.set_content(message.body_text or "")
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
        log.warning(
            "smtp_send_failed conv=%s msg=%s mid=%s error=%r",
            message.conversation_id, message.id, message.email_message_id, exc,
        )
        return False
    log.info(
        "smtp_send_ok conv=%s msg=%s mid=%s to=%s",
        message.conversation_id, message.id, message.email_message_id, to_addr,
    )
    return True


# --- public entry point -----------------------------------------------------


def send_reply(*, message: Message) -> None:
    """Deliver an agent's Message as a threaded email. Idempotent-ish: we stamp
    `message.email_message_id` BEFORE dispatch so a retry would collide on the
    unique-per-conv index instead of double-sending.
    """
    conversation = message.conversation
    contact = conversation.contact
    workspace = conversation.workspace

    if not contact.email:
        log.warning("mail_send_skip_no_contact_email conv=%s msg=%s", conversation.id, message.id)
        _mark_failed(message)
        return

    use_resend = bool(settings.RESEND_API_KEY)
    use_smtp = bool(settings.SMTP_HOST and settings.SMTP_USER)
    if not (use_resend or use_smtp):
        log.warning("mail_send_skip_not_configured conv=%s msg=%s", conversation.id, message.id)
        _mark_failed(message)
        return

    # 1. Set our Message-Id on the persisted row *before* dispatch — a retry would then
    # collide on unique(conversation, email_message_id) instead of double-sending.
    if not message.email_message_id:
        message.email_message_id = _build_message_id(conversation=conversation, message=message)
        message.save(update_fields=["email_message_id", "updated_at"])
    in_reply_to, references = _build_references(conversation=conversation, message=message)

    # 2. Plus-address Reply-To so the customer's reply threads even if they strip
    # References. Uses the workspace's per-tenant hmac_secret (Phase 1).
    local, _, from_domain = (settings.MAIL_FROM or "support@localhost").partition("@")
    plus_reply_to = addressing.encode(
        local=local or "support",
        domain=settings.MAIL_DOMAIN or from_domain or "localhost",
        workspace_hmac_secret=workspace.hmac_secret,
        conversation_id=conversation.id,
    )

    subject = conversation.subject or "(no subject)"
    if not subject.lower().startswith(("re:", "fwd:", "fw:")):
        subject = f"Re: {subject}"

    common = {
        "message": message,
        "from_addr": settings.MAIL_FROM,
        "to_addr": contact.email,
        "reply_to": plus_reply_to,
        "subject": subject,
        "in_reply_to": in_reply_to,
        "references": references,
    }

    ok = _send_via_resend(**common) if use_resend else _send_via_smtp(**common)
    if ok:
        _mark_sent(message)
    else:
        _mark_failed(message)
