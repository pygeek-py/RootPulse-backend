"""One adapter per feed format. Each turns a provider's raw JSON into the same thing.

Status pages are not standardized: most run Atlassian Statuspage (identical JSON everywhere),
but Slack and Google Cloud each publish their own shape, so each gets a small adapter rather
than one parser that tries to be clever. An adapter does two things and nothing else:

* `paths(base)`: which documents to fetch, as `{name: url}`;
* `parse(docs)`: turn the fetched documents into a `FeedResult`. It is pure (no network, no
  database), so it is tested against real captured responses.

The feed is outside data, so everything is cleaned on the way in: HTML and markdown become plain
text, links must be http(s), lists are capped, and a malformed entry is skipped rather than
failing the whole feed.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

MAX_UPDATES = 10
MAX_AFFECTED = 10
MAX_COMPONENTS = 25
BODY_LIMIT = 600


class FeedError(Exception):
    """The feed couldn't be read or isn't the format this adapter expects."""


@dataclass
class ParsedIncident:
    external_id: str
    title: str
    impact: str  # none | minor | major | critical
    stage: str  # investigating | identified | monitoring | resolved
    started_at: datetime
    ended_at: datetime | None
    url: str = ""
    updates: list[dict[str, str]] = field(default_factory=list)
    affected: list[str] = field(default_factory=list)


@dataclass
class FeedResult:
    status: str  # a Provider.Status value
    description: str = ""
    components: list[dict[str, str]] = field(default_factory=list)
    component_count: int = 0
    incidents: list[ParsedIncident] = field(default_factory=list)


# --- cleaning --------------------------------------------------------------------------

_TAG = re.compile(r"<[^>]+>")
_BREAKS = re.compile(r"</?(p|br|li|div|h[1-6]|ul|ol)[^>]*>", re.IGNORECASE)
_SPACE = re.compile(r"[ \t\r\f\v]+")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MD_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+.!|>-])")


def to_text(value: Any, limit: int = BODY_LIMIT) -> str:
    """Plain text from HTML or markdown, capped. Never raises."""
    if not isinstance(value, str):
        return ""
    text = _BREAKS.sub("\n", value)
    text = _TAG.sub("", text)
    text = html.unescape(text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)  # markdown headings
    text = _MD_ESCAPE.sub(r"\1", text)  # markdown escapes: \# becomes #
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)  # markdown headings
    text = text.replace("**", "").replace("__", "")
    text = _CONTROL.sub("", text)
    lines = [_SPACE.sub(" ", line).strip() for line in text.split("\n")]
    text = "\n".join(line for line in lines if line)
    return text[: limit - 1] + "…" if len(text) > limit else text


def clean_url(value: Any) -> str:
    text = value.strip() if isinstance(value, str) else ""
    return text[:500] if re.match(r"^https?://[^\s]+$", text, re.IGNORECASE) else ""


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _name(value: Any, limit: int = 80) -> str:
    return to_text(value, limit).replace("\n", " ") if isinstance(value, str) else ""


def _ordered(items: list[str], cap: int) -> list[str]:
    seen: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.append(item)
    return seen[:cap]


def _finish(started: datetime, ended: datetime | None) -> datetime | None:
    """Some feeds report a resolution time before the start (retroactive notices): the incident
    can't have ended before it began, so it ends when it began."""
    return None if ended is None else max(ended, started)


class Adapter:
    key = ""

    def paths(self, base: str) -> dict[str, str]:
        raise NotImplementedError

    #: documents that must be present for a parse to make sense
    required: tuple[str, ...] = ()

    def parse(self, docs: dict[str, Any]) -> FeedResult:
        raise NotImplementedError


# --- Atlassian Statuspage --------------------------------------------------------------

STATUSPAGE_INDICATOR = {
    "none": "operational",
    "minor": "degraded",
    "major": "partial_outage",
    "critical": "major_outage",
    "maintenance": "maintenance",
}
STATUSPAGE_COMPONENT = {
    "degraded_performance": "degraded",
    "partial_outage": "partial_outage",
    "major_outage": "major_outage",
    "under_maintenance": "maintenance",
}
IMPACTS = {"none", "minor", "major", "critical"}
STAGES = {"investigating", "identified", "monitoring", "resolved"}
NOT_INCIDENTS = {"scheduled", "in_progress", "verifying", "completed"}  # maintenance windows


