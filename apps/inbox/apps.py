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
        from apps.inbox import ai, realtime, snoozer

        bootstrap.register_background_thread("sweeper", realtime.make_sweeper_thread)
        bootstrap.register_background_thread("snooze_sweeper", snoozer.make_snooze_thread)
        # AI summary worker (Phase 7). Registered unconditionally — with an empty
        # ANTHROPIC_API_KEY the worker still runs and produces the deterministic
        # fallback (I8), which is the correct behaviour.
        bootstrap.register_background_thread("ai_worker", ai.make_ai_worker_thread)
