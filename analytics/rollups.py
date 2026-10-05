"""Building the rollups: raw checks -> hourly -> daily -> fleet.

The one rule: **a rollup row is recomputed from its source and replaced**, never added to.
Re-running the job over the same window therefore gives the same numbers, which is what
makes it safe to retry, to overlap with another run, or to re-run after an incident is
excluded from reports.

Raw checks older than the retention window are deleted once they are folded in (and never
re-derived: an old hour whose checks are gone keeps its rollup).
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from django.conf import settings
from django.core.cache import cache
from django.db.models import Count, Max, Min, Sum
from django.utils import timezone

from accounts.models import User
from incidents.models import Incident
from monitoring.models import Check, Monitor

from .models import CheckRollupDaily, CheckRollupHourly, FleetSummaryDaily

logger = logging.getLogger(__name__)

HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
BACKFILL_CHUNK = timedelta(days=7)  # bounds how many raw rows are in memory at once
REFRESH_SECONDS = 60  # how long a monitor's recent hours are trusted after a refresh
PRUNE_BATCH = 5000
PRUNE_LIMIT = 50_000  # per run, so one run can't spend its whole time deleting
FAR_FUTURE = datetime(9999, 1, 1, tzinfo=UTC)


def floor_hour(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def day_start(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def retention_cutoff(now: datetime) -> datetime:
    return floor_hour(now - timedelta(days=settings.CHECK_RETENTION_DAYS))


def percentile(sorted_values: list[int], q: float) -> int | None:
    """Nearest-rank percentile of an already sorted list."""
    if not sorted_values:
        return None
    rank = max(1, math.ceil(q * len(sorted_values)))
    return sorted_values[rank - 1]


def overlap_seconds(start: datetime, end: datetime, window_start: datetime, window_end: datetime):
    lo, hi = max(start, window_start), min(end, window_end)
    return max(0.0, (hi - lo).total_seconds())


@dataclass
class _Hour:
    up: int = 0
    down: int = 0
    inconclusive: int = 0
    down_excluded: int = 0
    responses: list[int] = field(default_factory=list)
    timing_count: int = 0
    dns: int = 0
    tcp: int = 0
    tls: int = 0
    ttfb: int = 0


def _excluded_windows(monitor: Monitor, start: datetime, end: datetime, now: datetime):
    incidents = Incident.objects.filter(
        monitor=monitor, excluded_from_reports=True, started_at__lt=end
    ).values_list("started_at", "ended_at")
    return [(s, e or now) for s, e in incidents if (e or now) >= start]


def to_update_fields() -> list[str]:
    return [
        "up", "down", "inconclusive", "down_excluded", "response_count", "response_sum_ms",
        "response_min_ms", "response_max_ms", "p50_ms", "p95_ms", "timing_count",
        "dns_sum_ms", "tcp_sum_ms", "tls_sum_ms", "ttfb_sum_ms",
    ]  # fmt: skip


# --- hourly -------------------------------------------------------------------------------


def compute_hours(monitor: Monitor, start: datetime, end: datetime, *, now=None) -> list[datetime]:
    """Recompute every hour in [start, end) for one monitor from its raw checks. Returns the
    hours that now have a rollup row."""
    now = now or timezone.now()
    start, end = floor_hour(start), floor_hour(end)
    if start >= end:
        return []
    cutoff = retention_cutoff(now)

    rows = (
        Check.objects.filter(
            monitor=monitor, confirmation=False, started_at__gte=start, started_at__lt=end
        )
        .order_by("started_at")
        .values_list(
            "started_at", "success", "duration_ms", "dns_ms", "tcp_ms", "tls_ms", "ttfb_ms"
        )
    )
    windows = _excluded_windows(monitor, start, end, now)

    hours: dict[datetime, _Hour] = defaultdict(_Hour)
    for started, success, duration, dns, tcp, tls, ttfb in rows:
        bucket = hours[floor_hour(started)]
        if success is None:
            bucket.inconclusive += 1
        elif success:
            bucket.up += 1
            if duration is not None:
                bucket.responses.append(duration)
            if ttfb is not None:  # the breakdown is only there for HTTP checks
                bucket.timing_count += 1
                bucket.dns += dns or 0
                bucket.tcp += tcp or 0
                bucket.tls += tls or 0
                bucket.ttfb += ttfb
        else:
            bucket.down += 1
            if any(w_start <= started <= w_end for w_start, w_end in windows):
                bucket.down_excluded += 1

    # One read and at most two writes however many hours there are (the database may be far
    # away: a round trip per hour made a backfill take minutes).
    existing = {
        row.hour: row
        for row in CheckRollupHourly.objects.filter(monitor=monitor, hour__gte=start, hour__lt=end)
    }
    to_create, to_update = [], []
    for hour, b in hours.items():
        responses = sorted(b.responses)
        values = {
            "up": b.up,
            "down": b.down,
            "inconclusive": b.inconclusive,
            "down_excluded": b.down_excluded,
            "response_count": len(responses),
            "response_sum_ms": sum(responses),
            "response_min_ms": responses[0] if responses else None,
            "response_max_ms": responses[-1] if responses else None,
            "p50_ms": percentile(responses, 0.50),
            "p95_ms": percentile(responses, 0.95),
            "timing_count": b.timing_count,
            "dns_sum_ms": b.dns,
            "tcp_sum_ms": b.tcp,
            "tls_sum_ms": b.tls,
            "ttfb_sum_ms": b.ttfb,
        }
        row = existing.get(hour)
        if row is None:
            to_create.append(CheckRollupHourly(monitor=monitor, hour=hour, **values))
        else:
            for name, value in values.items():
                setattr(row, name, value)
            row.computed_at = now
            to_update.append(row)
    if to_create:
        CheckRollupHourly.objects.bulk_create(to_create, ignore_conflicts=True)
    if to_update:
        CheckRollupHourly.objects.bulk_update(to_update, [*to_update_fields(), "computed_at"])

    # An hour with no checks any more (and whose raw rows can still be trusted to be complete)
    # has no row either. Hours past retention are frozen: their raw rows may be gone.
    stale = CheckRollupHourly.objects.filter(
        monitor=monitor, hour__gte=max(start, cutoff), hour__lt=end
    )
    stale.exclude(hour__in=list(hours)).delete()
    return sorted(hours)


# --- daily --------------------------------------------------------------------------------


def _weighted(rows, field_name: str) -> int | None:
    """Count-weighted mean of an hourly percentile (approximate; see RollupStats)."""
    pairs = [(getattr(r, field_name), r.response_count) for r in rows]
    pairs = [(value, count) for value, count in pairs if value is not None and count > 0]
    total = sum(count for _, count in pairs)
    if total == 0:
        return None
    return round(sum(value * count for value, count in pairs) / total)


def rebuild_days(monitor: Monitor, days: set[date]) -> int:
    """Recompute each day's row from the hourly rows. Returns how many day rows exist now."""
    written = 0
    for day in sorted(days):
        start = day_start(day)
        rows = list(
            CheckRollupHourly.objects.filter(monitor=monitor, hour__gte=start, hour__lt=start + DAY)
        )
        if not rows:
            CheckRollupDaily.objects.filter(monitor=monitor, day=day).delete()
            continue
        mins = [r.response_min_ms for r in rows if r.response_min_ms is not None]
        maxes = [r.response_max_ms for r in rows if r.response_max_ms is not None]
        CheckRollupDaily.objects.update_or_create(
            monitor=monitor,
            day=day,
            defaults={
                "up": sum(r.up for r in rows),
                "down": sum(r.down for r in rows),
                "inconclusive": sum(r.inconclusive for r in rows),
                "down_excluded": sum(r.down_excluded for r in rows),
                "response_count": sum(r.response_count for r in rows),
                "response_sum_ms": sum(r.response_sum_ms for r in rows),
                "response_min_ms": min(mins) if mins else None,
                "response_max_ms": max(maxes) if maxes else None,
                "p50_ms": _weighted(rows, "p50_ms"),
                "p95_ms": _weighted(rows, "p95_ms"),
                "timing_count": sum(r.timing_count for r in rows),
                "dns_sum_ms": sum(r.dns_sum_ms for r in rows),
                "tcp_sum_ms": sum(r.tcp_sum_ms for r in rows),
                "tls_sum_ms": sum(r.tls_sum_ms for r in rows),
                "ttfb_sum_ms": sum(r.ttfb_sum_ms for r in rows),
            },
        )
        written += 1
    return written


