"""Proving an email address belongs to the user before it receives alerts.

An email contact for the user's own account address is trusted. Any other address gets a
6-digit code (emailed to that address, valid 30 minutes, 5 tries); until it's entered, the
contact is saved but receives nothing. Otherwise anyone could point RootPulse at a
stranger's inbox.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from monitoring.models import AlertContact

logger = logging.getLogger(__name__)

TTL = timedelta(minutes=30)
MAX_ATTEMPTS = 5


def _digest(contact_id, code: str) -> str:
    return hmac.new(
        settings.SECRET_KEY.encode(), f"{contact_id}:{code}".encode(), hashlib.sha256
    ).hexdigest()


def needs_verification(contact: AlertContact, account_email: str) -> bool:
    return (
        contact.channel == AlertContact.Channel.EMAIL
        and contact.config.get("address", "").lower() != account_email.lower()
    )


def mark_trusted(contact: AlertContact) -> None:
    contact.verified_at = timezone.now()
    contact.verification_hash = ""
    contact.verification_expires_at = None
    contact.verification_attempts = 0


def start(contact: AlertContact) -> bool:
    """Email a fresh code to the contact's address. Returns whether the email went out."""
    code = f"{secrets.randbelow(1_000_000):06d}"
    contact.verified_at = None
    contact.verification_hash = _digest(contact.id, code)
    contact.verification_expires_at = timezone.now() + TTL
    contact.verification_attempts = 0
    contact.save(
        update_fields=[
            "verified_at",
            "verification_hash",
            "verification_expires_at",
            "verification_attempts",
        ]
    )
    try:
        send_mail(
            f"Confirm your RootPulse alert address: {code}",
            "Someone (hopefully you) asked RootPulse to send alerts to this address.\n\n"
            f"Confirmation code: {code}\n\n"
            "It expires in 30 minutes. If you didn't ask for this, ignore this email and no "
            "alerts will be sent here.",
            settings.DEFAULT_FROM_EMAIL,
            [contact.config["address"]],
        )
    except Exception:  # noqa: BLE001 - the caller reports it; the user can ask for a new code
        logger.exception("Failed to send the verification email")
        return False
    return True


def check(contact: AlertContact, code: str) -> bool:
    """Verify a code. Every wrong guess counts; after MAX_ATTEMPTS the code is dead."""
    if (
        not contact.verification_hash
        or contact.verification_expires_at is None
        or contact.verification_expires_at < timezone.now()
        or contact.verification_attempts >= MAX_ATTEMPTS
    ):
        return False
    if not hmac.compare_digest(contact.verification_hash, _digest(contact.id, code)):
        contact.verification_attempts += 1
        contact.save(update_fields=["verification_attempts"])
        return False
    mark_trusted(contact)
    contact.save(
        update_fields=[
            "verified_at",
            "verification_hash",
            "verification_expires_at",
            "verification_attempts",
        ]
    )
    return True
