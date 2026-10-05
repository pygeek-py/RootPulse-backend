"""Sending the queued subscriber emails: one pass claims what is due, sends it, and returns.

The same shape as the alert dispatcher (notifications/dispatcher.py): the table is the queue,
`select_for_update(skip_locked=True)` stops overlapping passes claiming the same row, and the
network call happens outside any lock. Plain text only, so nothing a page owner typed can be
interpreted as markup.
"""

from __future__ import annotations

import logging
import smtplib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.conf import settings
from django.core.mail import EmailMessage
from django.db import connections, transaction
from django.utils import timezone

from notifications.services import in_maintenance

from . import services
from .models import StatusPageEmail as Email

logger = logging.getLogger(__name__)

CLAIM_LEASE = timedelta(minutes=2)


@dataclass
class MailSummary:
    claimed: int = 0
    sent: int = 0
    retrying: int = 0
    failed: int = 0
    skipped: int = 0
    duration_ms: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def bump(self, name: str) -> None:
        with self._lock:
            setattr(self, name, getattr(self, name) + 1)

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}


def _line(text: str) -> str:
    """One line, for a subject: owner-typed text must not be able to add a header."""
    return " ".join(str(text).split())


def _when(iso: str) -> str:
    if not iso:
        return ""
    return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M UTC")


def _duration(seconds: int) -> str:
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} h {minutes} min"
    return f"{minutes} min" if minutes else f"{secs} s"


def compose(email: Email) -> tuple[str, str]:
    """(subject, body) for a queued email, from the facts frozen when it was queued."""
    p = email.payload
    kind = email.kind
    if kind == Email.Kind.CONFIRM:
        return (
            _line(f"Confirm your subscription to {p['page']}"),
            f"You asked to be emailed when {p['page']} reports a problem.\n\n"
            f"Confirm with this link (it works once):\n{p['confirm_url']}\n\n"
            "If you didn't ask for this, ignore this email and nothing will be sent.\n",
        )
    if kind == Email.Kind.INCIDENT_OPENED:
        return (
            _line(f"[{p['page']}] {p['component']} is down"),
            f"{p['component']} stopped responding at {_when(p['started_at'])}.\n"
            "We'll email you again when it's back.\n\n"
            f"Current status: {p['page_url']}\n",
        )
    if kind == Email.Kind.INCIDENT_RESOLVED:
        return (
            _line(f"[{p['page']}] {p['component']} is back up"),
            f"{p['component']} is working again after {_duration(p['duration_seconds'])}.\n\n"
            f"Current status: {p['page_url']}\n",
        )
    label = "Planned maintenance" if p["kind"] == "maintenance" else "Incident"
    lines = [p["title"], ""]
    if p["body"]:
        lines += [p["body"], ""]
    if p["kind"] == "maintenance" and p["starts_at"]:
        lines += [f"When: {_when(p['starts_at'])} to {_when(p['ends_at'])}", ""]
    if kind == Email.Kind.ANNOUNCEMENT:
        subject = f"[{p['page']}] {label}: {p['title']}"
    else:
        subject = f"[{p['page']}] Resolved: {p['title']}"
        lines = ["This is over.", ""] + lines
    return _line(subject), "\n".join(lines) + f"Current status: {p['page_url']}\n"


def unsubscribe_urls(email: Email) -> tuple[str, str]:
    subscriber = email.subscriber
    token = subscriber.unsubscribe_token
    page = subscriber.page
    return (
        f"{services.page_url(page)}/unsubscribe/{token}",
        f"{settings.API_PUBLIC_URL}/api/v1/public/status-pages/{page.slug}/unsubscribe/{token}/",
    )


def claim_due(now: datetime, limit: int) -> list[int]:
    with transaction.atomic():
        due = list(
            Email.objects.select_for_update(skip_locked=True)
            .filter(status=Email.Status.PENDING, next_attempt_at__lte=now)
            .order_by("next_attempt_at")[:limit]
        )
        for e in due:
            e.attempt_count += 1
            e.next_attempt_at = now + CLAIM_LEASE
            e.save(update_fields=["attempt_count", "next_attempt_at"])
    return [e.id for e in due]


