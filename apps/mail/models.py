"""Persistent IMAP fetch cursor. One row per mailbox account.

The `last_uid` advances BEFORE the message is processed so a crash mid-loop cannot
double-deliver the same email; if we crash after saving the cursor but before persisting
the Message, the poller resumes at the next UID — the unique(conversation, client_msg_id)
index (with `client_msg_id = uuid5(NAMESPACE_URL, message_id)`) also dedupes any replay
that does reach post_message.

UIDVALIDITY is captured because IMAP UIDs are only stable within a UIDVALIDITY epoch;
if the mailbox is renamed/recreated the value bumps and we must reset `last_uid = 0`.
"""

from django.db import models

from apps.core.models import BaseModel


class MailboxCursor(BaseModel):
    account = models.CharField(max_length=255, unique=True)  # IMAP username
    uidvalidity = models.PositiveBigIntegerField(default=0)
    last_uid = models.PositiveBigIntegerField(default=0)

    def __str__(self) -> str:
        return f"cursor<{self.account}@uv{self.uidvalidity}#{self.last_uid}>"
