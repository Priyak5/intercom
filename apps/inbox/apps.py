from django.apps import AppConfig


class InboxConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.inbox"
    label = "inbox"

    def ready(self):
        # Register the presence/typing sweeper. Runs during get_asgi_application()
        # (config/asgi.py), before maybe_start_background_threads(), so the factory is
        # present when bootstrap starts threads (only when RUN_BACKGROUND_THREADS=1).
        from apps.core import bootstrap
        from apps.inbox import realtime

        bootstrap.register_background_thread("sweeper", realtime.make_sweeper_thread)
