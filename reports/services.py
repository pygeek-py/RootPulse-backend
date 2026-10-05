"""The figures behind a report, for a date range the person picks.

Everything here reads what the dashboard reads: the daily rollups for checks and uptime, and
incidents for downtime (so a report for the last 30 days agrees with the analytics page for the
last 30 days). Days are UTC days, because that is what the rollups are made of. Incidents marked
"excluded from reports" are left out of every figure and of the list, and counted so the report
can say how many were left out.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any

from django.utils import timezone
from rest_framework.exceptions import ValidationError

from analytics import rollups
from analytics.models import CheckRollupDaily
from analytics.queries import _combine, _incidents, incident_figures
from incidents.models import Incident
from monitoring.models import Monitor

MAX_DAYS = 366
MAX_LISTED_INCIDENTS = 500


def parse_range(raw_start: str | None, raw_end: str | None, *, today: date | None = None):
    """Validate `start_date` / `end_date` (YYYY-MM-DD, both required, inclusive)."""
    today = today or timezone.now().astimezone(UTC).date()
    errors = {}
    parsed: dict[str, date] = {}
    for name, raw in (("start_date", raw_start), ("end_date", raw_end)):
        if not raw:
            errors[name] = "Choose a date."
            continue
        try:
            parsed[name] = date.fromisoformat(raw)
        except ValueError:
            errors[name] = "Use a date like 2026-06-01."
    if errors:
        raise ValidationError(errors)
    start, end = parsed["start_date"], parsed["end_date"]
    if end < start:
        raise ValidationError({"end_date": "The end can't be before the start."})
    if end > today:
        raise ValidationError({"end_date": "The end can't be in the future."})
    if (end - start).days + 1 > MAX_DAYS:
        raise ValidationError({"start_date": f"Pick a range of at most {MAX_DAYS} days."})
    return start, end


def _monitor_row(monitor: Monitor, rows: list, incidents: list, window, now) -> dict[str, Any]:
    figures = _combine(rows)
    mine = [i for i in incidents if i.monitor_id == monitor.id]
    inc = incident_figures(mine, *window, now)
    return {
        "name": monitor.name,
        "type": monitor.type,
        "target": monitor.target,
        "checks": figures["checks"],
        "up": figures["up"],
        "down": figures["down"],
        "uptime_percent": figures["uptime_percent"],
        "avg_response_ms": figures["avg_response_ms"],
        "incidents": inc["incidents"],
        "downtime_seconds": inc["downtime_seconds"],
        "longest_incident_seconds": inc["longest_incident_seconds"],
        "mttr_seconds": inc["mttr_seconds"],
        "excluded_incidents": inc["excluded_incidents"],
    }


def uptime_report(
    user, start: date, end: date, *, monitor_id=None, now: datetime | None = None
) -> dict[str, Any]:
    now = now or timezone.now()
    window = (rollups.day_start(start), rollups.day_start(end) + timedelta(days=1))

    monitors = Monitor.objects.filter(user=user).order_by("name", "created_at")
    if monitor_id is not None:
        monitors = monitors.filter(pk=monitor_id)
    monitors = list(monitors)
    ids = [m.id for m in monitors]

    by_monitor: dict[Any, list] = defaultdict(list)
    all_rows = []
    for row in CheckRollupDaily.objects.filter(monitor_id__in=ids, day__gte=start, day__lte=end):
        by_monitor[row.monitor_id].append(row)
        all_rows.append(row)

    incidents = _incidents(ids, *window, now)
    summary_inc = incident_figures(incidents, *window, now)
    summary = _combine(all_rows)

    rows = []
    for m in monitors:
        row = _monitor_row(m, by_monitor.get(m.id, []), incidents, window, now)
        # A monitor with nothing at all in the period (no checks, no incidents) isn't a row of
        # zeros: leaving it out keeps a report about last month from listing next month's monitors.
        if row["checks"] or row["incidents"] or row["downtime_seconds"]:
            rows.append(row)

    listed = sorted(
        (i for i in incidents if not i.excluded_from_reports and window[0] <= i.started_at),
        key=lambda i: i.started_at,
        reverse=True,
    )
    names = {m.id: m for m in monitors}
    return {
        "period": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "days": (end - start).days + 1,
        },
        "generated_at": now,
        "summary": {
            "monitors": len(rows),
            "checks": summary["checks"],
            "uptime_percent": summary["uptime_percent"],
            "avg_response_ms": summary["avg_response_ms"],
            "incidents": summary_inc["incidents"],
            "downtime_seconds": summary_inc["downtime_seconds"],
            "mttr_seconds": summary_inc["mttr_seconds"],
            "longest_incident_seconds": summary_inc["longest_incident_seconds"],
            "excluded_incidents": summary_inc["excluded_incidents"],
        },
        "monitors": rows,
        "incidents": [_incident_row(i, names[i.monitor_id], now) for i in listed][
            :MAX_LISTED_INCIDENTS
        ],
        "incidents_truncated": len(listed) > MAX_LISTED_INCIDENTS,
    }


def _incident_row(incident: Incident, monitor: Monitor, now: datetime) -> dict[str, Any]:
    ended = incident.ended_at
    return {
        "monitor": monitor.name,
        "started_at": incident.started_at,
        "ended_at": ended,
        "ongoing": ended is None,
        "duration_seconds": round(((ended or now) - incident.started_at).total_seconds()),
        "reason": incident.reason,
        "status_code": incident.status_code,
        "resolution": incident.resolution,
    }
