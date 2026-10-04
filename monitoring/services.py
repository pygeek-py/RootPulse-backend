"""Monitor lifecycle rules that more than one view needs."""

from __future__ import annotations

from datetime import datetime, timedelta

from django.utils import timezone

from .models import Monitor


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
