"""Dependency monitoring end to end: the catalogue, ingestion, who gets alerted and when,
replaying a real outage, the poller, and the API. Feeds are real captured responses
(tests/fixtures/providers) served through a stub transport, never the live internet."""

import json
import pathlib
from datetime import UTC, datetime, timedelta
from io import StringIO

import httpx
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone
from rest_framework.test import APIClient

from monitoring.models import AlertContact
from notifications import dispatcher, messages
from notifications.channels import CHANNELS
from notifications.models import NotificationDelivery as Delivery
from providers import catalog, ingest, poller
from providers.adapters import ADAPTERS, FeedResult, ParsedIncident
from providers.models import Provider, ProviderIncident, UserProviderSubscription
from tests.conftest import ORIGIN

pytestmark = pytest.mark.django_db

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "providers"
NOW = datetime(2026, 10, 5, 18, 0, tzinfo=UTC)


def load(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def provider(slug="github"):
    return Provider.objects.get(slug=slug)


def incident_data(
    external_id="inc1",
    *,
    impact="minor",
    ended=None,
    started=None,
    title="Elevated errors",
    stage=None,
    updates=None,
):
    started = started or NOW - timedelta(minutes=30)
    return ParsedIncident(
        external_id=external_id,
        title=title,
        impact=impact,
        stage=stage or ("resolved" if ended else "investigating"),
        started_at=started,
        ended_at=ended,
        url=f"https://status.example.com/{external_id}",
        updates=(
            updates
            if updates is not None
            else [
                {
                    "stage": "investigating",
                    "body": "We are looking into it.",
                    "at": started.isoformat(),
                }
            ]
        ),
        affected=["API"],
    )


def feed(*incidents, status="degraded"):
    return FeedResult(status=status, description="x", incidents=list(incidents))


def slack_contact(user, name="Ops", **kwargs):
    return AlertContact.objects.create(
        user=user,
        name=name,
        channel="slack",
        config={"webhook_url": "https://hooks.slack.com/services/T/B/x"},
        **kwargs,
    )


def subscribe(user, slug="github", contacts=None, min_impact="minor"):
    sub = UserProviderSubscription.objects.create(
        user=user, provider=provider(slug), min_impact=min_impact
    )
    sub.alert_contacts.set(
        contacts if contacts is not None else AlertContact.objects.filter(user=user)
    )
    return sub


@pytest.fixture
def sent(monkeypatch):
    """Every channel's send is recorded instead of performed."""
    box = []
    for key in CHANNELS:
        monkeypatch.setattr(
            CHANNELS[key],
            "send",
            lambda config, payload, delivery_id, key=key: box.append((key, payload)),
        )
    return box


# --- the catalogue ---------------------------------------------------------------------


class TestCatalogue:
    def test_every_deployment_has_the_curated_list_after_migrating(self):
        assert Provider.objects.count() == len(catalog.CATALOG) >= 15

    def test_entries_are_well_formed(self):
        slugs = [e.slug for e in catalog.CATALOG]
        assert len(slugs) == len(set(slugs))
        for entry in catalog.CATALOG:
            assert entry.adapter in ADAPTERS, entry.slug
            assert entry.feed.startswith("https://") and entry.page.startswith("https://")
            assert entry.name and entry.category and entry.description

    def test_it_spans_formats_and_categories(self):
        assert {e.adapter for e in catalog.CATALOG} == {"statuspage", "slack", "gcp"}
        assert len({e.category for e in catalog.CATALOG}) >= 4

    def test_syncing_again_changes_nothing_and_keeps_state(self):
        github = provider()
        github.status, github.consecutive_failures = "degraded", 2
        github.save()
        catalog.sync_catalog()
        catalog.sync_catalog()
        github.refresh_from_db()
        assert (github.status, github.consecutive_failures) == ("degraded", 2)
        assert Provider.objects.count() == len(catalog.CATALOG)

    def test_the_command_reports_it(self):
        out = StringIO()
        call_command("seed_providers", stdout=out)
        assert f"{len(catalog.CATALOG)} providers" in out.getvalue()


# --- ingestion -------------------------------------------------------------------------


class TestIngest:
    def test_stores_the_status_and_incidents(self):
        summary = ingest.ingest(
            provider(),
            feed(incident_data("a"), incident_data("b", ended=NOW, impact="major")),
            now=NOW,
        )
        github = provider()
        assert (github.status, github.consecutive_failures, github.last_success_at) == (
            "degraded",
            0,
            NOW,
        )
        assert summary.created == 2
        one = ProviderIncident.objects.get(external_id="b")
        assert (one.impact, one.ongoing, one.first_seen_at) == ("major", False, NOW)
        assert one.updates and one.affected == ["API"]

    def test_reading_the_same_feed_twice_changes_nothing(self):
        result = feed(incident_data("a"))
        ingest.ingest(provider(), result, now=NOW)
        second = ingest.ingest(provider(), result, now=NOW + timedelta(minutes=5))
        assert (second.created, second.updated) == (0, 0)
        assert ProviderIncident.objects.count() == 1
        assert (
            ProviderIncident.objects.get().first_seen_at == NOW
        )  # remembered from the first sight

    def test_an_incident_that_progresses_is_updated_in_place(self):
        ingest.ingest(provider(), feed(incident_data("a", stage="investigating")), now=NOW)
        ingest.ingest(
            provider(),
            feed(
                incident_data(
                    "a", stage="resolved", ended=NOW + timedelta(minutes=20), impact="major"
                )
            ),
            now=NOW + timedelta(minutes=30),
        )
        row = ProviderIncident.objects.get()
        assert (row.stage, row.impact, row.ongoing) == ("resolved", "major", False)

    def test_an_open_incident_the_feed_no_longer_lists_is_closed(self):
        ingest.ingest(provider(), feed(incident_data("a"), incident_data("b")), now=NOW)
        summary = ingest.ingest(
            provider(), feed(incident_data("b")), now=NOW + timedelta(minutes=10)
        )
        assert summary.closed_missing == 1
        assert not ProviderIncident.objects.get(external_id="a").ongoing
        assert ProviderIncident.objects.get(external_id="b").ongoing

    def test_an_empty_but_healthy_feed_does_close_what_was_open(self):
        ingest.ingest(provider(), feed(incident_data("a")), now=NOW)
        ingest.ingest(provider(), feed(status="operational"), now=NOW + timedelta(minutes=10))
        assert not ProviderIncident.objects.get().ongoing

    def test_an_empty_unhealthy_feed_closes_nothing(self):
        ingest.ingest(provider(), feed(incident_data("a")), now=NOW)
        ingest.ingest(provider(), feed(status="degraded"), now=NOW + timedelta(minutes=10))
        assert ProviderIncident.objects.get().ongoing

    def test_incidents_are_per_provider(self):
        ingest.ingest(provider("github"), feed(incident_data("same")), now=NOW)
        ingest.ingest(provider("vercel"), feed(incident_data("same")), now=NOW)
        assert ProviderIncident.objects.count() == 2

    def test_unreadable_feeds_count_failures_and_eventually_mean_unknown(self, settings):
        github = provider()
        ingest.ingest(github, feed(status="operational"), now=NOW)
        for n in range(1, 4):
            ingest.record_failure(github, "HTTP 500", now=NOW + timedelta(minutes=n))
            github.refresh_from_db()
            assert github.consecutive_failures == n
        assert github.status == "unknown" and "can't reach" in github.status_description
        assert github.last_error == "HTTP 500"
        # and a good read puts everything right
        ingest.ingest(github, feed(status="operational"), now=NOW + timedelta(minutes=10))
        github.refresh_from_db()
        assert (github.status, github.consecutive_failures, github.last_error) == (
            "operational",
            0,
            "",
        )

    def test_one_failure_does_not_blank_a_good_status(self):
        github = provider()
        ingest.ingest(github, feed(status="operational"), now=NOW)
        ingest.record_failure(github, "Timed out", now=NOW)
        github.refresh_from_db()
        assert github.status == "operational"


# --- who is alerted, and when ----------------------------------------------------------


class TestAlerts:
    def test_a_new_incident_alerts_everyone_tracking_the_provider(self, user, make_user):
        mine = slack_contact(user)
        other = make_user("bob@example.com")
        theirs = slack_contact(other, "Theirs")
        subscribe(user, contacts=[mine])
        subscribe(other, contacts=[theirs])
        summary = ingest.ingest(provider(), feed(incident_data("a")), now=NOW)
        assert summary.alerts == 2
        assert {d.contact for d in Delivery.objects.all()} == {mine, theirs}
        delivery = Delivery.objects.get(contact=mine)
        assert (delivery.event, delivery.status) == ("opened", "pending")
        assert (
            delivery.payload["kind"] == "provider" and delivery.provider_incident.external_id == "a"
        )

    def test_nobody_tracking_means_nobody_alerted(self, user):
        slack_contact(user)
        assert ingest.ingest(provider(), feed(incident_data("a")), now=NOW).alerts == 0
        assert not Delivery.objects.exists()

    def test_other_providers_do_not_alert_you(self, user):
        subscribe(user, "vercel", [slack_contact(user)])
        assert ingest.ingest(provider("github"), feed(incident_data("a")), now=NOW).alerts == 0

    def test_only_the_chosen_contacts_are_used(self, user):
        chosen, skipped = slack_contact(user, "Chosen"), slack_contact(user, "Skipped")
        subscribe(user, contacts=[chosen])
        ingest.ingest(provider(), feed(incident_data("a")), now=NOW)
        assert [d.contact for d in Delivery.objects.all()] == [chosen]
        assert skipped

    def test_off_and_unconfirmed_contacts_are_left_out(self, user):
        off = slack_contact(user, "Off", enabled=False)
        unconfirmed = AlertContact.objects.create(
            user=user, name="Mail", channel="email", config={"address": "boss@example.com"}
        )
        good = slack_contact(user, "Good")
        subscribe(user, contacts=[off, unconfirmed, good])
        ingest.ingest(provider(), feed(incident_data("a")), now=NOW)
        assert [d.contact for d in Delivery.objects.all()] == [good]

    def test_alerting_twice_for_the_same_incident_is_impossible(self, user):
        subscribe(user, contacts=[slack_contact(user)])
        for minutes in (0, 5, 10):
            ingest.ingest(
                provider(), feed(incident_data("a")), now=NOW + timedelta(minutes=minutes)
            )
        assert Delivery.objects.count() == 1

    @pytest.mark.parametrize(
        ("min_impact", "impact", "alerted"),
        [
            ("none", "none", True),
            ("none", "minor", True),
            ("minor", "none", False),
            ("minor", "minor", True),
            ("minor", "critical", True),
            ("major", "minor", False),
            ("major", "major", True),
            ("major", "critical", True),
        ],
    )
    def test_the_minimum_impact_decides(self, user, min_impact, impact, alerted):
        subscribe(user, contacts=[slack_contact(user)], min_impact=min_impact)
        ingest.ingest(provider(), feed(incident_data("a", impact=impact)), now=NOW)
        assert Delivery.objects.exists() is alerted

    def test_an_incident_that_escalates_alerts_those_who_only_wanted_serious_ones(self, user):
        subscribe(user, contacts=[slack_contact(user)], min_impact="major")
        ingest.ingest(provider(), feed(incident_data("a", impact="minor")), now=NOW)
        assert not Delivery.objects.exists()
        ingest.ingest(
            provider(), feed(incident_data("a", impact="major")), now=NOW + timedelta(minutes=10)
        )
        assert Delivery.objects.count() == 1

    def test_old_news_does_not_alert(self, user, settings):
        subscribe(user, contacts=[slack_contact(user)])
        old = incident_data(
            "a", started=NOW - timedelta(hours=settings.PROVIDER_NOTIFY_MAX_AGE_HOURS + 1)
        )
        ingest.ingest(provider(), feed(old), now=NOW)  # still "open" on their page, weeks later
        assert not Delivery.objects.exists()

    def test_an_incident_that_was_already_over_when_first_seen_alerts_nobody(self, user):
        subscribe(user, contacts=[slack_contact(user)])
        ingest.ingest(
            provider(), feed(incident_data("a", ended=NOW - timedelta(minutes=5))), now=NOW
        )
        assert not Delivery.objects.exists()

    def test_recovery_is_announced_only_to_those_told_it_began(self, user, sent):
        told, waiting = slack_contact(user, "Told"), slack_contact(
            user, "Waiting", delay_seconds=3600
        )
        subscribe(user, contacts=[told, waiting])
        ingest.ingest(provider(), feed(incident_data("a")), now=NOW)
        dispatcher.run_notifications_once(
            now=NOW, workers=1
        )  # "Told" is sent; "Waiting" is not due
        ingest.ingest(
            provider(),
            feed(incident_data("a", ended=NOW + timedelta(minutes=20))),
            now=NOW + timedelta(minutes=20),
        )
        by_contact = {(d.contact.name, d.event): d.status for d in Delivery.objects.all()}
        assert by_contact[("Told", "opened")] == "sent"
        assert by_contact[("Told", "resolved")] == "pending"
        assert by_contact[("Waiting", "opened")] == "skipped"  # recovered before it was ever sent
        assert ("Waiting", "resolved") not in by_contact

    def test_a_start_alert_that_never_went_out_is_not_followed_by_an_all_clear(self, user):
        subscribe(user, contacts=[slack_contact(user)])
        ingest.ingest(provider(), feed(incident_data("a")), now=NOW)
        ingest.ingest(
            provider(),
            feed(incident_data("a", ended=NOW + timedelta(minutes=2))),
            now=NOW + timedelta(minutes=2),
        )
        assert [(d.event, d.status) for d in Delivery.objects.all()] == [("opened", "skipped")]

    def test_a_contacts_delay_is_respected(self, user, sent):
        subscribe(user, contacts=[slack_contact(user, delay_seconds=600)])
        ingest.ingest(provider(), feed(incident_data("a")), now=NOW)
        assert (
            dispatcher.run_notifications_once(now=NOW + timedelta(minutes=5), workers=1).claimed
            == 0
        )
        assert (
            dispatcher.run_notifications_once(now=NOW + timedelta(minutes=11), workers=1).sent == 1
        )

    def test_an_alert_still_waiting_when_the_incident_ends_is_skipped_by_the_dispatcher_too(
        self, user, sent
    ):
        subscribe(user, contacts=[slack_contact(user)])
        ingest.ingest(provider(), feed(incident_data("a")), now=NOW)
        ProviderIncident.objects.update(
            ended_at=NOW + timedelta(minutes=1)
        )  # ended behind reconcile's back
        dispatcher.run_notifications_once(now=NOW + timedelta(minutes=2), workers=1)
        assert sent == [] and Delivery.objects.get().status == "skipped"

    def test_stopping_tracking_cancels_what_is_waiting(self, user):
        sub = subscribe(user, contacts=[slack_contact(user, delay_seconds=3600)])
        ingest.ingest(provider(), feed(incident_data("a")), now=NOW)
        assert ingest.cancel_pending(sub) == 1
        delivery = Delivery.objects.get()
        assert delivery.status == "skipped" and "stopped tracking" in delivery.error

    def test_the_alert_says_what_happened_in_plain_words(self, user):
        subscribe(user, contacts=[slack_contact(user)])
        ingest.ingest(
            provider(),
            feed(incident_data("a", impact="major", title="Git operations are failing")),
            now=NOW,
        )
        payload = Delivery.objects.get().payload
        assert payload["title"] == "GitHub: Git operations are failing"
        assert payload["severity"] == "down"
        assert payload["summary"].startswith(
            "Major impact · Investigating. We are looking into it."
        )
        assert payload["url"] == "https://status.example.com/a"
        assert payload["monitor"] == {
            "id": "github",
            "name": "GitHub",
            "type": "provider",
            "target": "https://www.githubstatus.com",
        }
        assert payload["provider"]["affected"] == ["API"]

    def test_minor_incidents_are_a_warning_and_resolutions_are_good_news(self):
        minor = ProviderIncident(
            provider=provider(),
            external_id="m",
            title="Slow",
            impact="minor",
            stage="investigating",
            started_at=NOW - timedelta(hours=1),
            first_seen_at=NOW,
        )
        assert messages.build_provider("opened", minor, now=NOW)["severity"] == "warning"
        minor.ended_at = NOW
        resolved = messages.build_provider("resolved", minor, now=NOW)
        assert resolved["severity"] == "up" and "resolved after 1 h" in resolved["summary"]
        assert resolved["title"] == "GitHub: Slow (resolved)"

    def test_an_alert_for_a_provider_with_no_link_uses_its_status_page(self):
        bare = ProviderIncident(
            provider=provider(),
            external_id="m",
            title="Slow",
            impact="minor",
            stage="investigating",
            started_at=NOW,
            first_seen_at=NOW,
            url="",
        )
        assert (
            messages.build_provider("opened", bare, now=NOW)["url"]
            == "https://www.githubstatus.com"
        )

    def test_every_channel_can_send_a_provider_alert(self, user, monkeypatch):
        """The payload has what each channel reads (they were written for monitor alerts)."""
        import httpx as _httpx

        from notifications import channels as ch

        subscribe(user, contacts=[slack_contact(user)])
        ingest.ingest(provider(), feed(incident_data("a")), now=NOW)
        payload = Delivery.objects.get().payload
        seen = []
        monkeypatch.setattr(
            ch,
            "post_json",
            lambda url, body, headers, **k: seen.append(json.loads(body))
            or _httpx.Response(200, json={}),
        )
        monkeypatch.setattr(
            ch, "safe_post", lambda url, body, headers: seen.append(json.loads(body)) or (200, "")
        )
        CHANNELS["slack"].send(
            {"webhook_url": "https://hooks.slack.com/services/T/B/x"}, payload, 1
        )
        CHANNELS["discord"].send(
            {"webhook_url": "https://discord.com/api/webhooks/1/abc"}, payload, 2
        )
        CHANNELS["telegram"].send(
            {"bot_token": "123456789:" + "A" * 35, "chat_id": "5"}, payload, 3
        )
        CHANNELS["webhook"].send({"url": "https://hooks.example.com/x", "secret": "s"}, payload, 4)
        assert len(seen) == 4
        assert (
            seen[3]["event"] == "provider_incident.opened"
            and seen[3]["provider"]["slug"] == "github"
        )
        assert "GitHub" in json.dumps(seen[0]) and "GitHub" in json.dumps(seen[1])

    def test_the_delivery_history_names_the_provider(self, auth_api, user):
        subscribe(user, contacts=[slack_contact(user)])
        ingest.ingest(provider(), feed(incident_data("a")), now=NOW)
        row = auth_api.get("/api/v1/notifications/").json()["results"][0]
        assert row["monitor_name"] == "GitHub" and row["event"] == "opened"


# --- replaying a real outage -----------------------------------------------------------


class TestReplayingARealOutage:
    """GitHub's real "Actions Job Delays" incident: 14:47 to 17:56 UTC on 1 October 2026."""

    def replay(self, as_of, slug="github"):
        out = StringIO()
        call_command(
            "replay_provider_feed",
            slug,
            f"--doc=summary={FIXTURES / 'github_summary.json'}",
            f"--doc=incidents={FIXTURES / 'github_incidents.json'}",
            f"--as-of={as_of}",
            stdout=out,
        )
        return out.getvalue()

    def test_it_shows_up_and_notifies_while_it_is_happening_and_again_when_it_ends(
        self, user, sent
    ):
        subscribe(user, contacts=[slack_contact(user, "Ops room")])

        # 15:00: the outage is 13 minutes old, so it is news.
        report = self.replay("2026-10-01T15:00:00Z")
        assert "1 alerts queued" in report
        job = ProviderIncident.objects.get(title="Actions Job Delays")
        assert job.ongoing and job.impact == "minor" and job.affected == ["Actions"]
        assert provider().status == "degraded"
        dispatcher.run_notifications_once(now=at("2026-10-01T15:00:30"), workers=1)
        assert [(c, p["event"], p["title"]) for c, p in sent] == [
            ("slack", "opened", "GitHub: Actions Job Delays")
        ]
        summary = sent[0][1]["summary"]
        assert summary.startswith("Minor impact") and "Actions" in summary

        # 18:00: it has been resolved, and the subscriber who was told is told it is over.
        self.replay("2026-10-01T18:00:00Z")
        job.refresh_from_db()
        assert not job.ongoing and job.ended_at.hour == 17
        assert provider().status == "operational"
        dispatcher.run_notifications_once(now=at("2026-10-01T18:00:30"), workers=1)
        assert [p["event"] for _, p in sent] == ["opened", "resolved"]
        assert "resolved after 3 h 9 min" in sent[1][1]["summary"]

    def test_replaying_the_same_moment_twice_alerts_once(self, user):
        subscribe(user, contacts=[slack_contact(user)])
        self.replay("2026-10-01T15:00:00Z")
        self.replay("2026-10-01T15:00:00Z")
        assert Delivery.objects.count() == 1

    def test_a_week_later_it_is_history_and_alerts_nobody(self, user):
        subscribe(user, contacts=[slack_contact(user)])
        self.replay("2026-10-08T12:00:00Z")
        assert not Delivery.objects.exists()
        assert ProviderIncident.objects.filter(
            title="Actions Job Delays", ended_at__isnull=False
        ).exists()

    def test_a_real_critical_outage_is_a_major_outage_with_a_loud_alert(self, user):
        subscribe(user, contacts=[slack_contact(user)])
        self.replay("2026-09-28T21:30:00Z")
        assert provider().status == "major_outage"
        assert Delivery.objects.get().payload["severity"] == "down"
        assert "Copilot Code Review" in Delivery.objects.get().payload["title"]

    def test_google_clouds_format_replays_the_same_way(self, user):
        subscribe(user, "gcp", [slack_contact(user)])
        out = StringIO()
        call_command(
            "replay_provider_feed",
            "gcp",
            f"--doc=incidents={FIXTURES / 'gcp_incidents.json'}",
            "--as-of=2026-08-20T16:00:00Z",
            stdout=out,
        )
        outage = ProviderIncident.objects.get(external_id="utF3FMFdQfwBzJcGG6vf")
        assert outage.ongoing and outage.impact == "critical"
        assert Delivery.objects.get().payload["severity"] == "down"

    def test_slacks_format_replays_the_same_way(self, user):
        subscribe(user, "slack", [slack_contact(user)])
        call_command(
            "replay_provider_feed",
            "slack",
            f"--doc=current={FIXTURES / 'slack_current.json'}",
            f"--doc=history={FIXTURES / 'slack_history.json'}",
            "--as-of=2026-10-01T06:00:00Z",
        )
        # 05:39 PDT is 12:39 UTC, so at 06:00 UTC nothing had happened; at 13:00 UTC it had.
        assert not Delivery.objects.exists()
        call_command(
            "replay_provider_feed",
            "slack",
            f"--doc=current={FIXTURES / 'slack_current.json'}",
            f"--doc=history={FIXTURES / 'slack_history.json'}",
            "--as-of=2026-10-01T13:00:00Z",
        )
        assert Delivery.objects.get().payload["title"].startswith("Slack: Free Plan Customers")

    def test_a_bad_command_says_what_is_wrong(self):
        with pytest.raises(CommandError, match="No provider"):
            call_command("replay_provider_feed", "nope")
        with pytest.raises(CommandError, match="NAME=PATH"):
            call_command("replay_provider_feed", "github", "--doc=oops")
        with pytest.raises(CommandError, match="valid"):
            call_command(
                "replay_provider_feed", "github", f"--doc=summary={FIXTURES / 'slack_current.json'}"
            )


# --- the poller ------------------------------------------------------------------------


def serve(routes):
    """A client whose responses come from `routes`: {url: (status, body)}; anything else 404s."""

    def handler(request):
        status, body = routes.get(str(request.url), (404, {}))
        content = body if isinstance(body, (bytes, str)) else json.dumps(body)
        return httpx.Response(status, content=content)

    return httpx.Client(transport=httpx.MockTransport(handler))


def github_routes(summary=None, incidents=None, status=200):
    return {
        "https://www.githubstatus.com/api/v2/summary.json": (
            status,
            summary or load("github_summary"),
        ),
        "https://www.githubstatus.com/api/v2/incidents.json": (
            status,
            incidents or load("github_incidents"),
        ),
    }


@pytest.fixture
def only_github():
    Provider.objects.exclude(slug="github").update(enabled=False)


class TestPoller:
    def test_reads_a_provider_and_stores_what_it_says(self, only_github):
        summary = poller.run_provider_polls_once(now=NOW, client=serve(github_routes()), workers=1)
        assert (summary.claimed, summary.ok, summary.failed) == (1, 1, 0)
        assert summary.new_incidents == 6
        github = provider()
        assert (
            github.status == "operational"
            and github.component_count == 12
            and github.last_success_at == NOW
        )
        assert ProviderIncident.objects.count() == 6

    def test_a_tracked_provider_is_read_soon_and_an_untracked_one_rarely(
        self, user, only_github, settings
    ):
        poller.run_provider_polls_once(now=NOW, client=serve(github_routes()), workers=1)
        assert provider().next_poll_at == NOW + timedelta(
            seconds=settings.PROVIDER_IDLE_POLL_SECONDS
        )
        subscribe(user, contacts=[])
        poller.run_provider_polls_once(
            now=NOW + timedelta(hours=1), client=serve(github_routes()), workers=1
        )
        assert provider().next_poll_at == NOW + timedelta(
            hours=1, seconds=settings.PROVIDER_POLL_SECONDS
        )

    def test_providers_are_only_read_when_due(self, only_github):
        client = serve(github_routes())
        poller.run_provider_polls_once(now=NOW, client=client, workers=1)
        assert (
            poller.run_provider_polls_once(
                now=NOW + timedelta(minutes=1), client=client, workers=1
            ).claimed
            == 0
        )

    def test_disabled_providers_are_left_alone(self):
        Provider.objects.update(enabled=False)
        assert poller.run_provider_polls_once(now=NOW, client=serve({}), workers=1).claimed == 0

    def test_a_claimed_provider_is_not_claimed_again_while_leased(self, only_github):
        assert len(poller.claim_due(NOW, 10)) == 1
        assert poller.claim_due(NOW, 10) == []
        assert len(poller.claim_due(NOW + poller.LEASE + timedelta(seconds=1), 10)) == 1

    @pytest.mark.parametrize("status", [404, 500, 503])
    def test_an_http_error_is_a_failure_with_backoff(self, only_github, settings, status):
        summary = poller.run_provider_polls_once(
            now=NOW, client=serve(github_routes(status=status)), workers=1
        )
        assert (summary.ok, summary.failed) == (0, 1)
        github = provider()
        assert github.consecutive_failures == 1 and github.last_error == f"HTTP {status}"
        assert github.next_poll_at == NOW + timedelta(seconds=settings.PROVIDER_POLL_SECONDS * 2)

    def test_backoff_grows_and_is_capped(self, only_github, settings):
        client = serve(github_routes(status=500))
        now = NOW
        waits = []
        for _ in range(6):
            poller.run_provider_polls_once(now=now, client=client, workers=1)
            waits.append((provider().next_poll_at - now).total_seconds())
            now = provider().next_poll_at + timedelta(seconds=1)
        assert waits == sorted(waits) and waits[-1] == 3600

    def test_three_failures_make_the_status_unknown(self, only_github):
        client = serve(github_routes(status=500))
        now = NOW
        for _ in range(3):
            poller.run_provider_polls_once(now=now, client=client, workers=1)
            now = provider().next_poll_at + timedelta(seconds=1)
        assert provider().status == "unknown"

    def test_the_history_is_optional_but_the_summary_is_not(self, only_github):
        routes = github_routes()
        del routes["https://www.githubstatus.com/api/v2/incidents.json"]
        assert poller.run_provider_polls_once(now=NOW, client=serve(routes), workers=1).ok == 1
        routes = github_routes()
        del routes["https://www.githubstatus.com/api/v2/summary.json"]
        assert (
            poller.run_provider_polls_once(
                now=NOW + timedelta(hours=1), client=serve(routes), workers=1
            ).failed
            == 1
        )

    def test_garbage_is_a_failure_not_a_crash(self, only_github):
        routes = {u: (200, "<html>maintenance</html>") for u in github_routes()}
        assert poller.run_provider_polls_once(now=NOW, client=serve(routes), workers=1).failed == 1
        assert provider().last_error == "Not JSON"

    def test_a_feed_in_the_wrong_format_is_a_failure(self, only_github):
        routes = github_routes(summary={"unexpected": True})
        assert poller.run_provider_polls_once(now=NOW, client=serve(routes), workers=1).failed == 1

    def test_an_oversized_feed_is_refused(self, only_github, monkeypatch):
        monkeypatch.setattr(poller, "MAX_BYTES", 100)
        assert (
            poller.run_provider_polls_once(now=NOW, client=serve(github_routes()), workers=1).failed
            == 1
        )
        assert provider().last_error == "The feed is too large."

    def test_timeouts_and_connection_errors_are_failures(self, only_github):
        def slow(request):
            raise httpx.ReadTimeout("slow")

        client = httpx.Client(transport=httpx.MockTransport(slow))
        assert poller.run_provider_polls_once(now=NOW, client=client, workers=1).failed == 1
        assert provider().last_error == "Timed out"

    def test_one_broken_provider_does_not_stop_the_others(self):
        Provider.objects.exclude(slug__in=["github", "npm"]).update(enabled=False)
        routes = github_routes()  # npm has no routes: 404
        summary = poller.run_provider_polls_once(now=NOW, client=serve(routes), workers=1)
        assert (summary.claimed, summary.ok, summary.failed) == (2, 1, 1)
        assert (
            provider("github").status == "operational" and provider("npm").consecutive_failures == 1
        )

    def test_a_crash_in_one_adapter_is_contained(self, monkeypatch, only_github):
        def boom(self, docs):
            raise RuntimeError("bug")

        monkeypatch.setattr(ADAPTERS["statuspage"].__class__, "parse", boom)
        summary = poller.run_provider_polls_once(now=NOW, client=serve(github_routes()), workers=1)
        assert summary.failed == 1 and provider().last_error == "Unexpected error"

    def test_an_unknown_format_is_a_failure(self, only_github):
        Provider.objects.filter(slug="github").update(adapter="nope")
        assert (
            poller.run_provider_polls_once(now=NOW, client=serve(github_routes()), workers=1).failed
            == 1
        )

    def test_running_out_of_time_hands_the_rest_back(self, only_github):
        summary = poller.run_provider_polls_once(
            now=NOW, client=serve(github_routes()), workers=1, time_budget=-1
        )
        assert summary.claimed == 1 and summary.ok == 0
        assert provider().next_poll_at == NOW  # due again straight away

    def test_a_poll_that_finds_trouble_alerts_subscribers_in_the_same_pass(
        self, user, only_github, sent
    ):
        subscribe(user, contacts=[slack_contact(user)])
        live = load("cloudflare_summary")
        routes = {
            "https://www.githubstatus.com/api/v2/summary.json": (200, live),
            "https://www.githubstatus.com/api/v2/incidents.json": (200, {"incidents": []}),
        }
        summary = poller.run_provider_polls_once(
            now=at("2026-10-05T18:00:00"), client=serve(routes), workers=1
        )
        assert summary.alerts >= 1  # Cloudflare's real, ongoing incidents, read as if GitHub's
        dispatcher.run_notifications_once(now=at("2026-10-05T18:00:10"), workers=1)
        assert sent and sent[0][1]["kind"] == "provider"

    def test_the_command_reports_a_summary(self, only_github, monkeypatch):
        monkeypatch.setattr(poller, "new_client", lambda: serve(github_routes()))
        out = StringIO()
        call_command("run_provider_polls_once", stdout=out)
        assert "claimed=1 ok=1 failed=0 new_incidents=6" in out.getvalue()


# --- the API ---------------------------------------------------------------------------


PROVIDERS = "/api/v1/providers/"
FEED = "/api/v1/provider-incidents/"


@pytest.fixture
def other_api(make_user, sign_in):
    client = APIClient(HTTP_ORIGIN=ORIGIN)
    person = make_user("mallory@example.com")
    client.credentials(
        HTTP_AUTHORIZATION=f"Bearer {sign_in(client, person.email).json()['access_token']}"
    )
    client.user = person
    return client


class TestProviderApi:
    def test_lists_the_curated_providers(self, auth_api):
        rows = auth_api.get(PROVIDERS).json()
        assert len(rows) == len(catalog.CATALOG)
        github = next(r for r in rows if r["slug"] == "github")
        assert github["name"] == "GitHub" and github["category"] == "Developer tools"
        assert github["subscribed"] is False and github["subscription"] is None
        assert github["status"] == "unknown" and github["stale"] is True  # never read yet

    def test_search_and_filters(self, auth_api):
        assert [r["slug"] for r in auth_api.get(f"{PROVIDERS}?q=github").json()] == ["github"]
        assert {r["category"] for r in auth_api.get(f"{PROVIDERS}?category=AI").json()} == {"AI"}
        assert auth_api.get(f"{PROVIDERS}?q=zzzz").json() == []
        assert [r["slug"] for r in auth_api.get(f"{PROVIDERS}?q=jira").json()] == [
            "atlassian"
        ]  # by what it does

    def test_shows_current_status_and_ongoing_incidents(self, auth_api):
        ingest.ingest(
            provider(),
            feed(incident_data("a"), incident_data("b", ended=NOW), status="partial_outage"),
            now=timezone.now(),
        )
        github = next(r for r in auth_api.get(PROVIDERS).json() if r["slug"] == "github")
        assert (github["status"], github["ongoing_incidents"], github["stale"]) == (
            "partial_outage",
            1,
            False,
        )

    def test_a_provider_we_cannot_read_is_marked_stale(self, auth_api):
        ingest.ingest(provider(), feed(status="operational"), now=timezone.now())
        for _ in range(3):
            ingest.record_failure(provider(), "HTTP 500")
        github = next(r for r in auth_api.get(PROVIDERS).json() if r["slug"] == "github")
        assert github["stale"] is True and github["status"] == "unknown"

    def test_detail_has_components_and_recent_incidents(self, auth_api):
        summary = FeedResult(
            status="partial_outage",
            description="x",
            components=[{"name": "Actions", "status": "major_outage"}],
            component_count=12,
            incidents=[incident_data("a")],
        )
        ingest.ingest(provider(), summary, now=timezone.now())
        body = auth_api.get(f"{PROVIDERS}github/").json()
        assert (
            body["components"] == [{"name": "Actions", "status": "major_outage"}]
            and body["component_count"] == 12
        )
        assert body["recent_incidents"][0]["title"] == "Elevated errors"
        assert body["recent_incidents"][0]["updates"][0]["body"] == "We are looking into it."

    def test_unknown_providers_404(self, auth_api):
        assert auth_api.get(f"{PROVIDERS}nope/").status_code == 404
        assert auth_api.post(f"{PROVIDERS}nope/subscribe/").status_code == 404

    def test_requires_sign_in(self, api):
        assert api.get(PROVIDERS).status_code == 401
        assert api.get(FEED).status_code == 401
        assert api.post(f"{PROVIDERS}github/subscribe/").status_code == 401


class TestSubscribing:
    def test_tracking_a_provider_alerts_all_your_contacts_by_default(self, auth_api, user):
        a, b = slack_contact(user, "A"), slack_contact(user, "B")
        resp = auth_api.post(f"{PROVIDERS}github/subscribe/", {}, format="json")
        assert resp.status_code == 201
        body = resp.json()
        assert body["subscribed"] is True
        assert body["subscription"]["min_impact"] == "minor"
        assert sorted(body["subscription"]["alert_contact_ids"]) == sorted([str(a.id), str(b.id)])

    def test_or_exactly_the_contacts_and_threshold_you_choose(self, auth_api, user):
        a, _ = slack_contact(user, "A"), slack_contact(user, "B")
        body = auth_api.post(
            f"{PROVIDERS}github/subscribe/",
            {"alert_contact_ids": [str(a.id)], "min_impact": "major"},
            format="json",
        ).json()
        assert (
            body["subscription"]["alert_contact_ids"] == [str(a.id)]
            and body["subscription"]["min_impact"] == "major"
        )

    def test_changing_it_later_keeps_one_subscription(self, auth_api, user):
        contact = slack_contact(user)
        auth_api.post(f"{PROVIDERS}github/subscribe/", {}, format="json")
        resp = auth_api.post(
            f"{PROVIDERS}github/subscribe/",
            {"min_impact": "none", "alert_contact_ids": []},
            format="json",
        )
        assert resp.status_code == 200
        assert UserProviderSubscription.objects.count() == 1
        assert resp.json()["subscription"] == {
            **resp.json()["subscription"],
            "min_impact": "none",
            "alert_contact_ids": [],
        }
        assert contact

    def test_changing_the_threshold_alone_keeps_the_contacts(self, auth_api, user):
        contact = slack_contact(user)
        auth_api.post(f"{PROVIDERS}github/subscribe/", {}, format="json")
        body = auth_api.post(
            f"{PROVIDERS}github/subscribe/", {"min_impact": "major"}, format="json"
        ).json()
        assert body["subscription"]["alert_contact_ids"] == [str(contact.id)]

    def test_you_cannot_attach_someone_elses_contact(self, auth_api, other_api):
        theirs = slack_contact(other_api.user, "Theirs")
        resp = auth_api.post(
            f"{PROVIDERS}github/subscribe/", {"alert_contact_ids": [str(theirs.id)]}, format="json"
        )
        assert resp.status_code == 400 and not UserProviderSubscription.objects.exists()

    def test_the_threshold_must_be_one_we_know(self, auth_api):
        assert (
            auth_api.post(
                f"{PROVIDERS}github/subscribe/", {"min_impact": "catastrophic"}, format="json"
            ).status_code
            == 400
        )

    def test_tracking_something_already_going_wrong_alerts_you_straight_away(self, auth_api, user):
        slack_contact(user)
        ingest.ingest(
            provider(),
            feed(incident_data("a", started=timezone.now() - timedelta(minutes=20))),
            now=timezone.now(),
        )
        auth_api.post(f"{PROVIDERS}github/subscribe/", {}, format="json")
        assert (
            Delivery.objects.filter(provider_incident__external_id="a", event="opened").count() == 1
        )

    def test_but_not_for_old_news(self, auth_api, user):
        slack_contact(user)
        ingest.ingest(
            provider(),
            feed(incident_data("a", started=timezone.now() - timedelta(days=30))),
            now=timezone.now(),
        )
        auth_api.post(f"{PROVIDERS}github/subscribe/", {}, format="json")
        assert not Delivery.objects.exists()

    def test_the_filter_shows_only_what_you_track(self, auth_api, user):
        auth_api.post(f"{PROVIDERS}github/subscribe/", {}, format="json")
        auth_api.post(f"{PROVIDERS}vercel/subscribe/", {}, format="json")
        assert {r["slug"] for r in auth_api.get(f"{PROVIDERS}?subscribed=true").json()} == {
            "github",
            "vercel",
        }

    def test_stopping_is_idempotent_and_cancels_waiting_alerts(self, auth_api, user):
        slack_contact(user, delay_seconds=3600)
        ingest.ingest(
            provider(), feed(incident_data("a", started=timezone.now())), now=timezone.now()
        )
        auth_api.post(f"{PROVIDERS}github/subscribe/", {}, format="json")
        assert auth_api.delete(f"{PROVIDERS}github/subscribe/").status_code == 204
        assert auth_api.delete(f"{PROVIDERS}github/subscribe/").status_code == 204
        assert not UserProviderSubscription.objects.exists()
        assert Delivery.objects.get().status == "skipped"

    def test_subscriptions_are_private(self, auth_api, other_api):
        auth_api.post(f"{PROVIDERS}github/subscribe/", {}, format="json")
        mine = next(r for r in auth_api.get(PROVIDERS).json() if r["slug"] == "github")
        theirs = next(r for r in other_api.get(PROVIDERS).json() if r["slug"] == "github")
        assert mine["subscribed"] is True and theirs["subscribed"] is False
        assert other_api.get(f"{FEED}").json()["results"] == []


class TestIncidentFeed:
    def seed(self, user):
        subscribe(user, "github", [])
        ingest.ingest(
            provider("github"),
            feed(
                incident_data("g-open", impact="major", title="Git down"),
                incident_data("g-done", ended=NOW, started=NOW - timedelta(hours=5)),
            ),
            now=NOW,
        )
        ingest.ingest(
            provider("vercel"), feed(incident_data("v-open", title="Builds slow")), now=NOW
        )

    def titles(self, resp):
        return [r["title"] for r in resp.json()["results"]]

    def test_shows_incidents_of_the_providers_you_track_newest_first(self, auth_api, user):
        self.seed(user)
        assert self.titles(auth_api.get(FEED)) == ["Git down", "Elevated errors"]

    def test_or_everyone_s(self, auth_api, user):
        self.seed(user)
        assert len(auth_api.get(f"{FEED}?scope=all").json()["results"]) == 3

    def test_filters(self, auth_api, user):
        self.seed(user)
        assert self.titles(auth_api.get(f"{FEED}?status=ongoing")) == ["Git down"]
        assert self.titles(auth_api.get(f"{FEED}?status=resolved")) == ["Elevated errors"]
        assert self.titles(auth_api.get(f"{FEED}?scope=all&provider=vercel")) == ["Builds slow"]
        assert auth_api.get(f"{FEED}?status=bogus").status_code == 400
        assert auth_api.get(f"{FEED}?scope=bogus").status_code == 400

    def test_each_row_says_who_and_for_how_long(self, auth_api, user):
        self.seed(user)
        row = auth_api.get(FEED).json()["results"][0]
        assert row["provider"] == {
            "slug": "github",
            "name": "GitHub",
            "category": "Developer tools",
            "status_page_url": "https://www.githubstatus.com",
        }
        assert (row["impact"], row["ongoing"], row["affected"]) == ("major", True, ["API"])
        assert row["duration_seconds"] >= 1800 and row["url"] == "https://status.example.com/g-open"

    def test_is_paged(self, auth_api, user):
        subscribe(user, "github", [])
        ingest.ingest(
            provider(),
            feed(
                *[
                    incident_data(f"i{n}", ended=NOW, started=NOW - timedelta(hours=n + 2))
                    for n in range(30)
                ]
            ),
            now=NOW,
        )
        body = auth_api.get(FEED).json()
        assert body["count"] == 30 and len(body["results"]) == 25 and body["next"]
