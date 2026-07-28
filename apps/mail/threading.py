"""Four-path conversation resolver for inbound email.

Called from `apps.mail.poller.process_inbound` with the parsed headers of a single MIME
message. Returns the resolved (or freshly-created) `Conversation` plus the path label
used, so the poller can log which route we took.

Order (from plan.md Phase 4):
  1. `In-Reply-To` / `References` → an existing `Message.email_message_id`
  2. Plus-address token `support+c<conv>.<hmac8>@` in To/Cc/Delivered-To
  3. Same sender + same normalised subject within 7 days
  4. Otherwise a NEW Conversation

Path 4 calls `Conversation.objects.create(...)` directly — not
`inbox.services.get_or_create_conversation` — because that helper reuses any open
conversation on the channel, which would defeat path 3's subject test.
"""

import logging
import re
from datetime import timedelta

from django.utils import timezone

from apps.inbox.models import Channel, Contact, Conversation, ConversationStatus, Message
from apps.mail import addressing

log = logging.getLogger("mail.threading")

SUBJECT_MATCH_WINDOW = timedelta(days=7)
_RE_SUBJ_PREFIX = re.compile(r"^\s*(?:(?:re|fw|fwd|aw|sv|antw)\s*(?:\[\d+\])?\s*:\s*)+", re.I)
_RE_WS = re.compile(r"\s+")


def normalise_subject(s: str) -> str:
    """Strip Re:/Fwd:/etc. prefixes and collapse whitespace, lower-case. Empty string is
    a valid normalised subject (matches other empty-subject messages).
    """
    if not s:
        return ""
    s = _RE_SUBJ_PREFIX.sub("", s)
    return _RE_WS.sub(" ", s).strip().lower()


def _referenced_ids(headers) -> list[str]:
    """Return every message-id token from In-Reply-To and References, in order,
    most-recent first (In-Reply-To takes precedence over the tail of References).
    """
    out: list[str] = []
    seen = set()
    for hdr in ("In-Reply-To", "References"):
        raw = headers.get(hdr) or ""
        for mid in re.findall(r"<[^<>\s]+>", raw):
            if mid not in seen:
                seen.add(mid)
                out.append(mid)
    return out


def resolve_conversation(
    *,
    workspace,
    headers,
    sender_email: str,
    subject: str,
    when=None,
    raw_recipients: list[str] | None = None,
) -> tuple[Conversation, str]:
    """Resolve (or create) the conversation for an inbound email.

    `raw_recipients` is the flat list of addresses from To/Cc/Delivered-To; we scan it
    for a plus-token. `when` is the email's Date (or now).
    """
    when = when or timezone.now()

    # Path 1: In-Reply-To / References → an existing Message.
    for mid in _referenced_ids(headers):
        m = (
            Message.objects.filter(
                conversation__workspace=workspace, email_message_id=mid
            )
            .select_related("conversation")
            .first()
        )
        if m is not None:
            log.info("mail_resolve path=reference mid=%s conv=%s", mid, m.conversation_id)
            return m.conversation, "reference"

    # Path 2: plus-address token in any recipient header. Requires workspace hmac_secret.
    for addr in raw_recipients or []:
        conv_uuid = addressing.decode(addr, workspace.hmac_secret)
        if conv_uuid is None:
            continue
        conv = Conversation.objects.filter(id=conv_uuid, workspace=workspace).first()
        if conv is not None:
            log.info("mail_resolve path=plus_token conv=%s", conv.id)
            return conv, "plus_token"

    # Path 3: same sender + same normalised subject within 7 days.
    norm_subj = normalise_subject(subject)
    contact = (
        Contact.objects.filter(workspace=workspace, email__iexact=sender_email).first()
        if sender_email
        else None
    )
    if contact is not None:
        cutoff = when - SUBJECT_MATCH_WINDOW
        candidates = Conversation.objects.filter(
            workspace=workspace,
            contact=contact,
            channel=Channel.EMAIL,
            last_message_at__gte=cutoff,
        ).exclude(status=ConversationStatus.RESOLVED).order_by("-last_message_at")
        for c in candidates:
            if normalise_subject(c.subject) == norm_subj:
                log.info("mail_resolve path=sender_subject conv=%s", c.id)
                return c, "sender_subject"

    # Path 4: new conversation. Create Contact if this is a first-time sender.
    if contact is None and sender_email:
        contact = Contact.objects.create(workspace=workspace, email=sender_email)
    elif contact is None:
        # No sender email at all — rare, malformed message. Anonymous contact.
        contact = Contact.objects.create(workspace=workspace)
    conv = Conversation.objects.create(
        workspace=workspace,
        contact=contact,
        channel=Channel.EMAIL,
        subject=(subject or "")[:512],
    )
    log.info("mail_resolve path=new conv=%s contact=%s", conv.id, contact.id)
    return conv, "new"
