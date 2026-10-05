"""What a status page shows, who may see it, and who is emailed.

The public payload (`build_public`) is the one place that decides what leaves the building. It is
assembled from scratch field by field, never by serialising a model, so a field added to a
monitor or an incident tomorrow cannot leak onto a public page by accident.
"""

from __future__ import annotations

import hashlib
import logging
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any

from django.contrib.auth.hashers import check_password, make_password
from django.core import signing
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from analytics.models import CheckRollupDaily
from analytics.queries import _uptime as uptime_percent
from incidents.models import Incident, IncidentComment
from monitoring.models import MaintenanceWindow

from .models import (
    StatusPage,
    StatusPageAnnouncement,
    StatusPageComponent,
    StatusPageEmail,
    StatusPageSubscriber,
)

logger = logging.getLogger(__name__)

UPTIME_DAYS = 90
RECENT_DAYS = 14  # how far back "past incidents" and "past announcements" go
MAX_RECENT_INCIDENTS = 30
MAX_COMPONENTS = 50
MAX_SUBSCRIBERS = 1000
MAX_UNCONFIRMED = 200
CONFIRM_COOLDOWN = timedelta(minutes=10)  # per address: one confirmation email this often
UNCONFIRMED_TTL = timedelta(days=7)
UNLOCK_MAX_AGE = 12 * 3600
UNLOCK_SALT = "statuspages.unlock"
UNLOCK_HEADER = "HTTP_X_STATUS_PAGE_TOKEN"

BRANDING_DEFAULTS = {"accent": "", "logo_url": "", "description": "", "support_url": ""}


# --- passwords ----------------------------------------------------------------------------


def set_password(page: StatusPage, raw: str | None) -> None:
    page.password_hash = make_password(raw) if raw else ""


def password_matches(page: StatusPage, raw: str) -> bool:
    return bool(page.password_hash) and check_password(raw, page.password_hash)


def _fingerprint(page: StatusPage) -> str:
    # Changing the password changes this, which retires every token issued before.
    return hashlib.sha256(page.password_hash.encode()).hexdigest()[:16]


def make_unlock_token(page: StatusPage) -> str:
    return signing.dumps({"p": str(page.id), "f": _fingerprint(page)}, salt=UNLOCK_SALT)


def unlock_token_ok(page: StatusPage, token: str | None) -> bool:
    if not token:
        return False
    try:
        data = signing.loads(token, salt=UNLOCK_SALT, max_age=UNLOCK_MAX_AGE)
    except signing.BadSignature:
        return False
    return data.get("p") == str(page.id) and data.get("f") == _fingerprint(page)


def may_view(page: StatusPage, token: str | None) -> bool:
    return not page.has_password or unlock_token_ok(page, token)


# --- the public payload -------------------------------------------------------------------


def branding_of(page: StatusPage) -> dict[str, str]:
    stored = page.branding if isinstance(page.branding, dict) else {}
    return {key: str(stored.get(key, default)) for key, default in BRANDING_DEFAULTS.items()}


def announcement_state(a: StatusPageAnnouncement, now: datetime) -> str:
    if a.kind == StatusPageAnnouncement.Kind.INCIDENT:
        return "resolved" if a.resolved_at else "active"
    if a.resolved_at or (a.ends_at and a.ends_at <= now):
        return "completed"
    if a.starts_at and a.starts_at > now:
        return "upcoming"
    return "in_progress"


def _announcement_out(a: StatusPageAnnouncement, now: datetime) -> dict[str, Any]:
    return {
        "id": str(a.id),
        "kind": a.kind,
        "impact": a.impact,
        "title": a.title,
        "body": a.body,
        "state": announcement_state(a, now),
        "starts_at": a.starts_at,
        "ends_at": a.ends_at,
        "resolved_at": a.resolved_at,
        "created_at": a.created_at,
    }