# --- fleet --------------------------------------------------------------------------------


def rebuild_fleet_days(user, days: set[date], *, now=None) -> int:
    """Recompute each day's fleet row from the monitors' daily rows plus their incidents."""
    now = now or timezone.now()
    written = 0
    for day in sorted(days):
        start, end = day_start(day), day_start(day) + DAY
        daily = CheckRollupDaily.objects.filter(monitor__user=user, day=day).aggregate(
            monitors=Count("id"),
            up=Sum("up"),
            down=Sum("down"),
            inconclusive=Sum("inconclusive"),
            down_excluded=Sum("down_excluded"),
            response_count=Sum("response_count"),
            response_sum_ms=Sum("response_sum_ms"),
        )
        incidents = Incident.objects.filter(
            monitor__user=user, excluded_from_reports=False, started_at__lt=end
        ).values_list("started_at", "ended_at")
        opened = 0
        downtime = 0.0
        for started, ended in incidents:
            ended = ended or now
            if ended <= start:
                continue
            if start <= started < end:
                opened += 1
            downtime += overlap_seconds(started, ended, start, min(end, max(now, start)))
        if not daily["monitors"] and not opened and not downtime:
            FleetSummaryDaily.objects.filter(user=user, day=day).delete()
            continue
        FleetSummaryDaily.objects.update_or_create(
            user=user,
            day=day,
            defaults={
                "monitors": daily["monitors"] or 0,
                "up": daily["up"] or 0,
                "down": daily["down"] or 0,
                "inconclusive": daily["inconclusive"] or 0,
                "down_excluded": daily["down_excluded"] or 0,
                "response_count": daily["response_count"] or 0,
                "response_sum_ms": daily["response_sum_ms"] or 0,
                "incidents_opened": opened,
                "incident_downtime_seconds": round(downtime),
            },
        )
        written += 1
    return written


