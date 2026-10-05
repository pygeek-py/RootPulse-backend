"""Storing what a feed said, and telling subscribers when it matters.

`ingest` is the only place a feed result touches the database, and it is idempotent: reading the
same feed twice changes nothing and alerts nobody twice. The same function serves the live poller
and `replay_provider_feed` (which feeds it a saved historical response as though it had just
arrived), so a replayed outage is handled by exactly the code that handles a real one.

Who is alerted, and when (`reconcile`):

* An incident that is **ongoing** alerts a subscription's contacts once its impact reaches the
  subscription's minimum, and only if it began recently enough to still be news (a feed's old,
  never-closed notices must not page anyone). Escalation works naturally: a minor incident that
  becomes major alerts subscribers who only wanted major ones, at that moment.
* A **resolved** incident is announced only to contacts who were actually told it started. If the
  start alert is still waiting (a contact's delay), it is cancelled instead.
* Contacts that are off or unconfirmed are left out, as for monitors.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from notifications import messages
from notifications.models import NotificationDelivery as Delivery
from notifications.services import deliverable

from .adapters import FeedResult, ParsedIncident
from .models import IMPACTS, Provider, ProviderIncident, UserProviderSubscription

logger = logging.getLogger(__name__)

SKIP_RESOLVED_FIRST = "It was resolved before this alert was sent."


def max_age() -> timedelta:
    return timedelta(hours=settings.PROVIDER_NOTIFY_MAX_AGE_HOURS)


def rank(impact: str) -> int:
    return IMPACTS.index(impact) if impact in IMPACTS else 1


@dataclass
class IngestSummary:
    created: int = 0
    updated: int = 0
    closed_missing: int = 0
    alerts: int = 0


def _fields_names() -> list[str]:
    return ["title", "impact", "stage", "started_at", "ended_at", "url", "updates", "affected"]


def _fields(parsed: ParsedIncident) -> dict:
    return {
        "title": parsed.title,
        "impact": parsed.impact,
        "stage": parsed.stage,
        "started_at": parsed.started_at,
        "ended_at": parsed.ended_at,
        "url": parsed.url,
        "updates": parsed.updates,
        "affected": parsed.affected,
    }


def ingest(provider: Provider, result: FeedResult, *, now: datetime | None = None) -> IngestSummary:
    """Record a successfully read feed and send whatever alerts it calls for."""
    now = now or timezone.now()
    summary = IngestSummary()

    provider.status = result.status
    provider.status_description = result.description
    provider.components = result.components
    provider.component_count = result.component_count
    provider.status_checked_at = now
    provider.last_success_at = now
    provider.consecutive_failures = 0
    provider.last_error = ""
    provider.save(
        update_fields=[
            "status",
            "status_description",
            "components",
            "component_count",
            "status_checked_at",
            "last_success_at",
            "consecutive_failures",
            "last_error",
        ]
    )

    ids = [i.external_id for i in result.incidents]
    existing = {
        i.external_id: i
        for i in ProviderIncident.objects.filter(provider=provider, external_id__in=ids)
    }
    # One write for all the new incidents and one for all the changed ones: the database may be
    # far away, and a first read of a provider brings in its whole recent history (about fifty).
    to_create, to_update = [], []
    for parsed in result.incidents:
        current = existing.get(parsed.external_id)
        values = _fields(parsed)
        if current is None:
            to_create.append(
                ProviderIncident(
                    provider=provider, external_id=parsed.external_id, first_seen_at=now, **values
                )
            )
        elif any(getattr(current, name) != value for name, value in values.items()):
            for name, value in values.items():
                setattr(current, name, value)
            current.updated_at = now
            to_update.append(current)
    if to_create:
        # ignore_conflicts: a concurrent poll may have stored the same incident first.
        ProviderIncident.objects.bulk_create(to_create, ignore_conflicts=True)
        summary.created = len(to_create)
    if to_update:
        ProviderIncident.objects.bulk_update(to_update, [*_fields_names(), "updated_at"])
        summary.updated = len(to_update)

    # An incident we think is still open that the feed no longer lists has been taken down.
    if ids or result.status == "operational":
        for gone in ProviderIncident.objects.filter(
            provider=provider, ended_at__isnull=True
        ).exclude(external_id__in=ids):
            gone.ended_at = max(now, gone.started_at)
            gone.stage = ProviderIncident.Stage.RESOLVED
            gone.save(update_fields=["ended_at", "stage", "updated_at"])
            summary.closed_missing += 1

    summary.alerts = reconcile(provider, now=now)
    return summary


def record_failure(provider: Provider, error: str, *, now: datetime | None = None) -> None:
    now = now or timezone.now()
    provider.consecutive_failures += 1
    provider.status_checked_at = now
    provider.last_error = error[:200]
    # A feed we can't read is "unknown", never "operational": silence isn't good news.
    if provider.consecutive_failures >= settings.PROVIDER_UNKNOWN_AFTER_FAILURES:
        provider.status = Provider.Status.UNKNOWN
        provider.status_description = "We can't reach this status page right now."
    provider.save(
        update_fields=[
            "consecutive_failures",
            "status_checked_at",
            "last_error",
            "status",
            "status_description",
        ]
    )


# --- alerts ----------------------------------------------------------------------------


def _create(sub, incident, contact, event: str, due: datetime, now: datetime, skip: str = ""):
    try:
        with transaction.atomic():
            return Delivery.objects.create(
                provider_incident=incident,
                contact=contact,
                contact_name=contact.name,
                channel=contact.channel,
                event=event,
                dedupe_key=event,
                status=Delivery.Status.SKIPPED if skip else Delivery.Status.PENDING,
                next_attempt_at=None if skip else due,
                error=skip,
                payload=messages.build_provider(event, incident, now=now),
            )
    except IntegrityError:
        return None  # already created: a repeated pass


def reconcile(
    provider: Provider, *, now: datetime | None = None, only: UserProviderSubscription | None = None
) -> int:
    """Create whatever alerts the provider's current incidents call for. Returns how many."""
    now = now or timezone.now()
    subs = (
        [only]
        if only
        else list(provider.subscriptions.select_related("user").prefetch_related("alert_contacts"))
    )
    if not subs:
        return 0
    incidents = list(
        ProviderIncident.objects.filter(provider=provider)
        .filter(Q(ended_at__isnull=True) | Q(ended_at__gte=now - max_age()))
        .select_related("provider")
    )
    created = 0
    for sub in subs:
        contacts = [c for c in sub.alert_contacts.all() if deliverable(c)]
        for incident in incidents:
            created += _reconcile_one(sub, incident, contacts, now)
    return created


