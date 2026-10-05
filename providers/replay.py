"""Seeing a past outage as it looked when it was happening.

A saved status-page response is a photograph taken *afterwards*: an incident that began at 14:47
and ended at 17:56 appears already resolved. To replay it honestly, `rewind` turns the clock
back to `as_of`: incidents that hadn't started are dropped, ones that hadn't ended yet are open
again, and each one shows only the updates (and the stage) the provider had published by then.
Feeding the rewound result through the normal ingest then behaves exactly as it did live.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from .adapters import FeedResult, ParsedIncident

ORDER = {"investigating": 0, "identified": 1, "monitoring": 2, "resolved": 3}


def _as_of(incident: ParsedIncident, when: datetime) -> ParsedIncident | None:
    if incident.started_at > when:
        return None  # it hadn't happened yet
    ended = incident.ended_at
    if ended is not None and ended <= when:
        return incident  # already over by then: unchanged
    seen = [u for u in incident.updates if u["at"] and datetime.fromisoformat(u["at"]) <= when]
    stage = max((u["stage"] for u in seen), key=lambda s: ORDER.get(s, 0), default="investigating")
    if stage == "resolved":
        stage = "monitoring"  # a resolution published later hasn't been written yet
    return replace(incident, ended_at=None, stage=stage, updates=seen)


def rewind(result: FeedResult, when: datetime) -> FeedResult:
    """What this feed looked like at `when`."""
    incidents = [i for i in (_as_of(i, when) for i in result.incidents) if i is not None]
    ongoing = [i for i in incidents if i.ended_at is None]
    if ongoing:
        worst = max(ongoing, key=lambda i: ["none", "minor", "major", "critical"].index(i.impact))
        status = {
            "none": "operational",
            "minor": "degraded",
            "major": "partial_outage",
            "critical": "major_outage",
        }[worst.impact]
        description = f"{len(ongoing)} active incident{'s' if len(ongoing) != 1 else ''}"
    else:
        status, description = "operational", "All systems operational"
    return replace(
        result, status=status, description=description, components=[], incidents=incidents
    )