class StatuspageAdapter(Adapter):
    """Atlassian Statuspage v2: `/api/v2/summary.json` and `/api/v2/incidents.json`."""

    key = "statuspage"
    required = ("summary",)

    def paths(self, base: str) -> dict[str, str]:
        return {
            "summary": f"{base}/api/v2/summary.json",
            "incidents": f"{base}/api/v2/incidents.json",
        }

    def parse(self, docs: dict[str, Any]) -> FeedResult:
        summary = docs.get("summary")
        if not isinstance(summary, dict) or not isinstance(summary.get("status"), dict):
            raise FeedError("That isn't a Statuspage summary.")

        indicator = str(summary["status"].get("indicator", ""))
        status = STATUSPAGE_INDICATOR.get(indicator, "unknown")
        maintenance = [
            m
            for m in (summary.get("scheduled_maintenances") or [])
            if isinstance(m, dict) and m.get("status") == "in_progress"
        ]
        if status == "operational" and maintenance:
            status = "maintenance"

        components = [
            c
            for c in (summary.get("components") or [])
            if isinstance(c, dict) and not c.get("group")
        ]
        troubled = [
            {"name": _name(c.get("name")), "status": STATUSPAGE_COMPONENT[c["status"]]}
            for c in components
            if c.get("status") in STATUSPAGE_COMPONENT
        ]

        # Unresolved incidents appear in the summary; the history has the rest. If the history
        # document is missing, the unresolved ones are still reported.
        by_id: dict[str, ParsedIncident] = {}
        history = docs.get("incidents")
        raw = list((history or {}).get("incidents") or []) if isinstance(history, dict) else []
        for raw_incident in [*raw, *(summary.get("incidents") or [])]:
            parsed = self._incident(raw_incident)
            if parsed is not None:
                by_id[parsed.external_id] = parsed  # the summary's copy is the freshest

        return FeedResult(
            status=status,
            description=_name(summary["status"].get("description"), 200),
            components=troubled[:MAX_COMPONENTS],
            component_count=len(components),
            incidents=list(by_id.values()),
        )

    @staticmethod
    def _incident(raw: Any) -> ParsedIncident | None:
        if not isinstance(raw, dict) or not raw.get("id"):
            return None
        if str(raw.get("status", "")) in NOT_INCIDENTS:
            return None
        started = parse_time(raw.get("started_at")) or parse_time(raw.get("created_at"))
        if started is None:
            return None
        stage = str(raw.get("status", ""))
        stage = (
            "resolved" if stage == "postmortem" else stage if stage in STAGES else "investigating"
        )
        ended = parse_time(raw.get("resolved_at"))
        if stage == "resolved" and ended is None:
            ended = parse_time(raw.get("updated_at")) or started
        if stage != "resolved" and ended is not None:
            stage = "resolved"  # the provider stamped a resolution time
        updates, affected = [], []
        for update in raw.get("incident_updates") or []:
            if not isinstance(update, dict):
                continue
            when = parse_time(update.get("display_at")) or parse_time(update.get("created_at"))
            ustage = str(update.get("status", ""))
            ustage = "resolved" if ustage == "postmortem" else ustage
            updates.append(
                {
                    "stage": ustage if ustage in STAGES else "investigating",
                    "body": to_text(update.get("body")),
                    "at": when.isoformat() if when else "",
                }
            )
            affected += [
                _name(c.get("name"))
                for c in update.get("affected_components") or []
                if isinstance(c, dict)
            ]
        affected += [
            _name(c.get("name")) for c in raw.get("components") or [] if isinstance(c, dict)
        ]
        impact = str(raw.get("impact", ""))
        return ParsedIncident(
            external_id=str(raw["id"])[:128],
            title=_name(raw.get("name"), 300) or "Incident",
            impact=impact if impact in IMPACTS else "minor",
            stage=stage,
            started_at=started,
            ended_at=_finish(started, ended),
            url=clean_url(raw.get("shortlink")),
            updates=updates[:MAX_UPDATES],
            affected=_ordered(affected, MAX_AFFECTED),
        )


# --- Slack -----------------------------------------------------------------------------


