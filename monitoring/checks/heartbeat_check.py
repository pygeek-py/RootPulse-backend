"""Heartbeat (cron) monitor: no outbound request. The user's job pings a secret
URL; this just asks "did a ping arrive recently enough?"."""

from __future__ import annotations

from datetime import datetime, timedelta

from . import result as r


def deadline_for(
    created_at: datetime, last_ping: datetime | None, interval: int, grace: int
) -> datetime:
    """The moment after which the monitor is considered late."""
    return (last_ping or created_at) + timedelta(seconds=interval + grace)


def evaluate_heartbeat(
    *,
    created_at: datetime,
    last_ping: datetime | None,
    interval_seconds: int,
    grace_seconds: int,
    now: datetime,
) -> r.CheckResult:
    deadline = deadline_for(created_at, last_ping, interval_seconds, grace_seconds)
    if last_ping is not None and now <= deadline:
        return r.CheckResult(
            success=True,
            status_detail=r.OK,
            detail={"last_ping": last_ping.isoformat(), "next_expected_by": deadline.isoformat()},
        )
    return r.CheckResult(
        success=False,
        status_detail=r.HEARTBEAT_MISSED,
        detail={
            "last_ping": last_ping.isoformat() if last_ping else None,
            "expected_by": deadline.isoformat(),
        },
    )