def _skip(email: Email, reason: str) -> str:
    email.status = Email.Status.SKIPPED
    email.next_attempt_at = None
    email.error = reason
    email.save(update_fields=["status", "next_attempt_at", "error"])
    return email.status


def _failed(email: Email, message: str, retryable: bool) -> str:
    email.error = message[:300]
    if not retryable or email.attempt_count >= settings.NOTIFY_MAX_ATTEMPTS:
        email.status = Email.Status.FAILED
        email.next_attempt_at = None
        email.save(update_fields=["status", "next_attempt_at", "error"])
    else:
        backoff = settings.NOTIFY_BACKOFF_SECONDS
        wait = backoff[min(email.attempt_count - 1, len(backoff) - 1)]
        email.next_attempt_at = timezone.now() + timedelta(seconds=wait)
        email.save(update_fields=["next_attempt_at", "error"])
    return email.status


def deliver(email_id: int, *, now: datetime | None = None) -> str:
    now = now or timezone.now()
    email = Email.objects.select_related(
        "subscriber__page", "incident__monitor", "announcement"
    ).get(pk=email_id)
    subscriber = email.subscriber
    page = subscriber.page
    confirm = email.kind == Email.Kind.CONFIRM

    if not confirm and subscriber.confirmed_at is None:
        return _skip(email, "The subscription was never confirmed.")
    if confirm and subscriber.confirmed_at is not None:
        return _skip(email, "Already confirmed.")
    if not page.is_public:
        return _skip(email, "The page is not published.")
    if email.kind == Email.Kind.INCIDENT_OPENED:
        if email.incident and email.incident.ended_at is not None:
            return _skip(email, "It recovered before this was sent.")
        if email.incident and in_maintenance(email.incident.monitor, now):
            return _skip(email, "The monitor is in a maintenance window.")
    if email.kind == Email.Kind.ANNOUNCEMENT and email.announcement:
        if services.announcement_state(email.announcement, now) in ("resolved", "completed"):
            return _skip(email, "It was over before this was sent.")

    subject, body = compose(email)
    message = EmailMessage(
        subject, body, settings.DEFAULT_FROM_EMAIL, [subscriber.email], headers={}
    )
    if not confirm:
        page_link, one_click = unsubscribe_urls(email)
        message.body += f"\n--\nYou get this because you subscribed to {page.name}.\n"
        message.body += f"Unsubscribe: {page_link}\n"
        message.extra_headers.update(
            {
                "List-Unsubscribe": f"<{one_click}>",
                "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
            }
        )
    try:
        message.send()
    except smtplib.SMTPRecipientsRefused:
        return _failed(email, "The mail server refused that address.", False)
    except Exception:  # noqa: BLE001 - a mail error must never lose the row
        logger.exception("status page email %s failed", email.id)
        return _failed(email, "Couldn't send the email.", True)

    email.status = Email.Status.SENT
    email.sent_at = now
    email.next_attempt_at = None
    email.error = ""
    email.save(update_fields=["status", "sent_at", "next_attempt_at", "error"])
    return email.status


def run_status_page_mail_once(
    *, batch_size: int | None = None, workers: int | None = None, now: datetime | None = None
) -> MailSummary:
    began = time.monotonic()
    now = now or timezone.now()
    summary = MailSummary()
    services.purge_stale(now)
    ids = claim_due(now, batch_size or settings.STATUS_PAGE_MAIL_BATCH_SIZE)
    summary.claimed = len(ids)

    def task(email_id: int, in_worker_thread: bool) -> None:
        try:
            status = deliver(email_id, now=now)
            key = {
                Email.Status.SENT: "sent",
                Email.Status.FAILED: "failed",
                Email.Status.SKIPPED: "skipped",
            }.get(status, "retrying")
            summary.bump(key)
        except Exception:  # noqa: BLE001
            logger.exception("sending status page email %s failed", email_id)
        finally:
            if in_worker_thread:
                connections.close_all()

    pool_size = workers or settings.STATUS_PAGE_MAIL_WORKERS
    if pool_size <= 1 or len(ids) <= 1:
        for email_id in ids:
            task(email_id, False)
    else:
        with ThreadPoolExecutor(max_workers=min(pool_size, len(ids))) as pool:
            list(pool.map(lambda i: task(i, True), ids))

    summary.duration_ms = round((time.monotonic() - began) * 1000)
    return summary
