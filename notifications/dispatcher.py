"""Sends the queued alerts: one pass claims what is due, delivers it, and returns.

Same shape as the scheduler (monitoring/engine.py): the table is the queue,
`select_for_update(skip_locked=True)` stops overlapping passes claiming the same row, and
network calls happen outside any lock.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.conf import settings
from django.db import connections, transaction
from django.utils import timezone

from incidents.models import IncidentEvent
from monitoring.models import AlertContact

from . import services
from .channels import CHANNELS
from .http import DeliveryError
from .models import NotificationDelivery as Delivery

logger = logging.getLogger(__name__)

# How long a claimed delivery is left alone. If the process dies mid-send, it becomes
# due again after this (and is retried), rather than being stuck forever.
CLAIM_LEASE = timedelta(minutes=2)


@dataclass
class DispatchSummary:
    reminders_created: int = 0
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


def claim_due(now: datetime, limit: int) -> list[int]:
    with transaction.atomic():
        due = list(
            Delivery.objects.select_for_update(skip_locked=True)
            .filter(status=Delivery.Status.PENDING, next_attempt_at__lte=now)
            .order_by("next_attempt_at")[:limit]
        )
        for delivery in due:
            delivery.attempt_count += 1
            delivery.next_attempt_at = now + CLAIM_LEASE
            delivery.save(update_fields=["attempt_count", "next_attempt_at"])
    return [d.id for d in due]


def _record(delivery: Delivery, kind: str, **extra) -> None:
    """Write the outcome onto the incident's timeline."""
    if delivery.incident_id is None or delivery.event == Delivery.Event.TEST:
        return
    IncidentEvent.objects.create(
        incident_id=delivery.incident_id,
        kind=kind,
        created_at=timezone.now(),
        metadata={
            "channel": delivery.channel,
            "contact": delivery.contact_name,
            "event": delivery.event,
            **extra,
        },
    )


def _skip(delivery: Delivery, reason: str) -> str:
    delivery.status = Delivery.Status.SKIPPED
    delivery.next_attempt_at = None
    delivery.error = reason
    delivery.save(update_fields=["status", "next_attempt_at", "error"])
    return delivery.status


def deliver(delivery_id: int, *, now: datetime | None = None) -> str:
    """Attempt one claimed delivery. Returns its resulting status."""
    now = now or timezone.now()
    delivery = Delivery.objects.select_related(
        "contact", "incident", "monitor", "provider_incident__provider"
    ).get(pk=delivery_id)
    contact = delivery.contact

    if contact is None:
        return _skip(delivery, "The contact was deleted.")
    if not services.deliverable(contact):
        return _skip(delivery, "The contact is switched off or not verified.")
    if delivery.event in (Delivery.Event.OPENED, Delivery.Event.REMINDER):
        if delivery.incident and delivery.incident.ended_at is not None:
            return _skip(delivery, "It recovered before this alert was sent.")
        if delivery.monitor and services.in_maintenance(delivery.monitor, now):
            return _skip(delivery, services.IN_MAINTENANCE)

    if delivery.provider_incident_id and delivery.event == Delivery.Event.OPENED:
        if delivery.provider_incident.ended_at is not None:
            return _skip(delivery, "It was resolved before this alert was sent.")

    channel = CHANNELS[delivery.channel]
    try:
        channel.send(contact.config, delivery.payload, delivery.id)
    except DeliveryError as exc:
        return _failed(
            delivery,
            contact,
            exc.message,
            exc.retryable,
            exc.retry_after,
            disable=getattr(exc, "disable_contact", False),
        )
    except Exception:  # noqa: BLE001 - a bug in an adapter must never lose the alert
        logger.exception("notification %s crashed", delivery.id)
        return _failed(delivery, contact, "Unexpected error while sending.", True, None)

    delivery.status = Delivery.Status.SENT
    delivery.sent_at = now
    delivery.next_attempt_at = None
    delivery.error = ""
    delivery.save(update_fields=["status", "sent_at", "next_attempt_at", "error"])
    _record(delivery, IncidentEvent.Kind.NOTIFICATION_SENT)
    return delivery.status


def _failed(delivery, contact, message, retryable, retry_after, *, disable=False) -> str:
    delivery.error = message[:300]
    final = (not retryable) or delivery.attempt_count >= settings.NOTIFY_MAX_ATTEMPTS
    if final:
        delivery.status = Delivery.Status.FAILED
        delivery.next_attempt_at = None
        delivery.save(update_fields=["status", "next_attempt_at", "error"])
        _record(delivery, IncidentEvent.Kind.NOTIFICATION_FAILED, error=delivery.error)
        if disable and contact is not None:
            AlertContact.objects.filter(pk=contact.pk).update(enabled=False)
    else:
        backoff = settings.NOTIFY_BACKOFF_SECONDS
        wait = retry_after or backoff[min(delivery.attempt_count - 1, len(backoff) - 1)]
        delivery.next_attempt_at = timezone.now() + timedelta(seconds=wait)
        delivery.save(update_fields=["next_attempt_at", "error"])
    return delivery.status


def run_notifications_once(
    *, batch_size: int | None = None, workers: int | None = None, now: datetime | None = None
) -> DispatchSummary:
    began = time.monotonic()
    now = now or timezone.now()
    summary = DispatchSummary()
    summary.reminders_created = services.schedule_reminders(now=now)

    ids = claim_due(now, batch_size or settings.NOTIFY_BATCH_SIZE)
    summary.claimed = len(ids)

    def task(delivery_id: int, in_worker_thread: bool) -> None:
        try:
            status = deliver(delivery_id, now=now)
            key = {
                Delivery.Status.SENT: "sent",
                Delivery.Status.FAILED: "failed",
                Delivery.Status.SKIPPED: "skipped",
            }.get(status, "retrying")
            summary.bump(key)
        except Exception:  # noqa: BLE001
            logger.exception("delivering %s failed", delivery_id)
        finally:
            if in_worker_thread:
                connections.close_all()

    pool_size = workers or settings.NOTIFY_WORKERS
    if pool_size <= 1 or len(ids) <= 1:
        for delivery_id in ids:
            task(delivery_id, False)
    else:
        with ThreadPoolExecutor(max_workers=min(pool_size, len(ids))) as pool:
            list(pool.map(lambda i: task(i, True), ids))

    summary.duration_ms = round((time.monotonic() - began) * 1000)
    return summary
