"""Recording a deploy, and connecting it to the incidents it may have caused."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from incidents.models import Incident, IncidentEvent
from monitoring.models import Monitor

from .models import Deploy, DeploySource
from .parsers import ParsedDeploy

logger = logging.getLogger(__name__)

MAX_RAW_BYTES = 8 * 1024
#: A provider's own clock is believed unless it is wildly off: older than this, or from the future.
MAX_AGE = timedelta(hours=24)
MAX_FUTURE = timedelta(minutes=5)


def window() -> timedelta:
    return timedelta(seconds=settings.DEPLOY_CORRELATION_WINDOW_SECONDS)


def when_it_happened(claimed: datetime | None, now: datetime) -> datetime:
    """The deploy's time: what the provider says, unless that is implausible."""
    if claimed is None or claimed > now + MAX_FUTURE or claimed < now - MAX_AGE:
        return now
    return min(claimed, now)


def _names(parsed: ParsedDeploy) -> list[str]:
    seen: list[str] = []
    for name in [parsed.service_name, *parsed.aliases]:
        lowered = name.strip().lower()
        if lowered and lowered not in seen:
            seen.append(lowered)
    return seen


def _trim(summary: dict) -> dict:
    """Keep the small, useful part of what the provider sent (it is outside data)."""
    text = json.dumps(summary, default=str)
    return summary if len(text.encode()) <= MAX_RAW_BYTES else {"truncated": True}


def tagged_monitors(deploy: Deploy) -> list[Monitor]:
    """Monitors whose `deploy_service` tag names this deploy's service. An untagged monitor
    matches nothing: linking has to be a choice, or one deploy could blame every monitor."""
    wanted = set(deploy.names)
    return [
        m
        for m in Monitor.objects.filter(user_id=deploy.user_id).exclude(deploy_service="")
        if m.deploy_service.strip().lower() in wanted
    ]


def latest_deploy_before(monitor: Monitor, at: datetime) -> Deploy | None:
    """The most recent deploy of the monitor's tagged service in the window before `at`."""
    tag = monitor.deploy_service.strip().lower()
    if not tag:
        return None
    candidates = Deploy.objects.filter(
        user_id=monitor.user_id, occurred_at__lte=at, occurred_at__gte=at - window()
    ).order_by("-occurred_at")
    return next((d for d in candidates if tag in d.names), None)


def _link(incident: Incident, deploy: Deploy) -> None:
    incident.deploy = deploy
    incident.save(update_fields=["deploy"])
    lag = max(0, round((incident.started_at - deploy.occurred_at).total_seconds()))
    IncidentEvent.objects.create(
        incident=incident,
        kind=IncidentEvent.Kind.DEPLOY_LINKED,
        created_at=timezone.now(),
        metadata={
            "deploy_id": str(deploy.id),
            "service": deploy.service_name,
            "version": deploy.version,
            "seconds_before": lag,
        },
    )


def correlate_incident(incident: Incident) -> Deploy | None:
    """When an incident opens: link it to the deploy that most plausibly caused it."""
    deploy = latest_deploy_before(incident.monitor, incident.started_at)
    if deploy is not None:
        _link(incident, deploy)
    return deploy


def link_deploy_to_incidents(deploy: Deploy) -> int:
    """When a deploy arrives late (after the incident it caused was already opened): link
    incidents that started just after it. A closer deploy replaces a farther one."""
    linked = 0
    for monitor in tagged_monitors(deploy):
        incidents = Incident.objects.filter(
            monitor=monitor,
            started_at__gte=deploy.occurred_at,
            started_at__lte=deploy.occurred_at + window(),
        ).select_related("deploy")
        for incident in incidents:
            current = incident.deploy
            if current is None or current.occurred_at < deploy.occurred_at:
                _link(incident, deploy)
                linked += 1
    return linked


class Outcome:
    RECORDED = "recorded"
    DUPLICATE = "duplicate"
    IGNORED = "ignored"


def ingest(
    source: DeploySource, parsed: ParsedDeploy, body: bytes
) -> tuple[str, Deploy | None, str]:
    """Store a verified deploy. Returns (outcome, deploy, reason)."""
    now = timezone.now()
    source.last_received_at = now
    source.save(update_fields=["last_received_at"])

    wanted = source.environment_filter.strip().lower()
    if wanted and parsed.environment and parsed.environment.lower() != wanted:
        return (
            Outcome.IGNORED,
            None,
            f"environment '{parsed.environment}' isn't '{source.environment_filter}'",
        )

    external = parsed.external_id
    if not external or external.endswith(":"):  # the provider left out its own id
        external = hashlib.sha256(body).hexdigest()[:32]
    try:
        with transaction.atomic():
            deploy = Deploy.objects.create(
                user=source.user,
                source=source,
                source_type=source.type,
                source_name=source.name,
                external_id=external[:128],
                service_name=parsed.service_name,
                names=_names(parsed),
                environment=parsed.environment,
                version=parsed.version,
                url=parsed.url,
                occurred_at=when_it_happened(parsed.occurred_at, now),
                raw_payload=_trim(parsed.summary),
            )
    except IntegrityError:
        return (
            Outcome.DUPLICATE,
            Deploy.objects.filter(source=source, external_id=external[:128]).first(),
            "",
        )
    try:
        with transaction.atomic():
            link_deploy_to_incidents(deploy)
    except Exception:  # noqa: BLE001 - the deploy is recorded; linking is best effort
        logger.exception("couldn't link deploy %s to incidents", deploy.id)
    return Outcome.RECORDED, deploy, ""
