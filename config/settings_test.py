"""Test settings: production settings with the slow/external bits swapped out.

- Database: in-memory SQLite by default, so the suite never touches the real
  (Neon) database named in .env and runs without Docker. CI sets
  TEST_DATABASE=postgres to run against its Postgres service container.
- Password hashing: MD5 for speed. (That prod uses Argon2 is asserted
  separately in tests/test_security_config.py.)
"""

import os

from .settings import *  # noqa: F403

if os.environ.get("TEST_DATABASE") != "postgres":
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
CORS_ALLOWED_ORIGINS = ["http://localhost:3000"]
FRONTEND_URL = "http://localhost:3000"
GITHUB_OAUTH_CLIENT_ID = "test-client-id"
GITHUB_OAUTH_CLIENT_SECRET = "test-client-secret"
GITHUB_OAUTH_REDIRECT_URI = "http://localhost:8000/api/v1/auth/github/callback/"
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# No resend cooldown in tests (its own test turns it back on).
EMAIL_CHALLENGE_RESEND_SECONDS = 0

# Engine: run checks inline, never pause, and never talk to real probers.
CHECK_WORKERS = 1
STATUS_PAGE_MAIL_WORKERS = 1
CHECK_RECHECK_DELAY_SECONDS = 0
PROBER_URLS = ""
PROBER_SHARED_SECRET = ""
SCHEDULER_SHARED_SECRET = "test-scheduler-secret"
PRIMARY_REGION = "primary"

# Notifications: no waiting, no real push keys.
NOTIFY_BACKOFF_SECONDS = (60, 300, 900, 900)
VAPID_PRIVATE_KEY = ""
VAPID_PUBLIC_KEY = ""
# One-click connections are off unless a test turns them on.
TELEGRAM_BOT_TOKEN = ""
TELEGRAM_BOT_USERNAME = ""
TELEGRAM_WEBHOOK_SECRET = ""
DISCORD_CLIENT_ID = ""
DISCORD_CLIENT_SECRET = ""
SLACK_CLIENT_ID = ""
SLACK_CLIENT_SECRET = ""
