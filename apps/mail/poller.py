"""IMAP poller thread (Phase 4).

Follows the sweeper pattern (apps/inbox/realtime.py::make_sweeper_thread): daemon
thread, `close_old_connections()` per iteration (I9), never dies on exception. Every
poll cycle:

  1. Refresh DB connections.
  2. Pick the workspace to route inbound mail into (MAIL_WORKSPACE_ID, else the first
     Workspace) — single-workspace-per-deploy for email (documented tradeoff).
  3. IMAP login → SELECT INBOX → check UIDVALIDITY.
  4. `UID SEARCH UID last_uid+1:*`, fetch RFC822 for each new UID.
  5. Advance `MailboxCursor.last_uid` **before** processing so a crash can't
     double-deliver; `services.post_message` also dedupes on Message-Id via
     uuid5 client_msg_id.

30-second polling: adequate latency for a POC. Documented in README.
"""

import logging
import threading
import time

from django.conf import settings
from django.db import close_old_connections
from imapclient import IMAPClient

from apps.mail import inbound

log = logging.getLogger("mail.poller")


def _pick_workspace():
    """Env-configured or first Workspace. Returns None until at least one exists."""
    from apps.accounts.models import Workspace

    wid = (getattr(settings, "MAIL_WORKSPACE_ID", "") or "").strip()
    if wid:
        return Workspace.objects.filter(id=wid).first()
    return Workspace.objects.order_by("created_at").first()


def _reset_cursor_if_uidvalidity_changed(cursor, uidvalidity: int) -> None:
    if cursor.uidvalidity != uidvalidity:
        if cursor.uidvalidity:
            log.warning(
                "mailbox_uidvalidity_changed account=%s old=%s new=%s reset_uid",
                cursor.account, cursor.uidvalidity, uidvalidity,
            )
        cursor.uidvalidity = uidvalidity
        cursor.last_uid = 0
        cursor.save(update_fields=["uidvalidity", "last_uid", "updated_at"])


def _poll_once() -> None:
    from apps.mail.models import MailboxCursor

    workspace = _pick_workspace()
    if workspace is None:
        log.debug("mail_poll_skip_no_workspace")
        return

    account = settings.IMAP_USER
    cursor, _ = MailboxCursor.objects.get_or_create(account=account)

    with IMAPClient(settings.IMAP_HOST, port=settings.IMAP_PORT, ssl=True, timeout=30) as client:
        client.login(settings.IMAP_USER, settings.IMAP_PASS)
        info = client.select_folder("INBOX")
        uidvalidity = int(info.get(b"UIDVALIDITY", 0) or 0)
        _reset_cursor_if_uidvalidity_changed(cursor, uidvalidity)

        # UID SEARCH for anything strictly greater than the persisted cursor.
        start = cursor.last_uid + 1
        uids = client.search(["UID", f"{start}:*"])
        # IMAP servers echo the highest UID when the range is empty; filter.
        uids = sorted({u for u in uids if u >= start})
        if not uids:
            return

        for uid in uids:
            resp = client.fetch([uid], ["RFC822"])
            body = resp.get(uid, {}).get(b"RFC822")
            if not body:
                log.warning("mail_fetch_empty uid=%s", uid)
                cursor.last_uid = uid
                cursor.save(update_fields=["last_uid", "updated_at"])
                continue
            # Advance cursor BEFORE processing (crash-safety).
            cursor.last_uid = uid
            cursor.save(update_fields=["last_uid", "updated_at"])
            try:
                inbound.process_inbound(body, workspace=workspace)
            except Exception as exc:  # noqa: BLE001 — one bad message must not stop the loop
                log.warning("mail_process_error uid=%s error=%r", uid, exc)


def make_imap_thread() -> threading.Thread:
    interval = int(getattr(settings, "IMAP_POLL_INTERVAL", 30))

    def run():
        log.info("imap_poller_start host=%s user=%s interval=%s", settings.IMAP_HOST, settings.IMAP_USER, interval)
        while True:
            close_old_connections()
            try:
                _poll_once()
            except Exception as exc:  # noqa: BLE001 — a poller must never die
                log.warning("mail_poll_error error=%r", exc)
            time.sleep(interval)

    t = threading.Thread(target=run, name="imap_poller", daemon=True)
    t.start()
    return t
