"""Gaps the Phase 17 coverage review found in code that has real behaviour: the engine loop's
recovery from a dropped database connection, the status page mail command, and the wording of
the postmortem. (Lines that only exist to talk to the real network are covered by tests/live.)"""

import io
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.db import OperationalError
from django.utils import timezone

from incidents import postmortem
from incidents.models import Incident, IncidentEvent
from monitoring.models import Monitor
from statuspages.models import StatusPage, StatusPageEmail, StatusPageSubscriber

pytestmark = pytest.mark.django_db


def incident_for(user, reason="timeout", status_code=None):
    monitor = Monitor.objects.create(
        user=user,
        name="Shop",
        type="http",
        target="https://example.com",
        interval_seconds=300,
        config={},
        next_check_at=timezone.now(),
    )
    return Incident.objects.create(
        monitor=monitor,
        started_at=timezone.now() - timedelta(hours=1),
        ended_at=timezone.now(),
        reason=reason,
        status_code=status_code,
        resolution="recovered",
    )


class TestPostmortemWording:
    @pytest.mark.parametrize(
        ("seconds", "words"),
        [
            (0, "0 seconds"),
            (59, "59 seconds"),
            (60, "1 minute"),
            (61, "1 minute 1 s"),
            (125, "2 minutes 5 s"),
            (3600, "1 hour"),
            (3660, "1 hour 1 min"),
            (7200, "2 hours"),
            (86_400, "1 day"),
            (90_000, "1 day 1 h"),
            (200_000, "2 days 7 h"),
        ],
    )
    def test_durations_read_like_a_person_wrote_them(self, seconds, words):
        assert postmortem.humanize(seconds) == words

    def test_an_unknown_time_zone_falls_back_to_utc(self):
        assert postmortem._zone("Not/AZone").key == "UTC"
        assert postmortem._zone("Europe/Paris").key == "Europe/Paris"

    def test_a_status_mismatch_names_the_code(self, user):
        incident = incident_for(user, "status_mismatch", 503)
        assert "(503)" in postmortem.reason_text(incident)

    def test_a_reason_with_no_prepared_sentence_is_still_readable(self, user):
        incident = incident_for(user, "some_new_reason")
        assert postmortem.reason_text(incident) == "some new reason"

    def test_every_event_kind_tells_its_part_of_the_story(self, user):
        incident = incident_for(user)

        def line(kind, **meta):
            event = IncidentEvent(
                incident=incident, kind=kind, metadata=meta, created_at=timezone.now()
            )
            return postmortem._event_line(event, incident)

        kinds = IncidentEvent.Kind
        assert "Outage detected" in line(kinds.OPENED, region="tokyo")
        assert "(from the primary region)" in line(kinds.OPENED)
        assert "failing: tokyo, frankfurt" in line(
            kinds.CONFIRMED, regions_failing=["tokyo", "frankfurt"]
        )
        assert "1 minute 30 s after a deploy of api (v7)" in line(
            kinds.DEPLOY_LINKED, service="api", version="v7", seconds_before=90
        )
        assert "a deploy of the service." in line(kinds.DEPLOY_LINKED, seconds_before=5)
        assert line(kinds.RESOLVED) == "Recovered: a check succeeded."
        assert "paused" in line(kinds.CLOSED_PAUSED)
        assert "settings were changed" in line(kinds.CLOSED_EDITED)
        assert line(kinds.EXCLUDED) is None and line(kinds.INCLUDED) is None


class TestEngineLoop:
    def run(self, monkeypatch, loops, fail_first=True):
        """Run `run_engine --loop` for `loops` passes, the first of which loses its database."""
        from monitoring.management.commands import run_engine

        calls = {"n": 0}

        class Summary:
            def as_dict(self):
                return {
                    "claimed": 0, "up": 0, "down": 0, "blips": 0,
                    "sent": 0, "retrying": 0, "failed": 0, "skipped": 0,
                }  # fmt: skip

        def scheduler():
            calls["n"] += 1
            if fail_first and calls["n"] == 1:
                raise OperationalError("server closed the connection unexpectedly")
            return Summary()

        monkeypatch.setattr(run_engine, "run_scheduler_once", scheduler)
        for name in (
            "run_provider_polls_once",
            "run_status_page_mail_once",
            "poll_once",
            "run_rollups_once",
        ):
            monkeypatch.setattr(run_engine, name, lambda: None)
        monkeypatch.setattr(run_engine, "run_notifications_once", lambda: Summary())

        def sleep(_seconds):
            if calls["n"] >= loops:
                raise KeyboardInterrupt

        monkeypatch.setattr(run_engine.time, "sleep", sleep)
        out, err = io.StringIO(), io.StringIO()
        with pytest.raises(KeyboardInterrupt):
            call_command("run_engine", loop=1, stdout=out, stderr=err)
        return calls["n"], out.getvalue(), err.getvalue()

    def test_a_dropped_database_connection_is_reported_and_the_next_pass_goes_ahead(
        self, monkeypatch
    ):
        passes, out, err = self.run(monkeypatch, loops=2)
        assert passes == 2
        assert "pass failed, will retry" in err and "closed the connection" in err
        assert out.count("checks: claimed=0") == 1  # the second pass ran and reported

    def test_a_single_run_without_loop_lets_the_error_through(self, monkeypatch):
        from monitoring.management.commands import run_engine

        def boom():
            raise OperationalError("down")

        monkeypatch.setattr(run_engine, "run_scheduler_once", boom)
        with pytest.raises(OperationalError):
            call_command("run_engine")


class TestStatusPageMailCommand:
    def test_it_sends_what_is_due_and_says_so(self, user):
        page = StatusPage.objects.create(user=user, name="P", slug="p", is_public=True)
        subscriber = StatusPageSubscriber.objects.create(page=page, email="a@example.com")
        StatusPageEmail.objects.create(
            subscriber=subscriber,
            kind="confirm",
            dedupe_key="confirm:1",
            next_attempt_at=timezone.now(),
            payload={"page": "P", "confirm_url": "https://x.example.com/confirm/t"},
        )
        out = io.StringIO()
        call_command("run_status_page_mail_once", stdout=out)
        assert "claimed=1 sent=1 retrying=0 failed=0 skipped=0" in out.getvalue()

    def test_with_nothing_due_it_does_nothing(self):
        out = io.StringIO()
        call_command("run_status_page_mail_once", stdout=out)
        assert "claimed=0 sent=0" in out.getvalue()
