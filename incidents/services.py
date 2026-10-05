"""The incident lifecycle (docs/plan/03-monitoring-engine.md sections 10 and 11).

The engine only calls `reconcile` after a check; everything else here is a rule about
when an incident exists. The invariant: **a monitor is down exactly when it has one
open incident.** `reconcile` enforces it from both directions, so a missed event (a
crash between steps, or data from before incidents existed) heals on the next check.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Iterable

from django.db import IntegrityError, transaction
from django.utils import timezone

from monitoring.models import Check, Monitor

from .models import Incident, IncidentEvent

logger = logging.getLogger(__name__)


def open_incident(
    monitor: Monitor, check: Check, confirmations: Iterable[Check] = ()
) -> Incident | None:
    """Open the incident for a confirmed failure. Returns None if one is already open."""
    confirmations = list(confirmations)
    try:
        with transaction.atomic():
            incident = Incident.objects.create(
                monitor=monitor,
                started_at=check.started_at,
                reason=check.status_detail,
                status_code=check.status_code,
                opened_by=check,
            )
    except IntegrityError:
        # The "one open incident per monitor" constraint: someone (another pass, a retry)
        # already opened it. That's the point of the constraint; nothing to do.
        return None

    IncidentEvent.objects.create(
        incident=incident,
        kind=IncidentEvent.Kind.OPENED,
        created_at=check.started_at,
        metadata={
            "reason": check.status_detail,
            "status_code": check.status_code,
            "region": check.region,
            "check_id": check.id,
        },
    )
    # Say what it likely followed (a deploy) and where it went wrong (a request stage), before
    # anyone is told, so the alert can include both. Best effort: never at the cost of the incident.
    try:
        with transaction.atomic():
            from deploys import rootcause
            from deploys import services as deploy_services

            deploy_services.correlate_incident(incident)
            rootcause.compute_for_incident(incident, check)
    except Exception:  # noqa: BLE001
        logger.exception("couldn't explain incident %s", incident.id)

    # Tell the monitor's contacts (after each one's configured delay). Same transaction as
    # the incident, so there is never an incident nobody was queued to hear about.
    from notifications import services as notify

    notify.schedule_opened(incident)
    if confirmations:
        regions = sorted({c.region for c in confirmations} | {check.region})
        failed = sorted({check.region} | {c.region for c in confirmations if c.success is False})
        IncidentEvent.objects.create(
            incident=incident,
            kind=IncidentEvent.Kind.CONFIRMED,
            created_at=timezone.now(),
            metadata={"regions_checked": regions, "regions_failing": failed},
        )
    return incident


def close_incident(
    monitor: Monitor,
    *,
    kind: str,
    resolution: str,
    at: datetime | None = None,
    check: Check | None = None,
    metadata: dict | None = None,
) -> Incident | None:
    """Close the monitor's open incident, if it has one."""
    incident = Incident.objects.filter(monitor=monitor, ended_at__isnull=True).first()
    if incident is None:
        return None
    ended = max(at or timezone.now(), incident.started_at)  # never before it started
    incident.ended_at = ended
    incident.resolution = resolution
    incident.resolved_by = check
    incident.save(update_fields=["ended_at", "resolution", "resolved_by"])
    IncidentEvent.objects.create(
        incident=incident,
        kind=kind,
        created_at=ended,
        metadata={
            "duration_seconds": round((ended - incident.started_at).total_seconds()),
            **(metadata or {}),
        },
    )
    return incident


def reconcile(
    monitor: Monitor, new_status: str, check: Check, confirmations: Iterable[Check] = ()
) -> None:
    """Make the incident match the monitor's status after `check`."""
    if new_status == Monitor.Status.DOWN:
        open_incident(monitor, check, confirmations)
    elif new_status == Monitor.Status.UP:
        closed = close_incident(
            monitor,
            kind=IncidentEvent.Kind.RESOLVED,
            resolution="recovered",
            at=check.started_at,
            check=check,
            metadata={"check_id": check.id, "region": check.region},
        )
        if closed is not None:
            from notifications import services as notify

            notify.schedule_resolved(closed)


def close_for_pause(monitor: Monitor) -> None:
    """A paused monitor isn't checked, so its outage can no longer be observed."""
    closed = close_incident(monitor, kind=IncidentEvent.Kind.CLOSED_PAUSED, resolution="paused")
    if closed is not None:
        from notifications import services as notify

        notify.cancel_pending(closed, "The monitor was paused before this alert was sent.")


def close_for_edit(monitor: Monitor) -> None:
    """Changing the target or config invalidates the verdict: start fresh."""
    closed = close_incident(monitor, kind=IncidentEvent.Kind.CLOSED_EDITED, resolution="edited")
    if closed is not None:
        from notifications import services as notify

        notify.cancel_pending(closed, "The monitor was changed before this alert was sent.")


def duration_seconds(incident: Incident, now: datetime | None = None) -> int:
    end = incident.ended_at or (now or timezone.now())
    return max(0, round((end - incident.started_at).total_seconds()))
