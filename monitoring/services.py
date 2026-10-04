"""Monitor lifecycle rules that more than one view needs."""

from __future__ import annotations

from datetime import datetime, timedelta

from django.conf import settings
from django.utils import timezone

from .models import Check, Monitor


def first_check_due(monitor: Monitor, now: datetime | None = None) -> datetime:
    """When the scheduler should next look at a freshly created or resumed monitor.

    Active monitors are due immediately so a new one shows a real status within
    a scheduler pass. A heartbeat monitor is different: nothing is checked, we
    wait for the user's job to ping, so it's due only after the first expected
    ping plus its grace period.
    """
    now = now or timezone.now()
    if monitor.type == Monitor.Type.CRON:
        grace = monitor.config.get("grace_period_seconds", 0)
        return now + timedelta(seconds=monitor.interval_seconds + grace)
    return now


def summarize(monitor: Monitor, hours: int = 24, now: datetime | None = None) -> dict:
    """Uptime and response-time stats over the last `hours`, from raw checks.

    Raw checks are only ever read for a window this short (docs/plan/03-monitoring-engine.md
    section 14); longer ranges come from rollups. Only scheduled checks from the primary
    region count, and inconclusive ones are ignored, so a re-check or a failed
    prober can't distort the figures.
    """
    now = now or timezone.now()
    rows = list(
        Check.objects.filter(
            monitor=monitor,
            confirmation=False,
            region=settings.PRIMARY_REGION,
            success__isnull=False,
            started_at__gte=now - timedelta(hours=hours),
        ).values_list("success", "duration_ms")
    )
    ups = sum(1 for success, _ in rows if success)
    timings = sorted(ms for success, ms in rows if success and ms is not None)
    p95 = timings[min(len(timings) - 1, int(len(timings) * 0.95))] if timings else None
    return {
        "window_hours": hours,
        "checks": len(rows),
        "uptime_percent": round(100 * ups / len(rows), 2) if rows else None,
        "avg_response_ms": round(sum(timings) / len(timings)) if timings else None,
        "p95_response_ms": p95,
    }
