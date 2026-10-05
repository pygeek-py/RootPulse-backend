"""
Django settings for RootPulse.

See docs/plan/01-tech-stack.md and docs/plan/04-security.md for the
reasoning behind the choices below (Argon2 hashing, JWT auth, CORS
scoping, etc.) — this file implements that plan, it doesn't redesign it.
"""

import os
from datetime import timedelta
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    return default if value is None else value.lower() in ("1", "true", "yes", "on")


def env_list(name: str, default: str = "") -> list[str]:
    raw = os.environ.get(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


# SECURITY WARNING: keep the production secret key out of source control.
SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY", "django-insecure-local-dev-only-do-not-use-in-prod"
)

DEBUG = env_bool("DJANGO_DEBUG", True)

# JWTs are signed with SECRET_KEY, so a guessable key means forgeable logins.
# Refuse to start in production with the dev default or a short key.
if not DEBUG and (SECRET_KEY.startswith("django-insecure") or len(SECRET_KEY) < 32):
    raise ImproperlyConfigured("Set DJANGO_SECRET_KEY to a random value of 32+ characters.")

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")


# Application definition

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third party
    "rest_framework",
    "rest_framework_simplejwt",
    "rest_framework_simplejwt.token_blacklist",
    "drf_spectacular",
    "corsheaders",
    # Local
    "accounts",
    "monitoring",
    "incidents",
    "notifications",
    "analytics",
    "deploys",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"


# Database — Postgres everywhere (docs/plan/01-tech-stack.md). DATABASE_URL
# (set by Neon, and by Render at deploy time) takes priority; local dev
# without it falls back to the docker-compose Postgres service.

if os.environ.get("DATABASE_URL"):
    DATABASES = {
        "default": dj_database_url.parse(
            os.environ["DATABASE_URL"],
            conn_max_age=600,
            # SSL is mandatory for Neon/Render Postgres; sqlite:/// is allowed
            # only as a zero-setup local override and has no SSL concept.
            ssl_require=not os.environ["DATABASE_URL"].startswith("sqlite"),
        )
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("DB_NAME", "rootpulse"),
            "USER": os.environ.get("DB_USER", "rootpulse"),
            "PASSWORD": os.environ.get("DB_PASSWORD", "rootpulse"),
            "HOST": os.environ.get("DB_HOST", "localhost"),
            "PORT": os.environ.get("DB_PORT", "5432"),
        }
    }


# Custom user model — set before the first migration, never retrofitted.
AUTH_USER_MODEL = "accounts.User"


# Password hashing — Argon2 first (docs/plan/04-security.md #1).
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 10},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# Internationalization

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True


# Static files

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# Django REST Framework

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    # Standard page-number pagination by default; high-volume endpoints
    # (checks, incidents — docs/plan/02-database-and-api.md) override this
    # per-view with CursorPagination when they're built in later phases.
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 25,
    "DEFAULT_THROTTLE_CLASSES": (
        "rest_framework.throttling.UserRateThrottle",
        "rest_framework.throttling.AnonRateThrottle",
    ),
    "DEFAULT_THROTTLE_RATES": {
        "user": "60/min",
        "anon": "20/min",
        # Per-endpoint limits for the auth surface (docs/plan/04-security.md #6).
        "auth_email_start": "20/hour",  # each one sends an email
        "auth_verify": "30/min",
        "auth_refresh": "30/min",
        "notify_test": "10/min",  # each one sends a real alert
        "contact_verify": "10/hour",
        "analytics": "60/min",
        "deploy_webhook": "120/min",  # per source IP; a deploy is rare, a retry storm is not
        "integration_connect": "30/hour",  # starting a Telegram/Discord/Slack connection
        "integration_poll": "120/min",  # the page asking "did the Telegram link finish yet?"
        "integration_callback": "30/min",
        "heartbeat": "120/min",  # per source IP; a job normally pings once per run
        "auth_github": "20/min",
    },
    "EXCEPTION_HANDLER": "config.exceptions.api_exception_handler",
    # How many reverse proxies sit in front of the app (Render = 1). Without
    # this DRF trusts the whole X-Forwarded-For header, which lets a client
    # spoof its IP and walk around every per-IP throttle.
    "NUM_PROXIES": int(os.environ.get("NUM_PROXIES", "0")),
}

