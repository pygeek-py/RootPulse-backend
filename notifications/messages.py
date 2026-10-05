"""What an alert says, independent of where it is sent."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from django.conf import settings
from django.utils import timezone

from incidents.models import Incident
from monitoring.models import Monitor

OUTCOMES = {
    "ok": "OK",
    "timeout": "Timed out",
    "dns_error": "DNS lookup failed",
    "connection_refused": "Connection refused",
    "connection_error": "Connection failed",
    "tls_error": "TLS error",
    "keyword_missing": "Keyword not found",
    "keyword_found": "Unwanted keyword found",
    "too_many_redirects": "Too many redirects",
    "certificate_expiring": "Certificate expiring",
    "certificate_expired": "Certificate expired",
    "domain_expiring": "Domain expiring",
    "dns_changed": "DNS records changed",
    "heartbeat_missed": "Heartbeat missed",
}


def outcome_label(reason: str, status_code: int | None = None) -> str:
    if reason == "status_mismatch":
        return f"Unexpected status {status_code}" if status_code else "Unexpected status"
    return OUTCOMES.get(reason, reason.replace("_", " ").capitalize())


def humanize_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} h {minutes} min" if minutes else f"{hours} h"
    days, hours = divmod(hours, 24)
    return f"{days} d {hours} h" if hours else f"{days} d"


STAGE_NAMES = {
    "dns": "the DNS lookup",
    "tcp": "the connection",
    "tls": "the TLS handshake",
    "server": "the server's response",
}


def context_lines(incident: Incident | None) -> list[str]:
    """What the incident followed (a deploy) and where it went wrong, when we know."""
    if incident is None:
        return []
    lines = []
    deploy = incident.deploy if incident.deploy_id else None
    if deploy is not None:
        lag = max(0, round((incident.started_at - deploy.occurred_at).total_seconds()))
        version = f" ({deploy.version})" if deploy.version else ""
        lines.append(
            f"Started {humanize_duration(lag)} after a deploy of {deploy.service_name}{version}."
        )
    if incident.root_cause_stage in STAGE_NAMES:
        lines.append(f"Most likely stage: {STAGE_NAMES[incident.root_cause_stage]}.")
    return lines


def link_for(incident: Incident | None, monitor: Monitor) -> str:
    base = settings.FRONTEND_URL.rstrip("/")
    return f"{base}/incidents/{incident.id}" if incident else f"{base}/monitors/{monitor.id}"


def build(
    event: str, monitor: Monitor, incident: Incident | None = None, *, now: datetime | None = None
) -> dict[str, Any]:
    """The frozen description of one alert. Everything a channel needs, as plain JSON."""
    now = now or timezone.now()
    reason = incident.reason if incident else ""
    status_code = incident.status_code if incident else None
    cause = outcome_label(reason, status_code) if incident else ""
    duration = 0
    if incident:
        end = incident.ended_at or now
        duration = max(0, round((end - incident.started_at).total_seconds()))

    name = monitor.name
    if event == "opened":
        title, severity = f"{name} is DOWN", "down"
        summary = f"{cause}." if cause else "The monitor is down."
        summary = " ".join([summary, *context_lines(incident)])
    elif event == "reminder":
        title, severity = f"{name} is still DOWN", "down"
        summary = f"Down for {humanize_duration(duration)} ({cause})."
    elif event == "resolved":
        title, severity = f"{name} is back UP", "up"
        summary = f"It was down for {humanize_duration(duration)} ({cause})."
    else:
        title, severity = f"Test alert for {name}", "test"
        summary = "This is a test. If you can read it, alerts are reaching you."

    return {
        "event": event,
        "severity": severity,
        "title": title,
        "summary": summary,
        "url": link_for(incident, monitor),
        "created_at": now.isoformat(),
        "monitor": {
            "id": str(monitor.id),
            "name": monitor.name,
            "type": monitor.type,
            "target": monitor.target,
        },
        "incident": (
            {
                "id": str(incident.id),
                "started_at": incident.started_at.isoformat(),
                "ended_at": incident.ended_at.isoformat() if incident.ended_at else None,
                "duration_seconds": duration,
                "reason": reason,
                "reason_label": cause,
                "status_code": status_code,
                "root_cause_stage": incident.root_cause_stage,
                "deploy": (
                    {
                        "service": incident.deploy.service_name,
                        "version": incident.deploy.version,
                        "url": incident.deploy.url,
                        "occurred_at": incident.deploy.occurred_at.isoformat(),
                    }
                    if incident.deploy_id
                    else None
                ),
            }
            if incident
            else None
        ),
    }
