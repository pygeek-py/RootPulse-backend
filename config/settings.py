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

# Passwordless sign-in (accounts/passwordless.py).
EMAIL_CHALLENGE_TTL_SECONDS = 15 * 60
EMAIL_CHALLENGE_RESEND_SECONDS = 60  # one email per address per minute
EMAIL_CODE_MAX_ATTEMPTS = 5

if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
