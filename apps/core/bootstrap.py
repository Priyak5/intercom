"""In-process background-thread lifecycle.

One place starts the worker / IMAP poller / sweeper, exactly once, gated by
RUN_BACKGROUND_THREADS (CLAUDE.md §4). Phase 0 registers none; later phases append
factories to _THREAD_FACTORIES. `thread_status()` backs the /healthz liveness check.
"""

import logging
import threading
from collections.abc import Callable

from django.conf import settings

log = logging.getLogger("core.bootstrap")

# name -> zero-arg factory that creates and starts a daemon thread, returning it.
# Empty in Phase 0; populated by phases 2/4/7 (worker, poller, sweeper).
_THREAD_FACTORIES: list[tuple[str, Callable[[], threading.Thread]]] = []

_threads: dict[str, threading.Thread] = {}
_lock = threading.Lock()
_started = False


def maybe_start_background_threads() -> None:
    global _started
    if not getattr(settings, "RUN_BACKGROUND_THREADS", False):
        return
    with _lock:
        if _started:
            return
        _started = True
        for name, factory in _THREAD_FACTORIES:
            _threads[name] = factory()
            log.info("thread_started name=%s", name)
        log.info("background_threads_enabled count=%d", len(_THREAD_FACTORIES))


def thread_status() -> dict[str, bool]:
    """name -> is_alive for every registered background thread (empty in Phase 0)."""
    return {name: thread.is_alive() for name, thread in _threads.items()}