def _reconcile_one(sub, incident: ProviderIncident, contacts, now: datetime) -> int:
    created = 0
    if incident.ended_at is None:
        if now - incident.started_at > max_age() or rank(incident.impact) < rank(sub.min_impact):
            return 0
        for contact in contacts:
            due = now + timedelta(seconds=contact.delay_seconds)
            if _create(sub, incident, contact, "opened", due, now):
                created += 1
        return created

    # Resolved: tell only those who were told it began.
    opened = {
        d.contact_id: d
        for d in Delivery.objects.filter(provider_incident=incident, dedupe_key="opened")
    }
    for contact in contacts:
        sent = opened.get(contact.id)
        if sent is None:
            continue
        if sent.status == Delivery.Status.PENDING:
            sent.status = Delivery.Status.SKIPPED
            sent.next_attempt_at = None
            sent.error = SKIP_RESOLVED_FIRST
            sent.save(update_fields=["status", "next_attempt_at", "error"])
        elif sent.status == Delivery.Status.SENT and _create(
            sub, incident, contact, "resolved", now, now
        ):
            created += 1
    return created


def cancel_pending(sub: UserProviderSubscription) -> int:
    """The user stopped tracking this provider: drop alerts still waiting."""
    return Delivery.objects.filter(
        provider_incident__provider=sub.provider,
        contact__user=sub.user,
        status=Delivery.Status.PENDING,
    ).update(
        status=Delivery.Status.SKIPPED,
        next_attempt_at=None,
        error="You stopped tracking this provider.",
    )