SPECTACULAR_SETTINGS = {
    "TITLE": "RootPulse API",
    "DESCRIPTION": "Uptime, incident & dependency monitoring — REST API v1.",
    "VERSION": "0.1.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "ENUM_NAME_OVERRIDES": {
        "StatusEnum": "monitoring.models.Monitor.Status",
        "TypeEnum": "monitoring.models.Monitor.Type",
        "DeploySourceTypeEnum": "deploys.models.DeploySource.Type",
        "DeliveryStatusEnum": "notifications.models.NotificationDelivery.Status",
        "DeliveryEventEnum": "notifications.models.NotificationDelivery.Event",
    },
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=15),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=30),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
}


# CORS — explicit allowlist only, never a wildcard (docs/plan/04-security.md #8).
CORS_ALLOWED_ORIGINS = env_list("CORS_ALLOWED_ORIGINS", "http://localhost:3000")
CORS_ALLOW_CREDENTIALS = True
# Browsers hide most response headers from cross-origin scripts. The frontend needs this one
# to name a downloaded CSV.
CORS_EXPOSE_HEADERS = ["Content-Disposition"]


# Auth (docs/plan/04-security.md #1, #7)

# Where users land after OAuth and where password-reset links point.
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:3000").rstrip("/")

# The refresh token lives in an httpOnly cookie scoped to the auth endpoints.
# In production the frontend (Vercel) and API (Render) are different *sites*,
# so the cookie must be SameSite=None; Secure to be sent at all — which is why
# cookie-using endpoints also verify the Origin header (accounts/security.py).
# Locally both are `localhost` (same site), where Strict is fine and stricter.
AUTH_REFRESH_COOKIE_NAME = "refresh_token"
AUTH_REFRESH_COOKIE_PATH = "/api/v1/auth/"
AUTH_REFRESH_COOKIE_SECURE = not DEBUG
AUTH_REFRESH_COOKIE_SAMESITE = os.environ.get(
    "AUTH_REFRESH_COOKIE_SAMESITE", "Strict" if DEBUG else "None"
)

GITHUB_OAUTH_CLIENT_ID = os.environ.get("GITHUB_OAUTH_CLIENT_ID", "")
GITHUB_OAUTH_CLIENT_SECRET = os.environ.get("GITHUB_OAUTH_CLIENT_SECRET", "")
# Must match the callback URL registered on the GitHub OAuth app.
GITHUB_OAUTH_REDIRECT_URI = os.environ.get(
    "GITHUB_OAUTH_REDIRECT_URI", "http://localhost:8000/api/v1/auth/github/callback/"
)

# Sign-in emails go over SMTP whenever EMAIL_HOST is set (Gmail, Resend, Brevo…
# all work), and print to the server console otherwise. EMAIL_BACKEND overrides.
EMAIL_HOST = os.environ.get("EMAIL_HOST", "")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)  # STARTTLS on 587
EMAIL_USE_SSL = env_bool("EMAIL_USE_SSL", False)  # implicit TLS on 465 (then disable TLS)
# Sign-in emails are sent inside the request, so a dead SMTP server must fail
# fast: a short timeout, with accounts.mail retrying a stalled connection.
EMAIL_TIMEOUT = 4
EMAIL_BACKEND = os.environ.get(
    "EMAIL_BACKEND",
    (
        "accounts.mail.RetryingSMTPBackend"
        if EMAIL_HOST
        else "django.core.mail.backends.console.EmailBackend"
    ),
)
# Most providers (Gmail included) only deliver mail whose From matches the
# authenticated account or a verified domain, so set this to match.
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "RootPulse <noreply@rootpulse.dev>")

# Public base URL of this API: used to build heartbeat ping URLs shown to users.
API_PUBLIC_URL = os.environ.get("API_PUBLIC_URL", "http://localhost:8000").rstrip("/")
# A free-tier guard: one user can't queue unbounded checks (docs/plan/00-overview.md).
MAX_MONITORS_PER_USER = int(os.environ.get("MAX_MONITORS_PER_USER", "50"))

