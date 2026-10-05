"""Pre-aggregated check data (docs/plan/03-monitoring-engine.md section 14).

Raw `Check` rows are high volume (a 5-minute monitor from three regions writes ~860 a day), so
dashboards never read them beyond the last day or two. They read these tables instead, which
`analytics.rollups` fills in. Every table is *replaced*, never incremented, when recomputed, so
running the job twice (or re-running a window) can't double count.

Days and hours are UTC.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models


class RollupStats(models.Model):
    """The numbers every grain carries.

    Only **scheduled** checks count (re-checks run to confirm a failure are ignored, as on the
    monitor page), and a check is up, down or inconclusive. Inconclusive ones (a problem on our
    side) are kept apart so they never lower uptime.
    """

    up = models.PositiveIntegerField(default=0)
    down = models.PositiveIntegerField(default=0)
    inconclusive = models.PositiveIntegerField(default=0)
    # Down checks that fell inside an incident marked "exclude from reports": they stay on
    # record but don't count against uptime.
    down_excluded = models.PositiveIntegerField(default=0)

    # Response time of successful checks.
    response_count = models.PositiveIntegerField(default=0)
    response_sum_ms = models.BigIntegerField(default=0)
    response_min_ms = models.PositiveIntegerField(null=True, blank=True)
    response_max_ms = models.PositiveIntegerField(null=True, blank=True)
    # Exact for an hour. For a day (and any longer range) these combine the hourly values
    # weighted by how many responses each had, so they are close, not exact.
    p50_ms = models.PositiveIntegerField(null=True, blank=True)
    p95_ms = models.PositiveIntegerField(null=True, blank=True)

    # Where the time went, for checks that have a breakdown (HTTP).
    timing_count = models.PositiveIntegerField(default=0)
    dns_sum_ms = models.BigIntegerField(default=0)
    tcp_sum_ms = models.BigIntegerField(default=0)
    tls_sum_ms = models.BigIntegerField(default=0)
    ttfb_sum_ms = models.BigIntegerField(default=0)

    computed_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class CheckRollupHourly(RollupStats):
    """One monitor, one hour. Read by the 24-hour and 7-day charts."""

    id = models.BigAutoField(primary_key=True)
    monitor = models.ForeignKey("monitoring.Monitor", on_delete=models.CASCADE, related_name="+")
    hour = models.DateTimeField()  # the start of the UTC hour

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["monitor", "hour"], name="one_hourly_rollup")
        ]
        indexes = [models.Index(fields=["monitor", "-hour"])]

    def __str__(self) -> str:
        return f"{self.monitor_id} {self.hour:%Y-%m-%d %H}h"


class CheckRollupDaily(RollupStats):
    """One monitor, one day, built from the hourly rows (not from raw checks)."""

    id = models.BigAutoField(primary_key=True)
    monitor = models.ForeignKey("monitoring.Monitor", on_delete=models.CASCADE, related_name="+")
    day = models.DateField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["monitor", "day"], name="one_daily_rollup")]
        indexes = [models.Index(fields=["monitor", "-day"])]

    def __str__(self) -> str:
        return f"{self.monitor_id} {self.day}"


class FleetSummaryDaily(models.Model):
    """One user, one day, across all their monitors (the fleet overview)."""

    id = models.BigAutoField(primary_key=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="fleet_summaries"
    )
    day = models.DateField()
    monitors = models.PositiveIntegerField(default=0)  # monitors that had checks that day
    up = models.PositiveIntegerField(default=0)
    down = models.PositiveIntegerField(default=0)
    inconclusive = models.PositiveIntegerField(default=0)
    down_excluded = models.PositiveIntegerField(default=0)
    response_count = models.PositiveIntegerField(default=0)
    response_sum_ms = models.BigIntegerField(default=0)
    # From incidents (not excluded ones): how many began that day, and how much of that day
    # was spent down (an incident that spans midnight counts in both days).
    incidents_opened = models.PositiveIntegerField(default=0)
    incident_downtime_seconds = models.PositiveIntegerField(default=0)
    computed_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "day"], name="one_fleet_summary_per_day")
        ]
        indexes = [models.Index(fields=["user", "-day"])]

    def __str__(self) -> str:
        return f"{self.user_id} {self.day}"