# --- orchestration ------------------------------------------------------------------------


def _days_between(start: datetime, end: datetime) -> set[date]:
    days: set[date] = set()
    cursor = start
    while cursor < end:
        days.add(cursor.astimezone(UTC).date())
        cursor += HOUR
    return days


@dataclass
class RollupSummary:
    monitors: int = 0
    hours: int = 0
    days: int = 0
    fleet_days: int = 0
    pruned: int = 0
    failed: int = 0

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def run_rollups_once(
    *, now: datetime | None = None, full: bool = False, prune: bool = True
) -> RollupSummary:
    """One pass: refresh recent hours for every monitor (or everything, with `full`), rebuild
    the days they touch, rebuild the fleet rows, then delete raw checks past retention."""
    now = now or timezone.now()
    end = floor_hour(now) + HOUR
    lookback = timedelta(hours=settings.ROLLUP_LOOKBACK_HOURS)
    summary = RollupSummary()
    days_by_user: dict = defaultdict(set)
    done: list = []

    for monitor in Monitor.objects.only("id", "user_id"):
        summary.monitors += 1
        try:
            latest = CheckRollupHourly.objects.filter(monitor=monitor).aggregate(m=Max("hour"))["m"]
            if full or latest is None:
                first = Check.objects.filter(monitor=monitor).aggregate(m=Min("started_at"))["m"]
                if first is None:
                    done.append(monitor.id)  # nothing to fold in, nothing to prune
                    continue
                start = floor_hour(first)
            else:
                start = floor_hour(latest - lookback)
            touched: set[date] = set()
            cursor = start
            while cursor < end:
                chunk_end = min(cursor + BACKFILL_CHUNK, end)
                summary.hours += len(compute_hours(monitor, cursor, chunk_end, now=now))
                touched |= _days_between(cursor, chunk_end)
                cursor = chunk_end
            summary.days += rebuild_days(monitor, touched)
            days_by_user[monitor.user_id] |= touched
            done.append(monitor.id)
        except Exception:  # noqa: BLE001 - one bad monitor mustn't stop the others
            logger.exception("rollups failed for monitor %s", monitor.id)
            summary.failed += 1

    today = now.astimezone(UTC).date()
    for user_id in {m.user_id for m in Monitor.objects.only("user_id")} | set(days_by_user):
        # Today and yesterday are always rebuilt: an ongoing incident keeps adding downtime.
        days = days_by_user[user_id] | {today, today - timedelta(days=1)}
        try:
            user = User.objects.filter(pk=user_id).first()
            if user is not None:
                summary.fleet_days += rebuild_fleet_days(user, days, now=now)
        except Exception:  # noqa: BLE001
            logger.exception("fleet rollup failed for user %s", user_id)
            summary.failed += 1

    if prune and done:
        summary.pruned = prune_old_checks(done, now=now)
    return summary


def prune_old_checks(monitor_ids: list, *, now: datetime) -> int:
    """Delete raw checks past retention, for monitors whose rollups are up to date."""
    cutoff = retention_cutoff(now)
    deleted = 0
    while deleted < PRUNE_LIMIT:
        ids = list(
            Check.objects.filter(monitor_id__in=monitor_ids, started_at__lt=cutoff).values_list(
                "id", flat=True
            )[:PRUNE_BATCH]
        )
        if not ids:
            break
        Check.objects.filter(id__in=ids).delete()
        deleted += len(ids)
    return deleted


# --- targeted refreshes -------------------------------------------------------------------


def refresh_recent(monitor: Monitor, *, now: datetime | None = None) -> None:
    """Bring the last couple of hours up to date. Cheap, and inside the window where reading
    raw checks is allowed; lets a monitor's analytics be current without waiting for the job."""
    now = now or timezone.now()
    # A page that reloads or several people opening it shouldn't recompute every time.
    if not cache.add(f"rollup-refresh:{monitor.pk}", 1, REFRESH_SECONDS):
        return
    start = floor_hour(now) - timedelta(hours=settings.ROLLUP_LOOKBACK_HOURS - 1)
    end = floor_hour(now) + HOUR
    compute_hours(monitor, start, end, now=now)
    rebuild_days(monitor, _days_between(start, end))


def recompute_for_incident(incident: Incident, *, now: datetime | None = None) -> None:
    """An incident was excluded from (or put back into) reports: redo the hours it covers."""
    now = now or timezone.now()
    monitor = incident.monitor
    start = floor_hour(incident.started_at)
    end = floor_hour(incident.ended_at or now) + HOUR
    touched: set[date] = set()
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + BACKFILL_CHUNK, end)
        compute_hours(monitor, cursor, chunk_end, now=now)
        touched |= _days_between(cursor, chunk_end)
        cursor = chunk_end
    rebuild_days(monitor, touched)
    rebuild_fleet_days(monitor.user, touched, now=now)
