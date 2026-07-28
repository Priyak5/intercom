"""Phase 4 email threading. Feeds raw MIME through `apps.mail.inbound.process_inbound`
and asserts the resolved conversation for each of the four resolution paths, plus
reply-to-reply, idempotent replay, and bad-plus-token fall-through.

All paths must be workspace-scoped: an inbound processed against Workspace A must not
resolve to a Message-Id that lives in Workspace B (tenancy invariant I6, covered as a
dedicated case in test_tenancy.py).
"""

import uuid
from email.utils import formatdate, make_msgid

import pytest

from apps.inbox.models import Channel, Conversation, ConversationStatus, Message, SenderType
from apps.mail import addressing, inbound, threading as mail_threading

pytestmark = pytest.mark.django_db


def _mime(
    *,
    from_addr: str,
    to_addr: str = "support@example.com",
    subject: str = "Help please",
    body: str = "hello, this is the customer",
    message_id: str | None = None,
    in_reply_to: str = "",
    references: str = "",
    date_str: str | None = None,
) -> bytes:
    mid = message_id or make_msgid(domain="mail.example.com")
    hdrs = [
        f"From: {from_addr}",
        f"To: {to_addr}",
        f"Subject: {subject}",
        f"Date: {date_str or formatdate(localtime=True)}",
        f"Message-ID: {mid}",
        "MIME-Version: 1.0",
        'Content-Type: text/plain; charset="utf-8"',
    ]
    if in_reply_to:
        hdrs.insert(-2, f"In-Reply-To: {in_reply_to}")
    if references:
        hdrs.insert(-2, f"References: {references}")
    return ("\r\n".join(hdrs) + "\r\n\r\n" + body + "\r\n").encode("utf-8")


# --- Path 1: In-Reply-To → existing email_message_id ------------------------


def test_path1_in_reply_to_matches_existing_message(admin_a):
    from apps.inbox.models import Contact

    ws = admin_a.workspace
    contact = Contact.objects.create(workspace=ws, email="cust@example.com")
    conv = Conversation.objects.create(
        workspace=ws, contact=contact, channel=Channel.EMAIL, subject="Original topic",
        last_seq=1,  # keeps post_message's next allocation at 2
    )
    original_mid = "<orig-abc@mail.example.com>"
    Message.objects.create(
        conversation=conv, seq=1, sender_type=SenderType.CONTACT,
        body_text="first", client_msg_id=uuid.uuid4(), email_message_id=original_mid,
    )

    raw = _mime(
        from_addr="cust@example.com",
        subject="Re: Original topic",
        in_reply_to=original_mid,
        message_id="<reply-1@mail.example.com>",
        body="thanks for the reply",
    )
    inbound.process_inbound(raw, workspace=ws)

    # The inbound reply lands on the SAME conversation.
    assert conv.messages.count() == 2
    assert Conversation.objects.filter(workspace=ws, channel=Channel.EMAIL).count() == 1


# --- Path 2: plus-token in To/Cc/Delivered-To -------------------------------


def test_path2_plus_address_token_resolves_conversation(admin_a):
    from apps.inbox.models import Contact

    ws = admin_a.workspace
    contact = Contact.objects.create(workspace=ws, email="cust@example.com")
    conv = Conversation.objects.create(
        workspace=ws, contact=contact, channel=Channel.EMAIL, subject="Old subject"
    )
    plus_addr = addressing.encode(
        local="support", domain="mail.example.com",
        workspace_hmac_secret=ws.hmac_secret, conversation_id=conv.id,
    )

    raw = _mime(
        from_addr="cust@example.com",
        to_addr=plus_addr,
        subject="Totally unrelated subject",  # No path-3 match
        # No In-Reply-To (path-1 skipped)
        message_id="<p2-msg@mail.example.com>",
    )
    inbound.process_inbound(raw, workspace=ws)

    assert conv.messages.count() == 1
    assert Conversation.objects.filter(workspace=ws, channel=Channel.EMAIL).count() == 1


# --- Path 3: same sender + normalised subject within 7 days -----------------


def test_path3_sender_and_subject_reuse_recent_conversation(admin_a):
    from apps.inbox.models import Contact

    ws = admin_a.workspace
    contact = Contact.objects.create(workspace=ws, email="cust@example.com")
    conv = Conversation.objects.create(
        workspace=ws, contact=contact, channel=Channel.EMAIL, subject="Billing question",
    )
    from django.utils import timezone

    conv.last_message_at = timezone.now()
    conv.save(update_fields=["last_message_at"])

    raw = _mime(
        from_addr="cust@example.com",
        subject="Re: Billing question",  # Re: gets normalised out
        message_id="<p3@mail.example.com>",
    )
    inbound.process_inbound(raw, workspace=ws)

    assert conv.messages.count() == 1
    assert Conversation.objects.filter(workspace=ws, channel=Channel.EMAIL).count() == 1


# --- Path 4: no match → new conversation ------------------------------------


