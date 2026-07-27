import logging

from django.apps import AppConfig

log = logging.getLogger("core.apps")


def _set_sqlite_pragmas(sender, connection, **kwargs):
    """Apply the SQLite pragmas that make the single-writer design correct, on EVERY
    connection — including background-thread connections (CLAUDE.md §8, architecture §4).
    """
    if connection.vendor != "sqlite":
        return
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.execute("PRAGMA busy_timeout=5000;")
        cursor.execute("PRAGMA foreign_keys=ON;")


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.core"
    label = "core"

    def ready(self):
        from django.db.backends.signals import connection_created

        connection_created.connect(_set_sqlite_pragmas, dispatch_uid="core.sqlite_pragmas")
