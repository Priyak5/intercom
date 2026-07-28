from django.apps import AppConfig
from django.conf import settings


class MailConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.mail"
    label = "mail"

    def ready(self):
        # Register the IMAP poller thread. Only booted when RUN_BACKGROUND_THREADS=1
        # (gated by bootstrap) AND IMAP creds are configured — otherwise the factory
        # is registered but the thread would exit fast, so skip registration entirely.
        if not (getattr(settings, "IMAP_HOST", "") and getattr(settings, "IMAP_USER", "")):
            return
        from apps.core import bootstrap
        from apps.mail import poller

        bootstrap.register_background_thread("imap_poller", poller.make_imap_thread)
