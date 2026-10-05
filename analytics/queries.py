"""Reading the rollups for the analytics endpoints.

Nothing here reads raw checks except `refresh_recent` (the last two hours of one monitor, well
inside the 24 to 48 hour window where raw reads are allowed). Incidents are read directly:
there are few of them compared with checks.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any

from django.db.models import Count, Max, Q, Sum
from django.utils import timezone

from incidents.models import Incident
from monitoring.models import Monitor

from . import rollups
from .models import CheckRollupDaily, CheckRollupHourly, FleetSummaryDaily
from .rollups import HOUR, overlap_seconds

#: Per-monitor ranges: (length, bucket size). Up to a week is hourly; longer is daily.
MONITOR_RANGES: dict[str, tuple[timedelta, str]] = {
    "24h": (timedelta(hours=24), "hour"),
    "7d": (timedelta(days=7), "hour"),
    "30d": (timedelta(days=30), "day"),
    "90d": (timedelta(days=90), "day"),
}
FLEET_RANGES: dict[str, int] = {"7d": 7, "30d": 30, "90d": 90}
TOP_OFFENDERS = 5


def _uptime(up: int, down: int, excluded: int) -> float | None:
    """Share of conclusive checks that passed. Failures inside an excluded incident are left
    out of both sides: they stay on record but don't count against uptime."""
    counted = up + down - excluded
    if counted <= 0:
        return None
    return round(100 * up / counted, 3)


def _weighted(pairs: list[tuple[int | None, int]]) -> int | None:
    pairs = [(v, c) for v, c in pairs if v is not None and c > 0]
    total = sum(c for _, c in pairs)
    return round(sum(v * c for v, c in pairs) / total) if total else None


def _avg(total: int, count: int) -> int | None:
    return round(total / count) if count else None


def _combine(rows) -> dict[str, Any]:
    rows = list(rows)
    up = sum(r.up for r in rows)
    down = sum(r.down for r in rows)
    excluded = sum(r.down_excluded for r in rows)
    response_count = sum(r.response_count for r in rows)
    timing = sum(r.timing_count for r in rows)
    return {
        "checks": up + down,
        "up": up,
        "down": down,
        "down_excluded": excluded,
        "uptime_percent": _uptime(up, down, excluded),
        "avg_response_ms": _avg(sum(r.response_sum_ms for r in rows), response_count),
        "p50_ms": _weighted([(r.p50_ms, r.response_count) for r in rows]),
        "p95_ms": _weighted([(r.p95_ms, r.response_count) for r in rows]),
        "timing": {
            "samples": timing,
            "dns_ms": _avg(sum(r.dns_sum_ms for r in rows), timing),
            "tcp_ms": _avg(sum(r.tcp_sum_ms for r in rows), timing),
            "tls_ms": _avg(sum(r.tls_sum_ms for r in rows), timing),
            "ttfb_ms": _avg(sum(r.ttfb_sum_ms for r in rows), timing),
        },
    }


def _incidents(monitor_ids, start: datetime, end: datetime, now: datetime):
    return list(
        Incident.objects.filter(monitor_id__in=monitor_ids, started_at__lt=end)
        .filter(Q(ended_at__isnull=True) | Q(ended_at__gt=start))
        .select_related("monitor")
    )


def incident_figures(incidents, start: datetime, end: datetime, now: datetime) -> dict[str, Any]:
    """Counts and times from incidents (excluded ones are left out, and reported separately)."""
    counted = [i for i in incidents if not i.excluded_from_reports]
    downtime = sum(
        overlap_seconds(i.started_at, i.ended_at or now, start, min(end, now)) for i in counted
    )
    started_here = [i for i in counted if start <= i.started_at < end]
    recovered = [i for i in started_here if i.ended_at and i.resolution == "recovered"]
    mttr = (
        round(sum((i.ended_at - i.started_at).total_seconds() for i in recovered) / len(recovered))
        if recovered
        else None
    )
    longest = max(
        (
            round(overlap_seconds(i.started_at, i.ended_at or now, start, min(end, now)))
            for i in counted
        ),
        default=0,
    )
    return {
        "incidents": len(started_here),
        "downtime_seconds": round(downtime),
        "mttr_seconds": mttr,
        "longest_incident_seconds": longest,
        "excluded_incidents": sum(1 for i in incidents if i.excluded_from_reports),
    }


# --- one monitor --------------------------------------------------------------------------


