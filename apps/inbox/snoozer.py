"""Snooze-expiry sweeper (Phase 5).

Every 60 s, flip any SNOOZED conversation whose `snoozed_until` is now in the past
back to OPEN. Reuses `services.set_status` so the audit line + `conversation.updated`
broadcast happen for free (I5 — one write path).

Follows the sweeper pattern from `apps/inbox/realtime.py::make_sweeper_thread`:
daemon thread, `close_old_connections()` at the top of every loop (I9), swallow all
exceptions so the sweeper never dies.
"""

import logging
import threading
import time

from django.db import close_old_connections

log = logging.getLogger("inbox.snoozer")

SWEEP_INTERVAL = 60.0  # seconds — granularity for reopen; documented Known Limitation


def make_snooze_thread() -> threading.Thread:
    def run():
        # Import inside run() so app registry is ready when the thread starts.
        from apps.inbox import services

        log.info("snooze_sweeper_start interval=%s", SWEEP_INTERVAL)
        while True:
            close_old_connections()
            try:
                services.reopen_expired_snoozes()
            except Exception as exc:  # noqa: BLE001 — a sweeper must never die
                log.warning("snooze_sweep_error error=%r", exc)
            time.sleep(SWEEP_INTERVAL)

    t = threading.Thread(target=run, name="snooze_sweeper", daemon=True)
    t.start()
    return t
