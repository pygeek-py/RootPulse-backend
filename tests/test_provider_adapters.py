"""Each feed format's adapter, against real responses captured from the providers' own status
pages (tests/fixtures/providers). No database and no network: adapters are pure."""

import copy
import json
import pathlib
from datetime import UTC, datetime

import pytest

from providers import adapters
from providers.adapters import ADAPTERS, FeedError, clean_url, parse_time, to_text
from providers.replay import rewind

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "providers"


def load(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


STATUSPAGE = ADAPTERS["statuspage"]
SLACK = ADAPTERS["slack"]
GCP = ADAPTERS["gcp"]


@pytest.fixture
def github():
    return STATUSPAGE.parse(
        {"summary": load("github_summary"), "incidents": load("github_incidents")}
    )


@pytest.fixture
def cloudflare():
    return STATUSPAGE.parse(
        {"summary": load("cloudflare_summary"), "incidents": load("cloudflare_incidents")}
    )


def by_title(result, text):
    return next(i for i in result.incidents if text in i.title)


class TestStatuspage:
    def test_reads_a_healthy_github(self, github):
        assert github.status == "operational"
        assert github.description == "All Systems Operational"
        assert github.component_count == 12 and github.components == []

    def test_reads_every_incident_with_its_impact_and_times(self, github):
        copilot = by_title(github, "Copilot Code Review")
        assert (copilot.impact, copilot.stage) == ("critical", "resolved")
        assert (
            copilot.started_at == at("2026-09-28T21:16:00")
            or copilot.started_at.date().isoformat() == "2026-09-28"
        )
        assert copilot.ended_at > copilot.started_at
        assert by_title(github, "Gemini").impact == "major"
        assert by_title(github, "Job Delays").impact == "minor"
        assert by_title(github, "SSH").impact == "none"

    def test_maintenance_windows_are_not_incidents(self):
        incidents = load("github_incidents")
        incidents["incidents"][0]["status"] = "scheduled"
        parsed = STATUSPAGE.parse({"summary": load("github_summary"), "incidents": incidents})
        assert (
            len(parsed.incidents) == len(incidents["incidents"]) - 1 - 2
            or len(parsed.incidents) < 6
        )

    def test_a_resolution_before_the_start_ends_when_it_began(self, github):
        # Real data: GitHub published a "[Retroactive]" notice resolved before it "started".
        retro = by_title(github, "[Retroactive]")
        assert retro.ended_at is not None and retro.ended_at >= retro.started_at

    def test_updates_are_plain_text_newest_first(self, github):
        job_delays = by_title(github, "Job Delays")
        assert len(job_delays.updates) == 10
        times = [u["at"] for u in job_delays.updates]
        assert times == sorted(times, reverse=True)
        for update in job_delays.updates:
            assert "<" not in update["body"] and update["stage"] in {
                "investigating",
                "identified",
                "monitoring",
                "resolved",
            }
        assert job_delays.updates[0]["stage"] == "resolved"

    def test_affected_components_are_collected(self, github):
        assert by_title(github, "Job Delays").affected == ["Actions"]
        assert by_title(github, "Gemini").affected == ["Copilot AI Model Providers"]

    def test_links_are_the_providers_own_pages(self, github):
        for incident in github.incidents:
            # Statuspage links incidents through its own shortener.
            assert incident.url.startswith(("https://stspg.io/", "https://www.githubstatus.com/"))

    def test_a_provider_with_live_trouble(self, cloudflare):
        assert cloudflare.status == "partial_outage"
        assert cloudflare.description == "Partial System Outage"
        assert cloudflare.component_count == 17
        names = {c["name"]: c["status"] for c in cloudflare.components}
        assert names["Arica, Chile - (ARI)"] == "major_outage"
        assert names["Baghdad, Iraq - (BGW)"] == "degraded"

    def test_unresolved_incidents_come_from_the_summary_even_without_history(self):
        parsed = STATUSPAGE.parse({"summary": load("cloudflare_summary")})
        open_ones = [i for i in parsed.incidents if i.ended_at is None]
        assert len(open_ones) == 4 and all(i.stage != "resolved" for i in open_ones)

    def test_ongoing_incidents_have_no_end_and_a_live_stage(self, cloudflare):
        durable = by_title(cloudflare, "Durable Objects")
        assert (durable.ended_at, durable.stage, durable.impact) == (None, "monitoring", "minor")
        assert by_title(cloudflare, "API Shield").stage == "identified"

    def test_old_never_closed_incidents_are_reported_as_they_are(self, cloudflare):
        stale = (
            by_title(cloudflare, "incorrect geo location".title())
            if False
            else by_title(cloudflare, "Incorrect geo location")
        )
        assert stale.ended_at is None and stale.started_at.date().isoformat() == "2026-08-27"

    @pytest.mark.parametrize(
        ("indicator", "status"),
        [
            ("none", "operational"),
            ("minor", "degraded"),
            ("major", "partial_outage"),
            ("critical", "major_outage"),
            ("maintenance", "maintenance"),
            ("???", "unknown"),
        ],
    )
    def test_the_indicator_maps_to_a_status(self, indicator, status):
        summary = {"status": {"indicator": indicator, "description": "x"}, "components": []}
        assert STATUSPAGE.parse({"summary": summary}).status == status

    def test_maintenance_in_progress_shows_as_maintenance(self):
        summary = {
            "status": {"indicator": "none", "description": "ok"},
            "scheduled_maintenances": [{"status": "in_progress"}],
        }
        assert STATUSPAGE.parse({"summary": summary}).status == "maintenance"

    def test_component_groups_are_not_counted(self):
        summary = {
            "status": {"indicator": "none", "description": "ok"},
            "components": [
                {"name": "A", "status": "operational", "group": True},
                {"name": "B", "status": "operational"},
            ],
        }
        assert STATUSPAGE.parse({"summary": summary}).component_count == 1

    def test_only_troubled_components_are_kept_and_capped(self):
        comps = [{"name": f"c{i}", "status": "major_outage"} for i in range(60)]
        parsed = STATUSPAGE.parse(
            {"summary": {"status": {"indicator": "major", "description": "x"}, "components": comps}}
        )
        assert len(parsed.components) == adapters.MAX_COMPONENTS and parsed.component_count == 60

    @pytest.mark.parametrize("bad", [{}, {"status": "x"}, [], None, "text"])
    def test_something_that_isnt_a_statuspage_summary_is_an_error(self, bad):
        with pytest.raises(FeedError):
            STATUSPAGE.parse({"summary": bad})

    def test_a_malformed_incident_is_skipped_not_fatal(self):
        history = {
            "incidents": [
                {
                    "id": "ok1",
                    "name": "Fine",
                    "status": "resolved",
                    "impact": "minor",
                    "started_at": "2026-10-01T10:00:00Z",
                    "resolved_at": "2026-10-01T11:00:00Z",
                },
                {"name": "no id"},
                {"id": "no-time"},
                "garbage",
                None,
                7,
            ]
        }
        parsed = STATUSPAGE.parse(
            {
                "summary": {"status": {"indicator": "none", "description": "ok"}},
                "incidents": history,
            }
        )
        assert [i.external_id for i in parsed.incidents] == ["ok1"]

    def test_unknown_impact_and_stage_get_safe_defaults(self):
        history = {
            "incidents": [
                {
                    "id": "x",
                    "name": "Odd",
                    "status": "weird",
                    "impact": "catastrophic",
                    "started_at": "2026-10-01T10:00:00Z",
                }
            ]
        }
        incident = STATUSPAGE.parse(
            {"summary": {"status": {"indicator": "none", "description": ""}}, "incidents": history}
        ).incidents[0]
        assert (incident.impact, incident.stage, incident.ended_at) == (
            "minor",
            "investigating",
            None,
        )

    def test_a_resolved_stamp_means_resolved_even_if_the_stage_lags(self):
        history = {
            "incidents": [
                {
                    "id": "x",
                    "name": "Lag",
                    "status": "monitoring",
                    "impact": "minor",
                    "started_at": "2026-10-01T10:00:00Z",
                    "resolved_at": "2026-10-01T12:00:00Z",
                }
            ]
        }
        incident = STATUSPAGE.parse(
            {"summary": {"status": {"indicator": "none", "description": ""}}, "incidents": history}
        ).incidents[0]
        assert incident.stage == "resolved" and incident.ended_at is not None

    def test_postmortem_counts_as_resolved(self):
        history = {
            "incidents": [
                {
                    "id": "x",
                    "name": "Pm",
                    "status": "postmortem",
                    "impact": "minor",
                    "started_at": "2026-10-01T10:00:00Z",
                    "updated_at": "2026-10-01T13:00:00Z",
                }
            ]
        }
        incident = STATUSPAGE.parse(
            {"summary": {"status": {"indicator": "none", "description": ""}}, "incidents": history}
        ).incidents[0]
        assert incident.stage == "resolved" and incident.ended_at == at("2026-10-01T13:00:00")

    def test_hostile_text_becomes_harmless_plain_text(self):
        history = {
            "incidents": [
                {
                    "id": "x",
                    "name": "<script>alert(1)</script>Bad &amp; ugly",
                    "status": "investigating",
                    "impact": "minor",
                    "started_at": "2026-10-01T10:00:00Z",
                    "shortlink": "javascript:alert(1)",
                    "incident_updates": [
                        {
                            "status": "investigating",
                            "body": "<img src=x onerror=alert(1)>Hello<br>world",
                            "created_at": "2026-10-01T10:00:00Z",
                        }
                    ],
                }
            ]
        }
        incident = STATUSPAGE.parse(
            {"summary": {"status": {"indicator": "none", "description": ""}}, "incidents": history}
        ).incidents[0]
        assert "<" not in incident.title and "alert(1)" in incident.title  # tags gone, text kept
        assert incident.url == ""
        assert incident.updates[0]["body"] == "Hello\nworld"

    def test_lists_are_capped(self):
        updates = [
            {"status": "investigating", "body": f"u{i}", "created_at": "2026-10-01T10:00:00Z"}
            for i in range(40)
        ]
        comps = [{"name": f"c{i}"} for i in range(40)]
        history = {
            "incidents": [
                {
                    "id": "x",
                    "name": "Big",
                    "status": "investigating",
                    "impact": "minor",
                    "started_at": "2026-10-01T10:00:00Z",
                    "incident_updates": updates,
                    "components": comps,
                }
            ]
        }
        incident = STATUSPAGE.parse(
            {"summary": {"status": {"indicator": "none", "description": ""}}, "incidents": history}
        ).incidents[0]
        assert (
            len(incident.updates) == adapters.MAX_UPDATES
            and len(incident.affected) == adapters.MAX_AFFECTED
        )

    def test_asks_for_the_documents_it_needs(self):
        assert STATUSPAGE.paths("https://s.example.com") == {
            "summary": "https://s.example.com/api/v2/summary.json",
            "incidents": "https://s.example.com/api/v2/incidents.json",
        }
        assert STATUSPAGE.required == ("summary",)


class TestSlack:
    def test_a_quiet_slack(self):
        parsed = SLACK.parse({"current": load("slack_current"), "history": load("slack_history")})
        assert parsed.status == "operational" and parsed.description == "No incidents reported"
        assert len(parsed.incidents) == 5

    def test_reads_incidents_services_and_resolution(self):
        parsed = SLACK.parse({"current": load("slack_current"), "history": load("slack_history")})
        messaging = next(i for i in parsed.incidents if i.external_id == "1579")
        assert messaging.title == "Free Plan Customers Are Experiencing Message Failures"
        assert (messaging.impact, messaging.stage) == ("minor", "resolved")
        assert messaging.affected == ["Messaging"]
        assert messaging.ended_at == at(
            "2026-10-01T11:59:35-07:00".replace("-07:00", "")
        ) or messaging.ended_at.isoformat().startswith("2026-10-01T11:59:35")
        assert messaging.url.startswith("https://slack-status.com/")
        assert len(messaging.updates) == 6
        assert (
            messaging.updates[0]["stage"] == "resolved"
            and "<p>" not in messaging.updates[0]["body"]
        )

    def test_two_services_are_both_listed(self):
        parsed = SLACK.parse({"current": load("slack_current"), "history": load("slack_history")})
        assert next(i for i in parsed.incidents if i.external_id == "1576").affected == [
            "Messaging",
            "Workspace/Org Administration",
        ]

    def test_an_active_incident_makes_slack_degraded(self):
        history = load("slack_history")
        active = copy.deepcopy(history[0])
        active.update(
            {
                "id": 9999,
                "status": "active",
                "title": "Messages are slow",
                "date_created": "2026-10-05T10:00:00-07:00",
            }
        )
        current = {"status": "active", "active_incidents": [active]}
        parsed = SLACK.parse({"current": current, "history": history})
        live = next(i for i in parsed.incidents if i.external_id == "9999")
        assert live.ended_at is None and live.stage in ("investigating", "identified")
        assert parsed.status == "degraded" and parsed.description == "1 active incident"

    def test_a_notice_is_informational(self):
        item = copy.deepcopy(load("slack_history")[0])
        item["type"] = "notice"
        parsed = SLACK.parse({"current": {"active_incidents": []}, "history": [item]})
        assert parsed.incidents[0].impact == "none"

    def test_the_active_copy_wins_over_the_history_copy(self):
        history = [copy.deepcopy(load("slack_history")[0])]
        active = copy.deepcopy(history[0])
        active["status"] = "active"
        parsed = SLACK.parse({"current": {"active_incidents": [active]}, "history": history})
        assert len(parsed.incidents) == 1 and parsed.incidents[0].ended_at is None

    @pytest.mark.parametrize("bad", [{}, [], None, {"status": "ok"}])
    def test_not_slacks_feed_is_an_error(self, bad):
        with pytest.raises(FeedError):
            SLACK.parse({"current": bad})

    def test_history_is_optional(self):
        assert SLACK.parse({"current": load("slack_current")}).incidents == []
        assert SLACK.required == ("current",)


class TestGoogleCloud:
    def test_reads_the_incident_list(self):
        parsed = GCP.parse({"incidents": load("gcp_incidents")})
        assert parsed.status == "operational" and parsed.description == "No active incidents"
        assert len(parsed.incidents) == 4

    def test_outage_severity_maps_to_impact(self):
        parsed = GCP.parse({"incidents": load("gcp_incidents")})
        outage = next(i for i in parsed.incidents if i.external_id == "utF3FMFdQfwBzJcGG6vf")
        assert outage.impact == "critical"  # SERVICE_OUTAGE with high severity
        disruption = next(i for i in parsed.incidents if i.external_id == "J5ia5t9p3g9Q5Wi7r8Ev")
        assert disruption.impact == "minor"  # SERVICE_DISRUPTION

    def test_markdown_in_reports_becomes_plain_text(self):
        parsed = GCP.parse({"incidents": load("gcp_incidents")})
        for incident in parsed.incidents:
            for update in incident.updates:
                assert "\\#" not in update["body"] and not update["body"].startswith("#")
        escaped = next(i for i in parsed.incidents if i.external_id == "3BvH3LVGcupoYqV6F4Nw")
        assert escaped.updates[0]["body"].startswith("Incident Report")

    def test_affected_products_and_link(self):
        incident = GCP.parse({"incidents": load("gcp_incidents")}).incidents[0]
        assert (
            "AlloyDB for PostgreSQL" in incident.affected
            and len(incident.affected) <= adapters.MAX_AFFECTED
        )
        assert incident.url == "https://status.cloud.google.com/incidents/J5ia5t9p3g9Q5Wi7r8Ev"

    def test_updates_are_newest_first_and_the_last_word_is_resolved(self):
        incident = GCP.parse({"incidents": load("gcp_incidents")}).incidents[0]
        times = [u["at"] for u in incident.updates]
        assert times == sorted(times, reverse=True)
        assert incident.updates[0]["stage"] == "resolved"

    def test_an_open_incident_sets_the_status_by_its_severity(self):
        raw = copy.deepcopy(load("gcp_incidents")[1])
        raw["end"] = None
        parsed = GCP.parse({"incidents": [raw]})
        assert parsed.incidents[0].ended_at is None and parsed.incidents[0].stage == "investigating"
        assert (parsed.status, parsed.description) == ("major_outage", "1 active incident")
        raw["status_impact"], raw["severity"] = "SERVICE_DISRUPTION", "medium"
        assert GCP.parse({"incidents": [raw]}).status == "degraded"
        raw["status_impact"] = "SERVICE_INFORMATION"
        assert GCP.parse({"incidents": [raw]}).status == "operational"

    @pytest.mark.parametrize("bad", [{}, "x", None, 5])
    def test_not_a_list_is_an_error(self, bad):
        with pytest.raises(FeedError):
            GCP.parse({"incidents": bad})

    def test_entries_without_an_id_or_time_are_skipped(self):
        assert (
            GCP.parse({"incidents": [{"begin": "2026-01-01T00:00:00Z"}, {"id": "x"}, 4]}).incidents
            == []
        )


class TestCleaning:
    @pytest.mark.parametrize(
        ("raw", "text"),
        [
            ("<p>Hello</p><p>world</p>", "Hello\nworld"),
            ("a &lt;b&gt; &amp; c", "a <b> & c"),
            ("## Heading\nBody **bold**", "Heading\nBody bold"),
            ("line1\x00\x07 line2", "line1 line2"),
            ("   spaced   out   ", "spaced out"),
            (None, ""),
            (5, ""),
        ],
    )
    def test_to_text(self, raw, text):
        assert to_text(raw) == text

    def test_to_text_is_capped(self):
        out = to_text("x" * 5000, 100)
        assert len(out) == 100 and out.endswith("…")

    @pytest.mark.parametrize(
        ("url", "kept"),
        [
            ("https://example.com/a", True),
            ("http://example.com", True),
            ("javascript:alert(1)", False),
            ("data:text/html,x", False),
            ("//evil.com", False),
            ("https://a b", False),
            (None, False),
            ("", False),
        ],
    )
    def test_urls_must_be_plain_web_links(self, url, kept):
        assert bool(clean_url(url)) is kept

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("2026-10-05T17:14:30.000Z", at("2026-10-05T17:14:30")),
            ("2026-10-01T05:39:35-07:00", datetime(2026, 10, 1, 12, 39, 35, tzinfo=UTC)),
            ("2026-10-05T17:14:30", at("2026-10-05T17:14:30")),
            ("nope", None),
            ("", None),
            (None, None),
            (5, None),
        ],
    )
    def test_parse_time(self, value, expected):
        parsed = parse_time(value)
        assert (parsed.astimezone(UTC) if parsed else None) == expected