# Monitoring engine (monitoring/engine.py).
PRIMARY_REGION = os.environ.get("PRIMARY_REGION", "primary")  # label for checks run by this API
CHECK_WORKERS = int(os.environ.get("CHECK_WORKERS", "20"))
CHECK_BATCH_SIZE = int(os.environ.get("CHECK_BATCH_SIZE", "100"))
# Stop starting new checks after this long, so the trigger's HTTP request can finish.
CHECK_PASS_BUDGET_SECONDS = int(os.environ.get("CHECK_PASS_BUDGET_SECONDS", "80"))
CHECK_RECHECK_DELAY_SECONDS = int(os.environ.get("CHECK_RECHECK_DELAY_SECONDS", "5"))
CHECK_TCP_TIMEOUT = int(os.environ.get("CHECK_TCP_TIMEOUT", "10"))
CHECK_USER_AGENT = "RootPulse/1.0 (+https://rootpulse.dev; uptime monitoring)"
# Regional probers (Cloudflare Workers): "name=https://url,name=https://url".
PROBER_URLS = os.environ.get("PROBER_URLS", "")
PROBER_SHARED_SECRET = os.environ.get("PROBER_SHARED_SECRET", "")
# Signs the GitHub Actions call to /internal/run-due-checks/. Separate from the prober
# secret so each can be rotated without the other.
SCHEDULER_SHARED_SECRET = os.environ.get("SCHEDULER_SHARED_SECRET", "")

# Notifications (notifications/).
NOTIFY_MAX_ATTEMPTS = 5
NOTIFY_BACKOFF_SECONDS = (60, 300, 900, 900)  # after attempt 1, 2, 3, 4 (docs/plan/03 section 5)
NOTIFY_WORKERS = int(os.environ.get("NOTIFY_WORKERS", "8"))
NOTIFY_BATCH_SIZE = int(os.environ.get("NOTIFY_BATCH_SIZE", "100"))
NOTIFY_HTTP_TIMEOUT = 10
NOTIFY_MAX_REMINDERS = 24  # a long outage reminds at most this many times
MAX_ALERT_CONTACTS_PER_USER = int(os.environ.get("MAX_ALERT_CONTACTS_PER_USER", "20"))
# Web push (VAPID). Generate once with: python manage.py generate_vapid_keys
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "mailto:alerts@rootpulse.dev")

# One-click connections (notifications/integrations.py). Each is optional: a channel whose
# credentials are missing simply doesn't offer "Connect", and people can still paste a
# webhook URL or their own bot token.
#   Telegram: ONE bot owned by this deployment (created with @BotFather). Locally it is polled;
#   in production set TELEGRAM_WEBHOOK_SECRET and run `manage.py telegram_webhook set`.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_BOT_USERNAME = os.environ.get("TELEGRAM_BOT_USERNAME", "").lstrip("@")
TELEGRAM_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
#   Discord and Slack: one OAuth app each. The redirect URL to register is
#   <API_PUBLIC_URL>/api/v1/integrations/<discord|slack>/callback/
DISCORD_CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET", "")
SLACK_CLIENT_ID = os.environ.get("SLACK_CLIENT_ID", "")
SLACK_CLIENT_SECRET = os.environ.get("SLACK_CLIENT_SECRET", "")
INTEGRATION_LINK_TTL_SECONDS = 15 * 60

# Analytics (analytics/). Raw checks are kept this long (docs/plan/03 section 14); after that only
# the rollups remain. Each rollup pass recomputes this many trailing hours, which also covers
# checks that were written a little late.
CHECK_RETENTION_DAYS = int(os.environ.get("CHECK_RETENTION_DAYS", "90"))
ROLLUP_LOOKBACK_HOURS = 2

# Deploys (deploys/). An incident is linked to the latest deploy of its monitor's tagged service
# that finished within this long before it started.
DEPLOY_CORRELATION_WINDOW_SECONDS = int(os.environ.get("DEPLOY_CORRELATION_WINDOW_SECONDS", "300"))
DEPLOY_MAX_BODY_BYTES = 256 * 1024
MAX_DEPLOY_SOURCES_PER_USER = int(os.environ.get("MAX_DEPLOY_SOURCES_PER_USER", "10"))

# Passwordless sign-in (accounts/passwordless.py).
EMAIL_CHALLENGE_TTL_SECONDS = 15 * 60
EMAIL_CHALLENGE_RESEND_SECONDS = 60  # one email per address per minute
EMAIL_CODE_MAX_ATTEMPTS = 5

if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