def _announcements(page: StatusPage, now: datetime) -> dict[str, list]:
    horizon = now - timedelta(days=RECENT_DAYS)
    rows = StatusPageAnnouncement.objects.filter(page=page).filter(
        Q(created_at__gte=horizon) | Q(resolved_at__isnull=True)
    )[:100]
    active, past = [], []
    for a in rows:
        out = _announcement_out(a, now)
        if out["state"] in ("active", "upcoming", "in_progress"):
            active.append(out)
        else:
            finished = a.resolved_at or a.ends_at or a.created_at
            if finished >= horizon:
                past.append((finished, out))
    active.sort(key=lambda x: x["created_at"], reverse=True)
    past.sort(key=lambda pair: pair[0], reverse=True)
    return {"active": active, "past": [out for _, out in past]}


def overall_status(statuses: list[str]) -> str:
    counted = [s for s in statuses if s in ("operational", "major_outage", "maintenance")]
    if not counted:
        return "unknown"
    down = counted.count("major_outage")
    if down == 0:
        return "maintenance" if "maintenance" in counted else "operational"
    return "major_outage" if down == len(counted) else "partial_outage"


def _component_status(monitor_status: str, monitor_id, maintenance_ids: set) -> str:
    if monitor_status == "paused":
        return "paused"
    if monitor_id in maintenance_ids:
        return "maintenance"
    return {"up": "operational", "down": "major_outage"}.get(monitor_status, "unknown")


def build_public(page: StatusPage, *, now: datetime | None = None) -> dict[str, Any]:
    """Everything the public page needs, in a fixed number of queries however big the page is."""
    now = now or timezone.now()
    components = list(
        StatusPageComponent.objects.filter(page=page).select_related("monitor").order_by("order")
    )
    monitor_ids = [c.monitor_id for c in components]

    maintenance_ids = set(
        MaintenanceWindow.objects.filter(
            starts_at__lte=now, ends_at__gt=now, monitors__in=monitor_ids
        ).values_list("monitors", flat=True)
    )

    today = now.astimezone(UTC).date()
    first_day = today - timedelta(days=UPTIME_DAYS - 1)
    by_monitor: dict[Any, dict[date, tuple[int, int, int]]] = defaultdict(dict)
    for monitor_id, day, up, down, excluded in CheckRollupDaily.objects.filter(
        monitor_id__in=monitor_ids, day__gte=first_day, day__lte=today
    ).values_list("monitor_id", "day", "up", "down", "down_excluded"):
        by_monitor[monitor_id][day] = (up, down, excluded)

    days = [first_day + timedelta(days=i) for i in range(UPTIME_DAYS)]
    out_components = []
    for c in components:
        figures = by_monitor.get(c.monitor_id, {})
        total = [sum(v[i] for v in figures.values()) for i in range(3)]
        out_components.append(
            {
                "id": str(c.id),
                "name": c.display_name,
                "group": c.group,
                "status": _component_status(c.monitor.status, c.monitor_id, maintenance_ids),
                "uptime_percent": uptime_percent(*total),
                "days": [
                    {
                        "date": d.isoformat(),
                        "uptime_percent": uptime_percent(*figures[d]) if d in figures else None,
                    }
                    for d in days
                ],
            }
        )

    names = {c.monitor_id: c.display_name for c in components}
    horizon = now - timedelta(days=RECENT_DAYS)
    incidents = list(
        Incident.objects.filter(monitor_id__in=monitor_ids, excluded_from_reports=False)
        .filter(Q(ended_at__isnull=True) | Q(ended_at__gte=horizon))
        .order_by("-started_at")[:MAX_RECENT_INCIDENTS]
    )
    notes: dict[Any, list[IncidentComment]] = defaultdict(list)
    for comment in IncidentComment.objects.filter(
        incident__in=incidents, visible_on_status_page=True
    ).order_by("created_at"):
        notes[comment.incident_id].append(comment)
    out_incidents = [
        {
            "id": str(i.id),
            "component": names[i.monitor_id],
            "started_at": i.started_at,
            "ended_at": i.ended_at,
            "ongoing": i.ended_at is None,
            "updates": [{"id": str(n.id), "body": n.body, "at": n.created_at} for n in notes[i.id]],
        }
        for i in incidents
    ]

    return {
        "name": page.name,
        "slug": page.slug,
        "branding": branding_of(page),
        "password_required": False,
        "generated_at": now,
        "overall": {"status": overall_status([c["status"] for c in out_components])},
        "components": out_components,
        "announcements": _announcements(page, now),
        "incidents": out_incidents,
        "uptime_days": UPTIME_DAYS,
    }


