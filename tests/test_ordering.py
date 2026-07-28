"""I2: seq is server-assigned and dense. Concurrent DISTINCT posts must produce a
gap-free 1..N range — proving the atomic UPDATE...RETURNING serializes correctly and
nothing uses SELECT max or timestamps for ordering.

Uses transactional_db (real commits) + real threads so SQLite's single-writer lock is
actually exercised. The file-based TEST db (settings) lets the threads share one database.
"""

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.db import close_old_connections

from apps.accounts import services as accounts
from apps.inbox import services as inbox
from apps.inbox.models import Contact, Message

PASSWORD = "Zq7!vx92Lm4p"


def _fresh_conversation():
    membership = accounts.sign_up(
        email="owner@example.com", password=PASSWORD, name="Owner", workspace_name="WS"
    )
    contact = Contact.objects.create(workspace=membership.workspace, email="v@example.com")
    return inbox.get_or_create_conversation(workspace=membership.workspace, contact=contact)


@pytest.mark.django_db(transaction=True)
def test_concurrent_posts_produce_dense_gapfree_seq():
    conv = _fresh_conversation()
    n = 25
    barrier = threading.Barrier(n)

    def worker(i):
        barrier.wait()  # release all workers at once → maximal write contention
        close_old_connections()
        try:
            msg = inbox.post_message(
                conversation=conv,
                sender_type="contact",
                body_text=f"m{i}",
                client_msg_id=uuid.uuid4(),
            )
            return msg.seq
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=n) as pool:
        seqs = list(pool.map(worker, range(n)))

    assert sorted(seqs) == list(range(1, n + 1)), "seq range must be dense and gap-free"
    assert len(set(seqs)) == n, "no seq reused"
    assert Message.objects.filter(conversation=conv).count() == n
    conv.refresh_from_db()
    assert conv.last_seq == n
