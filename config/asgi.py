"""
ASGI entrypoint. One process serves HTTP and WebSockets (CLAUDE.md I1).

Two jobs beyond routing:
  1. Assert the single-worker invariant at boot and crash loudly if violated (I1).
  2. Boot background threads (worker, IMAP poller, sweeper) once, gated by
     RUN_BACKGROUND_THREADS. No threads exist yet in Phase 0; the hook is a no-op
     until later phases register them.
"""

import asyncio
import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")


def _assert_single_worker() -> None:
    """I1: the InMemoryChannelLayer + in-process presence/threads only work in ONE
    ASGI worker. uvicorn does not expose its worker count to the app, so enforcement
    reads the operator-set UVICORN_WORKERS (the Docker CMD passes --workers 1 and sets
    UVICORN_WORKERS=1). If background threads are enabled with >1 worker, refuse to boot.
    """
    run_bg = os.environ.get("RUN_BACKGROUND_THREADS", "").strip().lower() in ("1", "true", "yes", "on")
    try:
        workers = int(os.environ.get("UVICORN_WORKERS", "1"))
    except ValueError:
        workers = 1
    if run_bg and workers > 1:
        raise RuntimeError(
            f"I1 violated: RUN_BACKGROUND_THREADS is on with UVICORN_WORKERS={workers}. "
            "This design requires exactly one ASGI worker — a second worker silently "
            "splits the channel layer and presence state. Run with --workers 1."
        )


_assert_single_worker()

from django.core.asgi import get_asgi_application  # noqa: E402  (must follow settings setup)

django_asgi_app = get_asgi_application()

from channels.auth import AuthMiddlewareStack  # noqa: E402
from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402

from apps.core.bootstrap import maybe_start_background_threads  # noqa: E402
from apps.inbox import realtime  # noqa: E402
from apps.inbox.routing import websocket_urlpatterns  # noqa: E402

maybe_start_background_threads()


async def lifespan_app(scope, receive, send):
    """Capture the ASGI main event loop at startup so background threads (the sweeper,
    and later the IMAP poller / job worker) can schedule broadcasts onto it via
    realtime.broadcast. Uvicorn drives the lifespan protocol by default.
    """
    while True:
        message = await receive()
        if message["type"] == "lifespan.startup":
            realtime.set_main_loop(asyncio.get_running_loop())
            await send({"type": "lifespan.startup.complete"})
        elif message["type"] == "lifespan.shutdown":
            realtime.clear_main_loop()
            await send({"type": "lifespan.shutdown.complete"})
            return


application = ProtocolTypeRouter(
    {
        "lifespan": lifespan_app,
        "http": django_asgi_app,
        "websocket": AuthMiddlewareStack(URLRouter(websocket_urlpatterns)),
    }
)