def locked_payload(page: StatusPage) -> dict[str, Any]:
    """What a visitor to a password-protected page sees before unlocking it: the name and the
    look, and nothing about the services."""
    return {
        "name": page.name,
        "slug": page.slug,
        "branding": branding_of(page),
        "password_required": True,
    }


# --- subscribers --------------------------------------------------------------------------


def page_url(page: StatusPage) -> str:
    from django.conf import settings

    return f"{settings.FRONTEND_URL}/s/{page.slug}"


def subscribe(page: StatusPage, email: str, *, now: datetime | None = None) -> None:
    """Start (or restart) the double opt-in. Says nothing about whether the address was already
    known, so the form can't be used to find out who subscribes."""
    now = now or timezone.now()
    email = email.strip().lower()
    existing = StatusPageSubscriber.objects.filter(page=page, email=email).first()
    if existing is not None:
        if existing.confirmed_at is not None:
            return
        if existing.last_confirmation_at and now - existing.last_confirmation_at < CONFIRM_COOLDOWN:
            return
        queue_confirmation(existing, now)
        return

    subscribers = StatusPageSubscriber.objects.filter(page=page)
    if subscribers.count() >= MAX_SUBSCRIBERS:
        return
    if subscribers.filter(confirmed_at__isnull=True).count() >= MAX_UNCONFIRMED:
        return
    try:
        with transaction.atomic():
            subscriber = StatusPageSubscriber.objects.create(page=page, email=email)
    except IntegrityError:  # two requests at once
        return
    queue_confirmation(subscriber, now)


def queue_confirmation(subscriber: StatusPageSubscriber, now: datetime) -> None:
    subscriber.confirmations_sent += 1
    subscriber.last_confirmation_at = now
    subscriber.save(update_fields=["confirmations_sent", "last_confirmation_at"])
    page = subscriber.page
    StatusPageEmail.objects.get_or_create(
        subscriber=subscriber,
        dedupe_key=f"confirm:{subscriber.confirmations_sent}",
        defaults={
            "kind": StatusPageEmail.Kind.CONFIRM,
            "next_attempt_at": now,
            "payload": {
                "page": page.name,
                "confirm_url": f"{page_url(page)}/confirm/{subscriber.confirm_token}",
            },
        },
    )


def confirm(token: str, *, now: datetime | None = None) -> StatusPageSubscriber | None:
    """Redeem a confirmation link. None if it is unknown, used or too old."""
    now = now or timezone.now()
    if not token:
        return None
    subscriber = (
        StatusPageSubscriber.objects.filter(confirm_token=token, confirmed_at__isnull=True)
        .select_related("page")
        .first()
    )
    if subscriber is None or now - subscriber.created_at > UNCONFIRMED_TTL:
        return None
    subscriber.confirmed_at = now
    subscriber.confirm_token = ""
    subscriber.save(update_fields=["confirmed_at", "confirm_token"])
    return subscriber


def subscriber_for_unsubscribe(token: str) -> StatusPageSubscriber | None:
    if not token:
        return None
    return (
        StatusPageSubscriber.objects.filter(unsubscribe_token=token).select_related("page").first()
    )


def purge_stale(now: datetime) -> None:
    StatusPageSubscriber.objects.filter(
        confirmed_at__isnull=True, created_at__lt=now - UNCONFIRMED_TTL
    ).delete()
    StatusPageEmail.objects.filter(
        status__in=[StatusPageEmail.Status.SENT, StatusPageEmail.Status.SKIPPED],
        created_at__lt=now - timedelta(days=30),
    ).delete()


def masked(email: str) -> str:
    name, _, domain = email.partition("@")
    return f"{name[:1]}{'*' * max(1, len(name) - 1)}@{domain}"


# --- who gets told what -------------------------------------------------------------------


def _fan_out(subscribers, *, kind, dedupe_key, payload, now, incident=None, announcement=None):
    StatusPageEmail.objects.bulk_create(
        [
            StatusPageEmail(
                subscriber=s,
                kind=kind,
                incident=incident,
                announcement=announcement,
                dedupe_key=dedupe_key,
                next_attempt_at=now,
                payload=payload,
            )
            for s in subscribers
        ],
        ignore_conflicts=True,
    )


