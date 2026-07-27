"""/healthz — a real DB write plus background-thread liveness (architecture §10).

Wired to the docker-compose healthcheck. Returns 200 only when the database accepts a
write and every registered background thread is alive.
"""

import logging

from django.db import connection, transaction
from django.http import JsonResponse

from apps.core.bootstrap import thread_status

log = logging.getLogger("core.health")


def _db_write_ok() -> bool:
    """Exercise the write path against a connection-local TEMP table. A read (SELECT 1)
    would not catch a read-only mount or a held write lock; a TEMP table pollutes no
    schema and works even before migrations have run.
    """
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("CREATE TEMP TABLE IF NOT EXISTS _healthz_probe (ok INTEGER)")
                cursor.execute("INSERT INTO _healthz_probe (ok) VALUES (1)")
                cursor.execute("DELETE FROM _healthz_probe")
        return True
    except Exception as exc:  # noqa: BLE001 — health check must report, not raise.
        log.error("healthz_db_write_failed error=%r", exc)
        return False


def healthz(request):
    threads = thread_status()
    db_ok = _db_write_ok()
    threads_ok = all(threads.values()) if threads else True
    healthy = db_ok and threads_ok
    return JsonResponse(
        {
            "status": "ok" if healthy else "degraded",
            "db": "ok" if db_ok else "fail",
            "threads": threads,
        },
        status=200 if healthy else 503,
    )
