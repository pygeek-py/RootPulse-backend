"""When an alert gets created. (Sending is the dispatcher's job.)

The rules, in one place:

* An incident **opening** alerts every enabled, verified contact attached to the monitor,
  after that contact's `delay_seconds` (and only if the monitor is *still* down then).
* **Recovery** is announced only to contacts who were actually told it went down. If the
  down-alert hasn't gone out yet (the delay hadn't elapsed, or it's retrying), it is
  cancelled instead, so nobody gets an "all clear" for an alarm they never heard.
* While an incident stays open, contacts with a `repeat_interval_seconds` are reminded,
  at most `NOTIFY_MAX_REMINDERS` times.
* Nothing is sent about a monitor that's in a **maintenance window**.
* Creation is idempotent (`dedupe_key` + a unique constraint), so a retried pass can
  never alert twice for the same thing.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from incidents.models import Incident
from monitoring.models import AlertContact, MaintenanceWindow, Monitor

from . import messages
from .models import NotificationDelivery as Delivery

logger = logging.getLogger(__name__)

IN_MAINTENANCE = "The monitor is in a maintenance window."


def in_maintenance(monitor: Monitor, now: datetime) -> bool:
    return MaintenanceWindow.objects.filter(
        starts_at__lte=now, ends_at__gt=now, monitors=monitor
    ).exists()


def deliverable(contact: AlertContact) -> bool:
    """Enabled, and (for email) proven to belong to the user."""
    if not contact.enabled:
        return False
    return contact.channel != AlertContact.Channel.EMAIL or contact.verified_at is not None


def attached_contacts(monitor: Monitor) -> list[AlertContact]:
    return [c for c in monitor.alert_contacts.all() if deliverable(c)]


def _create(
    *,
    incident: Incident | None,
    monitor: Monitor,
    contact: AlertContact,
    event: str,
    dedupe_key: str,
    payload: dict,
    due: datetime,
    skip_reason: str = "",
) -> Delivery | None:
    try:
        with transaction.atomic():
            return Delivery.objects.create(
                incident=incident,
                monitor=monitor,
                contact=contact,
                contact_name=contact.name,
                channel=contact.channel,
                event=event,
                dedupe_key=dedupe_key,
                status=Delivery.Status.SKIPPED if skip_reason else Delivery.Status.PENDING,
                next_attempt_at=None if skip_reason else due,
                error=skip_reason,
                payload=payload,
            )
    except IntegrityError:
        return None  # already created: a retried pass


def schedule_opened(incident: Incident, *, now: datetime | None = None) -> list[Delivery]:
    now = now or timezone.now()
    monitor = incident.monitor
    payload = messages.build("opened", monitor, incident, now=now)
    muted = in_maintenance(monitor, now)
    made = []
    for contact in attached_contacts(monitor):
        delivery = _create(
            incident=incident,
            monitor=monitor,
            contact=contact,
            event="opened",
            dedupe_key="opened",
            payload=payload,
            due=now + timedelta(seconds=contact.delay_seconds),
            skip_reason=IN_MAINTENANCE if muted else "",
        )
        if delivery:
            made.append(delivery)
    return made


def schedule_resolved(incident: Incident, *, now: datetime | None = None) -> list[Delivery]:
    now = now or timezone.now()
    monitor = incident.monitor
    payload = messages.build("resolved", monitor, incident, now=incident.ended_at or now)
    made = []
    for opened in Delivery.objects.filter(incident=incident, dedupe_key="opened").select_related(
        "contact"
    ):
        if opened.status == Delivery.Status.PENDING:
            # They were never told it went down, so don't tell them it's fixed.
            opened.status = Delivery.Status.SKIPPED
            opened.next_attempt_at = None
            opened.error = "It recovered before this alert was sent."
            opened.save(update_fields=["status", "next_attempt_at", "error"])
        elif (
            opened.status == Delivery.Status.SENT and opened.contact and deliverable(opened.contact)
        ):
            delivery = _create(
                incident=incident,
                monitor=monitor,
                contact=opened.contact,
                event="resolved",
                dedupe_key="resolved",
                payload=payload,
                due=now,
            )
            if delivery:
                made.append(delivery)
    return made


def cancel_pending(incident: Incident, reason: str) -> int:
    """The incident was closed without recovering (paused, edited): drop alerts not yet sent."""
    return Delivery.objects.filter(incident=incident, status=Delivery.Status.PENDING).update(
        status=Delivery.Status.SKIPPED, next_attempt_at=None, error=reason
    )


def schedule_reminders(*, now: datetime | None = None) -> int:
    """Create a reminder for every open incident whose contact's repeat interval has elapsed."""
    now = now or timezone.now()
    created = 0
    open_incidents = Incident.objects.filter(ended_at__isnull=True).select_related("monitor")
    for incident in open_incidents:
        monitor = incident.monitor
        if in_maintenance(monitor, now):
            continue
        for contact in attached_contacts(monitor):
            if contact.repeat_interval_seconds <= 0:
                continue
            sent = list(
                Delivery.objects.filter(
                    incident=incident,
                    contact=contact,
                    status=Delivery.Status.SENT,
                    event__in=["opened", "reminder"],
                ).order_by("-sent_at")
            )
            if not sent:  # never told them it went down: nothing to remind about
                continue
            reminders = sum(1 for d in sent if d.event == "reminder")
            if reminders >= settings.NOTIFY_MAX_REMINDERS:
                continue
            if (
                sent[0].sent_at
                and sent[0].sent_at + timedelta(seconds=contact.repeat_interval_seconds) > now
            ):
                continue
            if Delivery.objects.filter(
                incident=incident, contact=contact, status=Delivery.Status.PENDING
            ).exists():
                continue  # one is already on its way
            payload = messages.build("reminder", monitor, incident, now=now)
            if _create(
                incident=incident,
                monitor=monitor,
                contact=contact,
                event="reminder",
                dedupe_key=f"reminder:{reminders + 1}",
                payload=payload,
                due=now,
            ):
                created += 1
    return created


def create_test(contact: AlertContact, monitor: Monitor | None = None) -> Delivery:
    """A test alert, built to be sent immediately by the caller."""
    now = timezone.now()
    if monitor is None:
        monitor = Monitor(
            name="your monitor", type="http", target="https://example.com", id=contact.id
        )
        payload = messages.build("test", monitor, None, now=now)
        payload["url"] = settings.FRONTEND_URL.rstrip("/") + "/settings/notifications"
        payload["title"] = "Test alert from RootPulse"
        monitor_ref = None
    else:
        payload = messages.build("test", monitor, None, now=now)
        monitor_ref = monitor
    return Delivery.objects.create(
        monitor=monitor_ref,
        contact=contact,
        contact_name=contact.name,
        channel=contact.channel,
        event="test",
        dedupe_key=f"test:{now.timestamp():.0f}",
        status=Delivery.Status.PENDING,
        next_attempt_at=now,
        payload=payload,
    )