def _confirmed(page: StatusPage):
    return list(StatusPageSubscriber.objects.filter(page=page, confirmed_at__isnull=False))


def _follow_up(*, first_kind, filters: dict, resolved_kind, key, payload, now, **links):
    """Tell people it's over, but only those who were told it began. If the first email hasn't
    gone out yet, cancel it instead, so nobody gets an all-clear for an alarm they never heard."""
    first = list(StatusPageEmail.objects.filter(**filters, kind=first_kind))
    sent = [e.subscriber_id for e in first if e.status == StatusPageEmail.Status.SENT]
    for e in first:
        if e.status == StatusPageEmail.Status.PENDING:
            e.status = StatusPageEmail.Status.SKIPPED
            e.next_attempt_at = None
            e.error = "It was over before this was sent."
            e.save(update_fields=["status", "next_attempt_at", "error"])
    if not sent:
        return
    subscribers = StatusPageSubscriber.objects.filter(pk__in=sent)
    _fan_out(subscribers, kind=resolved_kind, dedupe_key=key, payload=payload, now=now, **links)


def on_incident_opened(incident: Incident, *, now: datetime | None = None) -> None:
    """A monitor went down: email the confirmed subscribers of every public page showing it."""
    now = now or timezone.now()
    for component in StatusPageComponent.objects.filter(
        monitor_id=incident.monitor_id, page__is_public=True
    ).select_related("page"):
        page = component.page
        _fan_out(
            _confirmed(page),
            kind=StatusPageEmail.Kind.INCIDENT_OPENED,
            dedupe_key=f"incident:{incident.id}:opened",
            incident=incident,
            now=now,
            payload={
                "page": page.name,
                "page_url": page_url(page),
                "component": component.display_name,
                "started_at": incident.started_at.isoformat(),
            },
        )


def on_incident_closed(incident: Incident, *, now: datetime | None = None) -> None:
    now = now or timezone.now()
    for component in StatusPageComponent.objects.filter(
        monitor_id=incident.monitor_id, page__is_public=True
    ).select_related("page"):
        page = component.page
        duration = max(0, round(((incident.ended_at or now) - incident.started_at).total_seconds()))
        _follow_up(
            first_kind=StatusPageEmail.Kind.INCIDENT_OPENED,
            filters={"incident": incident, "subscriber__page": page},
            resolved_kind=StatusPageEmail.Kind.INCIDENT_RESOLVED,
            key=f"incident:{incident.id}:resolved",
            incident=incident,
            now=now,
            payload={
                "page": page.name,
                "page_url": page_url(page),
                "component": component.display_name,
                "started_at": incident.started_at.isoformat(),
                "duration_seconds": duration,
            },
        )


def _announcement_payload(a: StatusPageAnnouncement) -> dict[str, Any]:
    return {
        "page": a.page.name,
        "page_url": page_url(a.page),
        "kind": a.kind,
        "title": a.title,
        "body": a.body,
        "starts_at": a.starts_at.isoformat() if a.starts_at else "",
        "ends_at": a.ends_at.isoformat() if a.ends_at else "",
    }


def announce(announcement: StatusPageAnnouncement, *, now: datetime | None = None) -> None:
    now = now or timezone.now()
    page = announcement.page
    if not page.is_public:
        return
    _fan_out(
        _confirmed(page),
        kind=StatusPageEmail.Kind.ANNOUNCEMENT,
        dedupe_key=f"announcement:{announcement.id}:posted",
        announcement=announcement,
        now=now,
        payload=_announcement_payload(announcement),
    )


def announcement_resolved(
    announcement: StatusPageAnnouncement, *, now: datetime | None = None
) -> None:
    now = now or timezone.now()
    _follow_up(
        first_kind=StatusPageEmail.Kind.ANNOUNCEMENT,
        filters={"announcement": announcement},
        resolved_kind=StatusPageEmail.Kind.ANNOUNCEMENT_RESOLVED,
        key=f"announcement:{announcement.id}:resolved",
        announcement=announcement,
        now=now,
        payload=_announcement_payload(announcement),
    )
