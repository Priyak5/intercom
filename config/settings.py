"""
Django settings for the support platform POC — one file, env-driven.

Every secret and every environment-specific value comes from the environment
(CLAUDE.md I10). `settings.py` supplies defaults only for non-secret values.
Read `architecture.md` before changing anything structural here.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Load .env for local dev. In Docker, env is supplied by compose `env_file`, so
# this is a harmless no-op there (load_dotenv does not override existing vars).
load_dotenv(BASE_DIR / ".env")


# --- tiny env helpers -------------------------------------------------------

def env(key: str, default: str | None = None) -> str | None:
    return os.environ.get(key, default)


def env_bool(key: str, default: bool = False) -> bool:
    return os.environ.get(key, str(default)).strip().lower() in ("1", "true", "yes", "on")


def env_list(key: str, default: str = "") -> list[str]:
    return [item.strip() for item in os.environ.get(key, default).split(",") if item.strip()]


# --- core -------------------------------------------------------------------

# SECURITY: no insecure fallback in production. A dev-only default keeps `manage.py`
# usable locally without a .env; production must set SECRET_KEY (checked when DEBUG=False).
SECRET_KEY = env("SECRET_KEY", "django-insecure-dev-only-key-change-me")

DEBUG = env_bool("DEBUG", False)

if not DEBUG and SECRET_KEY.startswith("django-insecure-"):
    raise RuntimeError("SECRET_KEY must be set from the environment when DEBUG=False (I10).")

# localhost/127.0.0.1 are always allowed so the in-container healthcheck (which hits
# http://localhost:8000/healthz) passes regardless of the public hostname.
ALLOWED_HOSTS = list(dict.fromkeys(["localhost", "127.0.0.1", *env_list("ALLOWED_HOSTS")]))

CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS")

# Non-secret, but handy for building absolute URLs and the custom-domain flow.
BASE_URL = env("BASE_URL", "http://localhost:8000")
BASE_DOMAIN = env("BASE_DOMAIN", "localhost")


# --- applications -----------------------------------------------------------

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "channels",
    "rest_framework",
    "apps.core",
    "apps.accounts",
    "apps.inbox",
    "apps.mail",
    "apps.kb",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # WhiteNoise serves collected static from the single ASGI process (no nginx, and
    # static is not shared with the Caddy container). See architecture.md §3.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Derives request.workspace/membership; needs request.user, so it follows auth (I6).
    "apps.core.middleware.TenantMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.accounts.context.workspace_context",
            ],
        },
    },
]

# ASGI only — the single uvicorn process serves both HTTP and WebSockets (I1).
ASGI_APPLICATION = "config.asgi.application"

# In-memory channel layer: correct for one process, and the load-bearing choice that
# lets background threads use `group_send` (architecture.md §2).
CHANNEL_LAYERS = {
    "default": {"BACKEND": "channels.layers.InMemoryChannelLayer"},
}


# --- database ---------------------------------------------------------------

# All mutable state lives under DATA_DIR (the docker volume mounted at /app/data).
DATA_DIR = Path(env("DATA_DIR", str(BASE_DIR / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": DATA_DIR / "db.sqlite3",
        # busy_timeout is also set via PRAGMA, but Django's own lock wait should match.
        "OPTIONS": {"timeout": 5},
        # File-based test DB (not :memory:) so the transactional concurrency tests, whose
        # worker threads each open their own connection, share one database.
        "TEST": {"NAME": str(DATA_DIR / "test_db.sqlite3")},
    }
}
# WAL / synchronous / foreign_keys / busy_timeout pragmas are applied to *every*
# connection (including background threads) by apps.core.apps.CoreConfig.ready().

AUTH_USER_MODEL = "accounts.User"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# --- password validation ----------------------------------------------------

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# --- i18n -------------------------------------------------------------------

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True


# --- static -----------------------------------------------------------------

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"] if (BASE_DIR / "static").exists() else []
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    # Compressed (not manifest) so collectstatic can't fail on an unreferenced asset.
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}


# --- security ---------------------------------------------------------------

# Behind Caddy: trust the proxy's scheme header so Django knows requests are HTTPS
# (required for Secure cookies and correct CSRF origin checks).
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = not DEBUG

CSRF_COOKIE_HTTPONLY = False  # JS reads the CSRF cookie for fetch() headers.
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = not DEBUG


# --- auth / DRF -------------------------------------------------------------

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"

REST_FRAMEWORK = {
    # Dashboard is session-cookie authenticated; SessionAuthentication also enforces
    # CSRF on unsafe methods for us. Widget token auth arrives in Phase 3.
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    # Default deny: every API view requires auth unless it opts out with AllowAny.
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    # Maps typed service-layer exceptions (core.exceptions) to status codes (I5).
    "EXCEPTION_HANDLER": "apps.core.exceptions.drf_exception_handler",
}


# --- email / invites --------------------------------------------------------

# Console backend logs invite emails to stdout; the invite link is also surfaced in
# the team UI. Swaps to real SMTP in Phase 4 by setting EMAIL_BACKEND in the env.
EMAIL_BACKEND = env("EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", "support@localhost")
INVITE_TTL_HOURS = int(env("INVITE_TTL_HOURS", "168"))  # 7 days

# Widget visitor-session token lifetime (Phase 2 minimal surface; Phase 3 hardens it).
WIDGET_TOKEN_TTL_HOURS = int(env("WIDGET_TOKEN_TTL_HOURS", "24"))


# --- email channel (Phase 4) ------------------------------------------------

# The IMAP poller polls a single mailbox; every inbound email that isn't threaded via
# In-Reply-To or a plus-token is routed to this workspace (empty => first Workspace
# at poll time). Documented Known Limitation: one workspace per deploy for email.
MAIL_WORKSPACE_ID = env("MAIL_WORKSPACE_ID", "")
MAIL_DOMAIN = env("MAIL_DOMAIN", "localhost")
MAIL_FROM = env("MAIL_FROM", DEFAULT_FROM_EMAIL)

IMAP_HOST = env("IMAP_HOST", "")
IMAP_PORT = int(env("IMAP_PORT", "993"))
IMAP_USER = env("IMAP_USER", "")
IMAP_PASS = env("IMAP_PASS", "")
IMAP_POLL_INTERVAL = int(env("IMAP_POLL_INTERVAL", "30"))

SMTP_HOST = env("SMTP_HOST", "")
SMTP_PORT = int(env("SMTP_PORT", "587"))
SMTP_USER = env("SMTP_USER", "")
SMTP_PASS = env("SMTP_PASS", "")
# Hard timeout: agent replies must not hang the dashboard request (I8-adjacent).
SMTP_TIMEOUT = int(env("SMTP_TIMEOUT", "8"))


# --- AI summarisation (Phase 7) --------------------------------------------

# Anthropic-Claude-Haiku generates conversation summaries in a background worker
# thread. CLAUDE.md I8 mandates: never blocks a request, hard timeout, one retry,
# then a deterministic non-LLM fallback. Empty key → worker still runs but skips
# the LLM call and produces degraded fallback summaries.
ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY", "")
AI_MODEL = env("AI_MODEL", "claude-haiku-4-5")
AI_TIMEOUT_SEC = int(env("AI_TIMEOUT_SEC", "8"))  # I8
AI_MAX_INPUT_TOKENS = int(env("AI_MAX_INPUT_TOKENS", "8000"))
AI_MAX_OUTPUT_TOKENS = int(env("AI_MAX_OUTPUT_TOKENS", "512"))
AI_ENQUEUE_THRESHOLD = int(env("AI_ENQUEUE_THRESHOLD", "5"))
AI_WORKER_POLL_INTERVAL_SEC = int(env("AI_WORKER_POLL_INTERVAL_SEC", "2"))
# List prices per 1M tokens; used by the admin cost dashboard. Update as pricing
# changes — no code depends on the specific values.
AI_PRICE_INPUT_USD_PER_MTOKEN = float(env("AI_PRICE_INPUT_USD_PER_MTOKEN", "1.00"))
AI_PRICE_OUTPUT_USD_PER_MTOKEN = float(env("AI_PRICE_OUTPUT_USD_PER_MTOKEN", "5.00"))


# --- logging ----------------------------------------------------------------

# One structured line per event on stdout (architecture.md §10 / CLAUDE.md §6).
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"kv": {"format": "%(asctime)s level=%(levelname)s logger=%(name)s %(message)s"}},
    "handlers": {"stdout": {"class": "logging.StreamHandler", "formatter": "kv"}},
    "root": {"handlers": ["stdout"], "level": env("LOG_LEVEL", "INFO")},
}


# --- background threads -----------------------------------------------------

# Gated so tests and one-off management commands don't start the worker/poller.
RUN_BACKGROUND_THREADS = env_bool("RUN_BACKGROUND_THREADS", False)
