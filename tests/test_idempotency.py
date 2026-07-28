"""I4: sends are idempotent on client_msg_id. The same id twice yields one message."""

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.db import close_old_connections

from apps.accounts import services as accounts
from apps.inbox import services as inbox
from apps.inbox.models import Contact, Message

PASSWORD = "Zq7!vx92Lm4p"


def _fresh_conversation(slug_email="owner@example.com"):
    membership = accounts.sign_up(
        email=slug_email, password=PASSWORD, name="Owner", workspace_name="WS"
    )
    contact = Contact.objects.create(workspace=membership.workspace, email="v@example.com")
    return inbox.get_or_create_conversation(workspace=membership.workspace, contact=contact)


@pytest.mark.django_db
def test_same_client_msg_id_returns_one_message():
    conv = _fresh_conversation()
    cid = uuid.uuid4()

    m1 = inbox.post_message(
        conversation=conv, sender_type="contact", body_text="hi", client_msg_id=cid
    )
    m2 = inbox.post_message(
        conversation=conv, sender_type="contact", body_text="hi again", client_msg_id=cid
    )

    assert m1.id == m2.id
    assert m1.seq == m2.seq
    assert Message.objects.filter(conversation=conv).count() == 1
    conv.refresh_from_db()
    assert conv.last_seq == 1  # the duplicate must not burn a second seq (fast path)


@pytest.mark.django_db(transaction=True)
def test_concurrent_duplicate_yields_one_row():
    conv = _fresh_conversation()
    cid = uuid.uuid4()
    n = 8
    barrier = threading.Barrier(n)

    def worker(_):
        barrier.wait()
        close_old_connections()
        try:
            return inbox.post_message(
                conversation=conv, sender_type="contact", body_text="dup", client_msg_id=cid
            ).id
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=n) as pool:
        ids = list(pool.map(worker, range(n)))

    assert len(set(ids)) == 1, "all concurrent duplicates resolve to one message"
    assert Message.objects.filter(conversation=conv).count() == 1