class SlackAdapter(Adapter):
    """Slack's own status API: `/api/v2.0.0/current` (what's active) and `/history`."""

    key = "slack"
    required = ("current",)

    def paths(self, base: str) -> dict[str, str]:
        return {
            "current": f"{base}/api/v2.0.0/current",
            "history": f"{base}/api/v2.0.0/history",
        }

    def parse(self, docs: dict[str, Any]) -> FeedResult:
        current = docs.get("current")
        if not isinstance(current, dict) or "active_incidents" not in current:
            raise FeedError("That isn't Slack's status feed.")
        history = docs.get("history")
        raw = [
            *(history if isinstance(history, list) else []),
            *(current.get("active_incidents") or []),
        ]
        by_id: dict[str, ParsedIncident] = {}
        for item in raw:
            parsed = self._incident(item)
            if parsed is not None:
                by_id[parsed.external_id] = parsed  # active ones come last, so they win
        active = [i for i in by_id.values() if i.ended_at is None]
        if not active:
            status, description = "operational", "No incidents reported"
        else:
            worst = max(
                active, key=lambda i: ["none", "minor", "major", "critical"].index(i.impact)
            )
            status = "degraded" if worst.impact in ("none", "minor") else "partial_outage"
            description = f"{len(active)} active incident{'s' if len(active) != 1 else ''}"
        return FeedResult(status=status, description=description, incidents=list(by_id.values()))

    @staticmethod
    def _incident(raw: Any) -> ParsedIncident | None:
        if not isinstance(raw, dict) or raw.get("id") is None:
            return None
        started = parse_time(raw.get("date_created"))
        if started is None:
            return None
        resolved = str(raw.get("status", "")) == "resolved"
        notes = [n for n in raw.get("notes") or [] if isinstance(n, dict)]
        updates = [
            {
                "stage": "resolved" if resolved and i == 0 else "investigating",
                "body": to_text(n.get("body")),
                "at": (parse_time(n.get("date_created")) or started).isoformat(),
            }
            for i, n in enumerate(reversed(notes))  # newest first
        ]
        latest = updates[0]["body"].lower() if updates else ""
        stage = (
            "resolved"
            if resolved
            else "identified" if re.search(r"\b(identified|fix)\b", latest) else "investigating"
        )
        ended = (parse_time(raw.get("date_updated")) or started) if resolved else None
        return ParsedIncident(
            external_id=str(raw["id"]),
            title=_name(raw.get("title"), 300) or "Incident",
            # Slack publishes no severity: a "notice" is informational, an "incident" is real.
            impact="none" if raw.get("type") == "notice" else "minor",
            stage=stage,
            started_at=started,
            ended_at=_finish(started, ended),
            url=clean_url(raw.get("url")),
            updates=updates[:MAX_UPDATES],
            affected=_ordered([_name(s) for s in raw.get("services") or []], MAX_AFFECTED),
        )


# --- Google Cloud ----------------------------------------------------------------------


class GoogleCloudAdapter(Adapter):
    """Google Cloud's `incidents.json`: one list, `end` is null while an incident is open."""

    key = "gcp"
    required = ("incidents",)

    def paths(self, base: str) -> dict[str, str]:
        return {"incidents": f"{base}/incidents.json"}

    def parse(self, docs: dict[str, Any]) -> FeedResult:
        raw = docs.get("incidents")
        if not isinstance(raw, list):
            raise FeedError("That isn't Google Cloud's incident list.")
        incidents = [i for i in (self._incident(r) for r in raw) if i is not None]
        active = [i for i in incidents if i.ended_at is None]
        worst = max(
            (["none", "minor", "major", "critical"].index(i.impact) for i in active), default=-1
        )
        status = (
            "operational" if worst < 1
            else "degraded" if worst == 1
            else "partial_outage" if worst == 2
            else "major_outage"
        )  # fmt: skip
        description = (
            f"{len(active)} active incident{'s' if len(active) != 1 else ''}"
            if active
            else "No active incidents"
        )
        return FeedResult(status=status, description=description, incidents=incidents)

    @staticmethod
    def _incident(raw: Any) -> ParsedIncident | None:
        if not isinstance(raw, dict) or not raw.get("id"):
            return None
        started = parse_time(raw.get("begin")) or parse_time(raw.get("created"))
        if started is None:
            return None
        ended = parse_time(raw.get("end"))
        impact_key = str(raw.get("status_impact", ""))
        impact = {"SERVICE_OUTAGE": "major", "SERVICE_DISRUPTION": "minor"}.get(impact_key, "none")
        if impact == "major" and str(raw.get("severity")) == "high":
            impact = "critical"
        updates = []
        for update in raw.get("updates") or []:
            if isinstance(update, dict):
                when = parse_time(update.get("when")) or parse_time(update.get("created"))
                updates.append(
                    {
                        "stage": "investigating",
                        "body": to_text(update.get("text")),
                        "at": when.isoformat() if when else "",
                    }
                )
        updates.sort(key=lambda u: u["at"], reverse=True)
        if ended and updates:
            updates[0]["stage"] = "resolved"  # the newest word on a closed incident
        affected = [
            _name(p.get("title")) for p in raw.get("affected_products") or [] if isinstance(p, dict)
        ]
        uri = raw.get("uri")
        return ParsedIncident(
            external_id=str(raw["id"]),
            title=_name(raw.get("external_desc"), 300) or "Incident",
            impact=impact,
            stage="resolved" if ended else "investigating",
            started_at=started,
            ended_at=_finish(started, ended),
            url=clean_url(f"https://status.cloud.google.com/{uri}") if isinstance(uri, str) else "",
            updates=updates[:MAX_UPDATES],
            affected=_ordered(affected, MAX_AFFECTED),
        )


ADAPTERS: dict[str, Adapter] = {
    a.key: a for a in (StatuspageAdapter(), SlackAdapter(), GoogleCloudAdapter())
}