def test_path4_new_sender_creates_new_conversation(admin_a):
    ws = admin_a.workspace
    raw = _mime(
        from_addr="new-person@example.com",
        subject="First contact",
        message_id="<new@mail.example.com>",
    )
    inbound.process_inbound(raw, workspace=ws)

    convs = Conversation.objects.filter(workspace=ws, channel=Channel.EMAIL)
    assert convs.count() == 1
    assert convs.first().subject == "First contact"
    assert convs.first().contact.email == "new-person@example.com"


def test_path4_existing_sender_different_subject_does_not_reuse(admin_a):
    """Path 3 requires subject match. A new subject from the same customer creates a
    NEW conversation, not piggybacking on the existing open one.
    """
    from apps.inbox.models import Contact

    ws = admin_a.workspace
    contact = Contact.objects.create(workspace=ws, email="cust@example.com")
    from django.utils import timezone

    Conversation.objects.create(
        workspace=ws, contact=contact, channel=Channel.EMAIL,
        subject="Old topic", last_message_at=timezone.now(),
    )
    raw = _mime(
        from_addr="cust@example.com",
        subject="Completely different topic",
        message_id="<different@mail.example.com>",
    )
    inbound.process_inbound(raw, workspace=ws)

    assert Conversation.objects.filter(workspace=ws, channel=Channel.EMAIL).count() == 2


# --- Reply-to-a-reply stays on one conversation -----------------------------


def test_reply_to_reply_stays_in_one_conversation(admin_a):
    ws = admin_a.workspace

    # 1. Initial customer email (path 4).
    mid1 = "<m1@mail.example.com>"
    inbound.process_inbound(
        _mime(from_addr="cust@example.com", subject="Nested thread", message_id=mid1),
        workspace=ws,
    )
    conv = Conversation.objects.get(workspace=ws, channel=Channel.EMAIL)

    # 2. Second reply — In-Reply-To points at the first (path 1).
    mid2 = "<m2@mail.example.com>"
    inbound.process_inbound(
        _mime(
            from_addr="cust@example.com", subject="Re: Nested thread",
            message_id=mid2, in_reply_to=mid1, references=mid1,
        ),
        workspace=ws,
    )

    # 3. Third reply — In-Reply-To points at the SECOND (still path 1).
    mid3 = "<m3@mail.example.com>"
    inbound.process_inbound(
        _mime(
            from_addr="cust@example.com", subject="Re: Re: Nested thread",
            message_id=mid3, in_reply_to=mid2, references=f"{mid1} {mid2}",
        ),
        workspace=ws,
    )

    assert Conversation.objects.filter(workspace=ws, channel=Channel.EMAIL).count() == 1
    assert conv.messages.count() == 3


# --- Idempotent replay (same raw MIME twice) --------------------------------


def test_idempotent_replay_yields_one_message(admin_a):
    ws = admin_a.workspace
    raw = _mime(
        from_addr="cust@example.com",
        subject="Please help",
        message_id="<dedupe-me@mail.example.com>",
    )
    inbound.process_inbound(raw, workspace=ws)
    inbound.process_inbound(raw, workspace=ws)

    convs = Conversation.objects.filter(workspace=ws, channel=Channel.EMAIL)
    assert convs.count() == 1
    assert convs.first().messages.count() == 1


# --- Bad plus-token → falls through cleanly ---------------------------------


def test_bad_plus_token_falls_through_to_path4(admin_a):
    ws = admin_a.workspace
    fake_conv_uuid = uuid.uuid4()
    bad_addr = f"support+c{fake_conv_uuid.hex}.deadbeef@mail.example.com"

    raw = _mime(
        from_addr="cust@example.com",
        to_addr=bad_addr,
        subject="Trying to inject",
        message_id="<forged@mail.example.com>",
    )
    inbound.process_inbound(raw, workspace=ws)

    # Fresh conversation was created; no attempt made to reuse the forged conv id.
    convs = Conversation.objects.filter(workspace=ws, channel=Channel.EMAIL)
    assert convs.count() == 1
    assert convs.first().id != fake_conv_uuid


# --- Subject normalisation --------------------------------------------------


def test_subject_normalisation_strips_prefixes():
    assert mail_threading.normalise_subject("Re: Fwd: Foo") == "foo"
    assert mail_threading.normalise_subject("RE:   Foo   Bar  ") == "foo bar"
    assert mail_threading.normalise_subject("") == ""
    assert mail_threading.normalise_subject("No prefix here") == "no prefix here"


# --- Addressing round-trip --------------------------------------------------


def test_addressing_round_trip(admin_a):
    ws = admin_a.workspace
    conv_id = uuid.uuid4()
    addr = addressing.encode(
        local="support", domain="mail.example.com",
        workspace_hmac_secret=ws.hmac_secret, conversation_id=conv_id,
    )
    decoded = addressing.decode(addr, ws.hmac_secret)
    assert decoded == conv_id

    # Wrong secret → None.
    assert addressing.decode(addr, "not-the-secret") is None
    # Malformed → None.
    assert addressing.decode("just-support@mail.example.com", ws.hmac_secret) is None
    assert addressing.decode("", ws.hmac_secret) is None