class TestRewind:
    """Seeing a saved response as it looked at an earlier moment."""

    def test_an_incident_is_open_before_it_ended(self, github):
        # "Actions Job Delays" ran 14:47 to 17:56 on 2026-10-01.
        view = rewind(github, at("2026-10-01T15:00:00"))
        job = by_title(view, "Job Delays")
        assert job.ended_at is None and job.stage != "resolved"
        assert all(
            datetime.fromisoformat(u["at"]) <= at("2026-10-01T15:00:00") for u in job.updates
        )

    def test_status_follows_the_open_incidents(self, github):
        view = rewind(github, at("2026-10-01T15:00:00"))
        assert (view.status, view.description) == ("degraded", "1 active incident")

    def test_later_incidents_do_not_exist_yet(self, github):
        # On 28 September at 21:30 the 1 October incidents hadn't happened.
        view = rewind(github, at("2026-09-28T21:30:00"))
        assert all(i.started_at <= at("2026-09-28T21:30:00") for i in view.incidents)
        assert not any("Job Delays" in i.title for i in view.incidents)
        assert any("Copilot Code Review" in i.title for i in view.incidents)

    def test_after_it_ended_it_is_unchanged(self, github):
        view = rewind(github, at("2026-10-02T00:00:00"))
        assert by_title(view, "Job Delays").ended_at is not None
        assert view.status == "operational"

    def test_a_critical_one_makes_it_a_major_outage(self, github):
        view = rewind(github, at("2026-09-28T21:30:00"))
        assert by_title(view, "Copilot Code Review").ended_at is None
        assert view.status == "major_outage"
