"""Inbound MIME → Message. Called by the poller for every fetched RFC822 blob.

`process_inbound` is idempotent on Message-ID: the same raw email fed twice yields one
Message, because `client_msg_id` is derived via uuid5(NAMESPACE_URL, message_id) and
`unique(conversation, client_msg_id)` in the DB makes `services.post_message` return
the existing row on retry.
"""

import html
import logging
import re
import uuid
from email import message_from_bytes, policy
from email.utils import getaddresses, parsedate_to_datetime

from django.conf import settings
from django.utils import timezone
from email_reply_parser import EmailReplyParser

from apps.inbox import services
from apps.inbox.models import DeliveryState, SenderType
from apps.mail import threading as mail_threading

log = logging.getLogger("mail.inbound")

_NS_EMAIL = uuid.NAMESPACE_URL  # stable namespace for uuid5


def _sender_email(msg) -> str:
    """First address in From:, lower-cased. Empty string on malformed From."""
    addrs = getaddresses(msg.get_all("From", []) or [])
    for _name, email_addr in addrs:
        if email_addr:
            return email_addr.strip().lower()
    return ""


def _all_recipients(msg) -> list[str]:
    """Flat list of every address in To/Cc/Delivered-To (used to find a plus-token)."""
    values = []
    for hdr in ("To", "Cc", "Delivered-To", "X-Original-To"):
        values.extend(msg.get_all(hdr, []) or [])
    return [addr.lower() for _name, addr in getaddresses(values) if addr]


def _pick_body(msg) -> tuple[str, list[dict]]:
    """Return (text_body, attachments). Prefer text/plain; fall back to text/html stripped
    to text. Attachments are metadata-only (name, size, content_type) — the raw payload
    is discarded (documented Known Limitation).
    """
    text_body = ""
    html_body = ""
    attachments: list[dict] = []

    for part in msg.walk():
        if part.is_multipart():
            continue
        disposition = (part.get_content_disposition() or "").lower()
        ctype = part.get_content_type()
        if disposition == "attachment" or part.get_filename():
            payload = part.get_payload(decode=True) or b""
            attachments.append(
                {
                    "filename": part.get_filename() or "attachment",
                    "content_type": ctype,
                    "size": len(payload),
                }
            )
            continue
        if ctype == "text/plain" and not text_body:
            text_body = _decode(part)
        elif ctype == "text/html" and not html_body:
            html_body = _decode(part)

    if not text_body and html_body:
        text_body = _strip_tags(html_body)
    return text_body, attachments


def _decode(part) -> str:
    payload = part.get_payload(decode=True) or b""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


_RE_TAG = re.compile(r"<[^>]+>")
_RE_MULTI_WS = re.compile(r"\n\s*\n\s*\n+")


def _strip_tags(s: str) -> str:
    """Cheap HTML → text. Good enough to save a body when the sender omitted text/plain;
    we do NOT try to render layout — the raw MIME is on the Message for debugging."""
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", s, flags=re.I | re.S)
    s = _RE_TAG.sub("", s)
    s = html.unescape(s)
    return _RE_MULTI_WS.sub("\n\n", s).strip()


def _client_msg_id_for(message_id: str) -> uuid.UUID:
    """Stable per Message-ID so IMAP re-fetches dedupe via unique(conversation, client_msg_id)."""
    if not message_id:
        # No Message-ID at all — fall back to a random uuid; dedup is best-effort here.
        return uuid.uuid4()
    return uuid.uuid5(_NS_EMAIL, message_id.strip())


def _parse_date(msg) -> "timezone.datetime":
    hdr = msg.get("Date")
    if not hdr:
        return timezone.now()
    try:
        dt = parsedate_to_datetime(hdr)
        if dt.tzinfo is None:
            dt = timezone.make_aware(dt, timezone.utc)
        return dt
    except (TypeError, ValueError):
        return timezone.now()


def process_inbound(raw_mime: bytes, workspace) -> None:
    """Parse, thread, post. Any exception is logged by the caller — DO NOT swallow here
    so the poller's outer try/except records `uid` alongside the failure.
    """
    msg = message_from_bytes(raw_mime, policy=policy.default)

    message_id = (msg.get("Message-ID") or "").strip()
    sender = _sender_email(msg)
    subject = msg.get("Subject") or ""
    recipients = _all_recipients(msg)
    when = _parse_date(msg)

    text_body, attachments = _pick_body(msg)
    # Strip quoted history (previous replies, "On <date>, <person> wrote:" blocks).
    if text_body:
        try:
            text_body = EmailReplyParser.parse_reply(text_body)
        except Exception:  # noqa: BLE001 — parser can throw on odd inputs; fall back to raw.
            log.debug("reply_parser_fallback msg_id=%s", message_id)

    if attachments:
        pieces = [text_body.rstrip() if text_body else ""]
        for att in attachments:
            kb = max(1, (att["size"] + 512) // 1024)
            pieces.append(f"[Attachment omitted: {att['filename']} ({kb} KB, {att['content_type']})]")
        text_body = "\n\n".join(p for p in pieces if p)

    if not text_body:
        text_body = "(empty message)"

    conv, path = mail_threading.resolve_conversation(
        workspace=workspace,
        headers=msg,
        sender_email=sender,
        subject=subject,
        when=when,
        raw_recipients=recipients,
    )

    # `services.post_message` is idempotent on (conversation, client_msg_id) — a stable
    # uuid5 of the Message-ID guards against IMAP retries producing duplicate rows.
    services.post_message(
        conversation=conv,
        sender_type=SenderType.CONTACT,
        body_text=text_body[:65535],  # sanity cap; conversations don't render megabytes
        client_msg_id=_client_msg_id_for(message_id),
        email_meta={
            "email_message_id": message_id,
            "email_in_reply_to": (msg.get("In-Reply-To") or "").strip(),
            "raw_mime": raw_mime.decode("utf-8", errors="replace"),
            "delivery_state": DeliveryState.SENT,
        },
    )

    log.info(
        "mail_in path=%s conv=%s msg_id=%s from=%s subject=%r",
        path, conv.id, message_id, sender, (subject or "")[:80],
    )
