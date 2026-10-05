"""A pre-filled postmortem: the timeline writes itself, the thinking is left to people."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.utils.text import slugify

from . import services
from .models import Incident, IncidentEvent

REASONS = {
    "timeout": "the request timed out",
    "dns_error": "the DNS lookup failed",
    "connection_refused": "the connection was refused",
    "connection_error": "the connection failed",
    "tls_error": "the TLS handshake failed",
    "status_mismatch": "an unexpected HTTP status came back",
    "keyword_missing": "the expected keyword was missing",
    "keyword_found": "an unwanted keyword appeared",
    "too_many_redirects": "the request hit too many redirects",
    "certificate_expiring": "the TLS certificate is about to expire",
    "certificate_expired": "the TLS certificate has expired",
    "domain_expiring": "the domain registration is about to expire",
    "dns_changed": "the DNS records changed",
    "heartbeat_missed": "an expected heartbeat ping never arrived",
}


def reason_text(incident: Incident) -> str:
    text = REASONS.get(incident.reason, incident.reason.replace("_", " "))
    if incident.reason == "status_mismatch" and incident.status_code:
        text = f"an unexpected HTTP status ({incident.status_code}) came back"
    return text


def humanize(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} seconds"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}" + (f" {rest} s" if rest else "")
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''}" + (f" {minutes} min" if minutes else "")
    days, hours = divmod(hours, 24)
    return f"{days} day{'s' if days != 1 else ''}" + (f" {hours} h" if hours else "")


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _event_line(event: IncidentEvent, incident: Incident) -> str | None:
    meta = event.metadata
    if event.kind == IncidentEvent.Kind.OPENED:
        where = meta.get("region", "the primary region")
        return f"Outage detected: {reason_text(incident)} (from {where})."
    if event.kind == IncidentEvent.Kind.CONFIRMED:
        failing = ", ".join(meta.get("regions_failing", []))
        return f"Confirmed from more than one place (failing: {failing})."
    if event.kind == IncidentEvent.Kind.DEPLOY_LINKED:
        version = f" ({meta['version']})" if meta.get("version") else ""
        lag = humanize(int(meta.get("seconds_before", 0)))
        return f"Started {lag} after a deploy of {meta.get('service', 'the service')}{version}."
    if event.kind == IncidentEvent.Kind.RESOLVED:
        return "Recovered: a check succeeded."
    if event.kind == IncidentEvent.Kind.CLOSED_PAUSED:
        return "Closed because the monitor was paused."
    if event.kind == IncidentEvent.Kind.CLOSED_EDITED:
        return "Closed because the monitor's settings were changed."
    return None  # report-exclusion toggles aren't part of the story


STAGE_WORDS = {
    "dns": "the DNS lookup",
    "tcp": "the connection",
    "tls": "the TLS handshake",
    "server": "the server's response",
}


def _root_cause_hint(incident: Incident) -> str:
    """What RootPulse already knows, so the writer starts from facts (empty if nothing)."""
    lines = []
    deploy = incident.deploy if incident.deploy_id else None
    if deploy is not None:
        lag = humanize(max(0, round((incident.started_at - deploy.occurred_at).total_seconds())))
        version = f" ({deploy.version})" if deploy.version else ""
        lines.append(f"- It began {lag} after a deploy of {deploy.service_name}{version}.")
    stage = STAGE_WORDS.get(incident.root_cause_stage)
    if stage:
        lines.append(f"- The request went wrong at {stage}.")
    return "\n".join(lines) + "\n\n" if lines else ""


def build(incident: Incident, timezone_name: str = "UTC") -> dict[str, str]:
    zone = _zone(timezone_name)
    monitor = incident.monitor

    def fmt(moment: datetime, with_date: bool = False) -> str:
        local = moment.astimezone(zone)
        return local.strftime("%Y-%m-%d %H:%M" if with_date else "%H:%M")

    duration = services.duration_seconds(incident)
    entries: list[tuple[datetime, str]] = []
    for event in incident.events.all():
        line = _event_line(event, incident)
        if line:
            entries.append((event.created_at, line))
    for comment in incident.comments.select_related("author"):
        who = comment.author.email if comment.author else "someone"
        entries.append((comment.created_at, f"Note from {who}: {comment.body}"))
    entries.sort(key=lambda item: item[0])

    timeline = "\n".join(f"- **{fmt(when)}** {text}" for when, text in entries)
    root_cause_hint = _root_cause_hint(incident)
    zone_label = timezone_name if zone.key == timezone_name else "UTC"
    day = fmt(incident.started_at, with_date=True).split(" ")[0]

    markdown = f"""# Postmortem: {monitor.name} outage, {day}

| | |
| --- | --- |
| **Monitor** | {monitor.name} ({monitor.target or monitor.get_type_display()}) |
| **Started** | {fmt(incident.started_at, with_date=True)} ({zone_label}) |
| **Ended** | {fmt(incident.ended_at, with_date=True) if incident.ended_at else "Ongoing"} |
| **Duration** | {humanize(duration)} |
| **Detected by** | RootPulse: {reason_text(incident)} |

## Summary

{monitor.name} was unavailable for {humanize(duration)} because {reason_text(incident)}.
_Describe what users experienced, in plain words._

## Timeline ({zone_label})

{timeline}

## Root cause

{root_cause_hint}_What actually went wrong, and why?_

## Resolution

_What fixed it?_

## What went well

-

## What went wrong

-

## Where we got lucky

-

## Action items

- [ ]
"""
    return {
        "markdown": markdown,
        "filename": f"postmortem-{slugify(monitor.name) or 'monitor'}-{day}.md",
    }