def monitor_analytics(monitor: Monitor, range_key: str, *, now: datetime | None = None) -> dict:
    now = now or timezone.now()
    length, grain = MONITOR_RANGES[range_key]
    rollups.refresh_recent(monitor, now=now)

    if grain == "hour":
        count = int(length / HOUR)
        first = rollups.floor_hour(now) - (count - 1) * HOUR
        rows = list(CheckRollupHourly.objects.filter(monitor=monitor, hour__gte=first))
        by_key = {r.hour: r for r in rows}
        keys = [first + i * HOUR for i in range(count)]
        stamps = [k.isoformat() for k in keys]
        start = first
    else:
        count = length.days
        first_day = now.astimezone(UTC).date() - timedelta(days=count - 1)
        rows = list(CheckRollupDaily.objects.filter(monitor=monitor, day__gte=first_day))
        by_key = {r.day: r for r in rows}
        keys = [first_day + timedelta(days=i) for i in range(count)]
        stamps = [k.isoformat() for k in keys]
        start = rollups.day_start(first_day)

    series = []
    for key, stamp in zip(keys, stamps, strict=True):
        row = by_key.get(key)
        if row is None:
            series.append(
                {"t": stamp, "checks": 0, "down": 0, "uptime_percent": None,
                 "avg_response_ms": None, "p50_ms": None, "p95_ms": None}  # fmt: skip
            )
            continue
        series.append(
            {
                "t": stamp,
                "checks": row.up + row.down,
                "down": row.down - row.down_excluded,
                "uptime_percent": _uptime(row.up, row.down, row.down_excluded),
                "avg_response_ms": _avg(row.response_sum_ms, row.response_count),
                "p50_ms": row.p50_ms,
                "p95_ms": row.p95_ms,
            }
        )

    combined = _combine(rows)
    # Incidents are measured over the exact range (the buckets above are aligned to hours/days).
    window_start = now - length if grain == "hour" else start
    figures = incident_figures(
        _incidents([monitor.id], window_start, now, now), window_start, now, now
    )
    return {
        "range": range_key,
        "granularity": grain,
        "start": window_start.isoformat(),
        "end": now.isoformat(),
        "summary": {
            "uptime_percent": combined["uptime_percent"],
            "checks": combined["checks"],
            "avg_response_ms": combined["avg_response_ms"],
            "p50_ms": combined["p50_ms"],
            "p95_ms": combined["p95_ms"],
            **figures,
        },
        "timing": combined["timing"],
        "series": series,
    }


# --- the whole account --------------------------------------------------------------------


def fleet_analytics(user, range_key: str, *, now: datetime | None = None) -> dict:
    now = now or timezone.now()
    days = FLEET_RANGES[range_key]
    today = now.astimezone(UTC).date()
    first_day = today - timedelta(days=days - 1)
    start = rollups.day_start(first_day)

    rows = {r.day: r for r in FleetSummaryDaily.objects.filter(user=user, day__gte=first_day)}
    daily = []
    for i in range(days):
        day: date = first_day + timedelta(days=i)
        row = rows.get(day)
        daily.append(
            {
                "date": day.isoformat(),
                "uptime_percent": _uptime(row.up, row.down, row.down_excluded) if row else None,
                "checks": (row.up + row.down) if row else 0,
                "incidents": row.incidents_opened if row else 0,
                "downtime_seconds": row.incident_downtime_seconds if row else 0,
            }
        )

    up = sum(r.up for r in rows.values())
    down = sum(r.down for r in rows.values())
    excluded = sum(r.down_excluded for r in rows.values())
    response_count = sum(r.response_count for r in rows.values())

    monitors = {m.id: m for m in Monitor.objects.filter(user=user)}
    incidents = _incidents(list(monitors), start, now, now)
    figures = incident_figures(incidents, start, now, now)
    # How many of this period's incidents followed a deploy (PRD section 6.5).
    began = [i for i in incidents if not i.excluded_from_reports and start <= i.started_at]
    linked = sum(1 for i in began if i.deploy_id)

    # Per-monitor downtime from incidents (this is what answers "who hurt me most").
    by_monitor: dict = defaultdict(lambda: {"downtime": 0.0, "incidents": 0})
    for incident in incidents:
        if incident.excluded_from_reports:
            continue
        entry = by_monitor[incident.monitor_id]
        entry["downtime"] += overlap_seconds(
            incident.started_at, incident.ended_at or now, start, now
        )
        if start <= incident.started_at:
            entry["incidents"] += 1

    uptimes = {
        row["monitor"]: _uptime(row["up"] or 0, row["down"] or 0, row["excluded"] or 0)
        for row in CheckRollupDaily.objects.filter(monitor__user=user, day__gte=first_day)
        .values("monitor")
        .annotate(up=Sum("up"), down=Sum("down"), excluded=Sum("down_excluded"))
    }
    offenders = sorted(by_monitor.items(), key=lambda kv: kv[1]["downtime"], reverse=True)
    top = [
        {
            "monitor": {
                "id": monitors[mid].id,
                "name": monitors[mid].name,
                "type": monitors[mid].type,
                "target": monitors[mid].target,
            },
            "downtime_seconds": round(entry["downtime"]),
            "incidents": entry["incidents"],
            "uptime_percent": uptimes.get(mid),
        }
        for mid, entry in offenders[:TOP_OFFENDERS]
        if mid in monitors and entry["downtime"] > 0
    ]

    as_of = FleetSummaryDaily.objects.filter(user=user).aggregate(m=Max("computed_at"))["m"]
    active_monitors = (
        CheckRollupDaily.objects.filter(monitor__user=user, day__gte=first_day)
        .values("monitor")
        .annotate(n=Count("id"))
        .count()
    )
    return {
        "range": range_key,
        "start": first_day.isoformat(),
        "end": today.isoformat(),
        "summary": {
            "monitors": active_monitors,
            "uptime_percent": _uptime(up, down, excluded),
            "checks": up + down,
            "avg_response_ms": _avg(sum(r.response_sum_ms for r in rows.values()), response_count),
            **figures,
            "deploy_linked_incidents": linked,
            "deploy_linked_percent": round(100 * linked / len(began), 1) if began else None,
        },
        "daily": daily,
        "top_offenders": top,
        "data_as_of": as_of.isoformat() if as_of else None,
    }
