"""Passwordless sign-in: an emailed link + 6-digit code (either one works).

Flow: `start()` stores a hashed challenge and emails the link and code;
`verify_link()` / `verify_code()` consume it and return the user, creating the
account on first use. Everything that could reveal whether an address already
has an account (send/no-send, timing aside) is deliberately uniform.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import uuid
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

from .models import EmailChallenge, User
from .timezones import is_valid_timezone

logger = logging.getLogger(__name__)


class InvalidChallenge(Exception):
    """Wrong, expired, used-up or unknown link/code. One error for all of them."""


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _code_digest(email: str, code: str) -> str:
    # Keyed and email-salted: a leaked table can't be reversed with a
    # precomputed 10^6 lookup.
    return hmac.new(settings.SECRET_KEY.encode(), f"{email}:{code}".encode(), "sha256").hexdigest()


def start(email: str, purpose: str) -> bool:
    """Create a challenge and email it. Returns whether an email was sent.

    Callers must respond identically either way. Not sent when: logging in to
    an address with no (or a disabled) account, or when an unused email went
    out less than a minute ago.
    """
    existing = User.objects.filter(email__iexact=email).first()
    if purpose == EmailChallenge.LOGIN and (existing is None or not existing.is_active):
        return False
    if existing is not None and not existing.is_active:
        return False
    if existing is None and not settings.SIGNUPS_OPEN:
        return False  # sign-up is closed; answer exactly as for any other "no email sent"

    now = timezone.now()
    cooldown = now - timedelta(seconds=settings.EMAIL_CHALLENGE_RESEND_SECONDS)
    # Only a still-pending email counts: once it has been used, signing in again
    # straight away (sign up, sign out, sign back in) must work.
    pending = EmailChallenge.objects.filter(
        email=email, consumed_at__isnull=True, created_at__gt=cooldown
    )
    if pending.exists():
        return False

    link_token = secrets.token_urlsafe(32)
    code = f"{secrets.randbelow(1_000_000):06d}"
    with transaction.atomic():
        # Only the newest email is valid; older links/codes stop working.
        EmailChallenge.objects.filter(email=email, consumed_at__isnull=True).update(consumed_at=now)
        challenge = EmailChallenge.objects.create(
            email=email,
            purpose=purpose,
            link_hash=_digest(link_token),
            code_hash=_code_digest(email, code),
            expires_at=now + timedelta(seconds=settings.EMAIL_CHALLENGE_TTL_SECONDS),
        )

    link = f"{settings.FRONTEND_URL}/auth/verify#token={link_token}"
    minutes = settings.EMAIL_CHALLENGE_TTL_SECONDS // 60
    try:
        send_mail(
            f"Your RootPulse sign-in code: {code}",
            f"Sign in to RootPulse with this link:\n\n{link}\n\n"
            f"Or enter this code: {code}\n\n"
            f"Both expire in {minutes} minutes and can be used once. "
            "If you didn't ask for this, you can ignore this email.",
            settings.DEFAULT_FROM_EMAIL,
            [email],
        )
    except Exception:  # noqa: BLE001 - an SMTP outage must not change the response
        logger.exception("Failed to send sign-in email")
        # Nothing reached the user, so don't let this challenge hold the resend cooldown.
        EmailChallenge.objects.filter(pk=challenge.pk).update(consumed_at=timezone.now())
        return False
    return True


def _finish(challenge: EmailChallenge, browser_timezone: str | None) -> tuple[User, bool]:
    """Mark consumed and return (user, created), creating the user for a sign-up."""
    challenge.consumed_at = timezone.now()
    challenge.save(update_fields=["consumed_at"])

    created = False
    user = User.objects.filter(email__iexact=challenge.email).first()
    if user is None:
        if challenge.purpose != EmailChallenge.SIGNUP:
            raise InvalidChallenge  # account vanished between request and use
        if not settings.SIGNUPS_OPEN:
            raise InvalidChallenge  # sign-up was closed after this email went out
        user = User(username=uuid.uuid4().hex, email=challenge.email)
        if browser_timezone and is_valid_timezone(browser_timezone):
            user.timezone = browser_timezone
        user.set_unusable_password()
        user.save()
        created = True
    if not user.is_active:
        raise InvalidChallenge
    return user, created


def verify_link(token: str, browser_timezone: str | None = None) -> tuple[User, bool]:
    with transaction.atomic():
        challenge = (
            EmailChallenge.objects.select_for_update()
            .filter(
                link_hash=_digest(token), consumed_at__isnull=True, expires_at__gt=timezone.now()
            )
            .first()
        )
        if challenge is None:
            raise InvalidChallenge
        return _finish(challenge, browser_timezone)


def verify_code(email: str, code: str, browser_timezone: str | None = None) -> tuple[User, bool]:
    with transaction.atomic():
        challenge = (
            EmailChallenge.objects.select_for_update()
            .filter(email=email, consumed_at__isnull=True, expires_at__gt=timezone.now())
            .order_by("-created_at")
            .first()
        )
        if challenge is None or challenge.failed_attempts >= settings.EMAIL_CODE_MAX_ATTEMPTS:
            raise InvalidChallenge
        if hmac.compare_digest(challenge.code_hash, _code_digest(email, code)):
            return _finish(challenge, browser_timezone)
        challenge.failed_attempts += 1
        challenge.save(update_fields=["failed_attempts"])
    # Raised after the block so the attempt count is committed, not rolled back.
    raise InvalidChallenge
