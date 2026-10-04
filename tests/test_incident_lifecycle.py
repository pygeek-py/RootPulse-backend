import ipaddress
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from incidents import services
from incidents.models import Incident, IncidentEvent
from monitoring import probers
from monitoring.checks import result as r
from monitoring.checks.http_check import HttpChecker
from monitoring.engine import Engine, run_scheduler_once
from monitoring.models import Check, Monitor

pytestmark = pytest.mark.django_db

UP = r.CheckResult(success=True, status_detail=r.OK, status_code=200, duration_ms=120)
DOWN = r.CheckResult(
    success=False, status_detail=r.STATUS_MISMATCH, status_code=503, duration_ms=80
)
UNSURE = r.CheckResult.inconclusive(r.BLOCKED_TARGET)


def make(user, name="m", **kwargs):
    fields = {
        "type": "http",
        "target": "https://example.com",
        "interval_seconds": 300,
        "config": {},
        "status": "up",
        "next_check_at": timezone.now() - timedelta(minutes=1),
    }
    fields.update(kwargs)
    return Monitor.objects.create(user=user, name=name, **fields)


def script(*outcomes):
    calls = []

    def runner(monitor):
        outcome = outcomes[min(len(calls), len(outcomes) - 1)]
        calls.append(1)
        return (outcome, None)

    runner.calls = calls
    return runner


def run(runner, monitor=None):
    """One pass; the monitor is made due first."""
    Monitor.objects.update(next_check_at=timezone.now() - timedelta(minutes=1))
    return run_scheduler_once(engine=Engine(runner=runner, sleep=lambda s: None))


def incidents_of(monitor):
    return list(Incident.objects.filter(monitor=monitor).order_by("started_at"))


class TestOpening:
    def test_a_confirmed_failure_opens_exactly_one_incident(self, user):
        monitor = make(user)
        run(script(DOWN, DOWN))  # failure, then the local re-check also fails

        monitor.refresh_from_db()
        assert monitor.status == "down"
        incident = Incident.objects.get()
        assert incident.monitor == monitor and incident.ongoing
        assert incident.reason == "status_mismatch" and incident.status_code == 503
        assert incident.opened_by.success is False and incident.opened_by.confirmation is False
        kinds = list(incident.events.values_list("kind", flat=True))
        assert (
            kinds[0] == IncidentEvent.Kind.OPENED
            and incident.events.first().metadata["status_code"] == 503
        )

    def test_a_blip_opens_nothing(self, user):
        monitor = make(user)
        run(script(DOWN, UP))  # fails once, the re-check succeeds
        assert not Incident.objects.exists()
        monitor.refresh_from_db()
        assert monitor.status == "up"

    def test_an_inconclusive_check_opens_nothing(self, user):
        make(user)
        run(script(UNSURE))
        assert not Incident.objects.exists()

    def test_it_stays_one_incident_however_many_checks_fail(self, user):
        monitor = make(user)
        run(script(DOWN, DOWN))
        for _ in range(5):
            run(script(DOWN))  # already down: just logged
        assert Incident.objects.count() == 1
        assert Check.objects.filter(monitor=monitor, success=False).count() >= 6

    def test_several_regions_agreeing_still_make_one_incident(self, user, monkeypatch):
        monkeypatch.setattr(
            probers,
            "configured_probers",
            lambda: [probers.Prober(n, f"https://{n}.example") for n in ("tokyo", "frankfurt")],
        )

        def probe(configured, kind, target, config):
            return [(p, DOWN) for p in configured]

        monitor = make(user)
        run_scheduler_once(engine=Engine(runner=script(DOWN), probe=probe, sleep=lambda s: None))

        incident = Incident.objects.get()  # one, not one per region
        events = {e.kind: e.metadata for e in incident.events.all()}
        assert sorted(events["recheck_confirmed"]["regions_failing"]) == [
            "frankfurt",
            "primary",
            "tokyo",
        ]
        assert Check.objects.filter(monitor=monitor, confirmation=True).count() == 2

    def test_it_is_down_exactly_when_it_has_an_open_incident(self, user):
        monitor = make(user)
        run(script(DOWN, DOWN))
        monitor.refresh_from_db()
        assert (
            monitor.status == "down" and Incident.objects.filter(ended_at__isnull=True).count() == 1
        )
        run(script(UP))
        monitor.refresh_from_db()
        assert (
            monitor.status == "up" and Incident.objects.filter(ended_at__isnull=True).count() == 0
        )


class TestRecovery:
    def test_the_next_successful_check_closes_it_with_a_duration(self, user):
        monitor = make(user)
        run(script(DOWN, DOWN))
        Incident.objects.update(started_at=timezone.now() - timedelta(minutes=14))

        run(script(UP))

        incident = Incident.objects.get()
        assert not incident.ongoing and incident.resolution == "recovered"
        assert incident.resolved_by.success is True
        resolved = incident.events.get(kind="resolved")
        assert 13 * 60 <= resolved.metadata["duration_seconds"] <= 15 * 60
        monitor.refresh_from_db()
        assert monitor.status == "up"

    def test_a_second_outage_is_a_second_incident(self, user):
        monitor = make(user)
        run(script(DOWN, DOWN))
        run(script(UP))
        run(script(DOWN, DOWN))
        first, second = incidents_of(monitor)
        assert not first.ongoing and second.ongoing

    def test_recovery_never_ends_before_it_started(self, user):
        make(user)
        run(script(DOWN, DOWN))
        # A clock quirk: the incident's start is later than the recovering check's time.
        Incident.objects.update(started_at=timezone.now() + timedelta(hours=1))
        run(script(UP))
        incident = Incident.objects.get()
        assert incident.ended_at >= incident.started_at
        assert services.duration_seconds(incident) == 0

    def test_a_heartbeat_monitor_gets_incidents_too(self, user):
        make(
            user,
            type="cron",
            target="",
            config={"grace_period_seconds": 60},
            heartbeat_token="tok",
            interval_seconds=3600,
        )
        missed = r.CheckResult(success=False, status_detail=r.HEARTBEAT_MISSED)
        run(script(missed))  # deterministic failure: confirmed immediately
        assert Incident.objects.get().reason == "heartbeat_missed"
        run(script(UP))
        assert not Incident.objects.get().ongoing


class TestTheConstraint:
    def test_the_database_refuses_a_second_open_incident(self, user):
        monitor = make(user)
        now = timezone.now()
        Incident.objects.create(monitor=monitor, started_at=now, reason="timeout")
        with pytest.raises(IntegrityError), transaction.atomic():
            Incident.objects.create(monitor=monitor, started_at=now, reason="timeout")

    def test_closed_incidents_do_not_count(self, user):
        monitor = make(user)
        now = timezone.now()
        Incident.objects.create(monitor=monitor, started_at=now, ended_at=now, reason="timeout")
        Incident.objects.create(monitor=monitor, started_at=now, reason="timeout")  # fine

    def test_an_incident_cannot_end_before_it_starts(self, user):
        monitor = make(user)
        now = timezone.now()
        with pytest.raises(IntegrityError), transaction.atomic():
            Incident.objects.create(
                monitor=monitor, started_at=now, ended_at=now - timedelta(seconds=1), reason="x"
            )

    def test_opening_twice_is_harmless(self, user):
        monitor = make(user, status="down")
        check = Check.objects.create(
            monitor=monitor,
            region="primary",
            started_at=timezone.now(),
            success=False,
            status_detail="timeout",
        )
        assert services.open_incident(monitor, check) is not None
        assert services.open_incident(monitor, check) is None  # a retried pass
        assert Incident.objects.count() == 1


class TestSelfHealing:
    def test_a_down_monitor_with_no_incident_gets_one_on_its_next_failing_check(self, user):
        # E.g. it went down before incidents existed.
        monitor = make(user, status="down")
        assert not Incident.objects.exists()
        run(script(DOWN))
        assert Incident.objects.get().monitor == monitor

    def test_an_up_monitor_with_a_stale_open_incident_is_cleaned_up_by_a_success(self, user):
        monitor = make(user, status="up")
        Incident.objects.create(
            monitor=monitor, started_at=timezone.now() - timedelta(hours=1), reason="timeout"
        )
        run(script(UP))
        assert not Incident.objects.get().ongoing

    def test_a_failure_while_creating_the_incident_rolls_the_status_back(self, user, monkeypatch):
        monitor = make(user)

        def boom(*args, **kwargs):
            raise RuntimeError("database hiccup")

        monkeypatch.setattr(services, "open_incident", boom)
        summary = run(script(DOWN, DOWN))

        monitor.refresh_from_db()
        assert monitor.status == "up"  # not left "down" without an incident
        assert summary.errors == 1
        monkeypatch.undo()
        run(script(DOWN, DOWN))  # the next pass does it properly
        monitor.refresh_from_db()
        assert monitor.status == "down" and Incident.objects.count() == 1


class TestOtherWaysToEnd:
    def test_pausing_a_down_monitor_closes_its_incident(self, auth_api, user):
        monitor = make(user)
        run(script(DOWN, DOWN))
        resp = auth_api.post(f"/api/v1/monitors/{monitor.id}/pause/")
        assert resp.status_code == 200
        incident = Incident.objects.get()
        assert not incident.ongoing and incident.resolution == "paused"
        assert incident.events.filter(kind="closed_paused").exists()

    def test_pausing_a_healthy_monitor_touches_no_incidents(self, auth_api, user):
        monitor = make(user)
        auth_api.post(f"/api/v1/monitors/{monitor.id}/pause/")
        assert not Incident.objects.exists()

    def test_resuming_after_a_pause_starts_clean(self, auth_api, user):
        monitor = make(user)
        run(script(DOWN, DOWN))
        auth_api.post(f"/api/v1/monitors/{monitor.id}/pause/")
        auth_api.post(f"/api/v1/monitors/{monitor.id}/resume/")
        run(script(DOWN, DOWN))
        first, second = incidents_of(monitor)
        assert first.resolution == "paused" and second.ongoing

    def test_editing_the_target_of_a_down_monitor_closes_its_incident(self, auth_api, user):
        monitor = make(user)
        run(script(DOWN, DOWN))
        resp = auth_api.patch(
            f"/api/v1/monitors/{monitor.id}/", {"target": "https://new.example.com"}, format="json"
        )
        assert resp.status_code == 200
        incident = Incident.objects.get()
        assert (
            incident.resolution == "edited"
            and incident.events.filter(kind="closed_edited").exists()
        )

    def test_renaming_does_not_close_it(self, auth_api, user):
        monitor = make(user)
        run(script(DOWN, DOWN))
        auth_api.patch(f"/api/v1/monitors/{monitor.id}/", {"name": "Renamed"}, format="json")
        assert Incident.objects.get().ongoing

    def test_deleting_a_monitor_removes_its_incidents(self, auth_api, user):
        monitor = make(user)
        run(script(DOWN, DOWN))
        auth_api.delete(f"/api/v1/monitors/{monitor.id}/")
        assert not Incident.objects.exists()


# --- the definition of done, against a real (deliberately unstable) HTTP target ----


class Target:
    """A local HTTP server whose behaviour the test controls."""

    def __init__(self):
        self.mode = "up"  # up | down | blip
        self.blipped = False
        target = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                code = 200
                if target.mode == "down":
                    code = 503
                elif target.mode == "blip" and not target.blipped:
                    target.blipped = True
                    code = 500
                self.send_response(code)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://localhost:{self.server.server_address[1]}/"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def target():
    t = Target()
    yield t
    t.close()


def real_runner(monitor):
    # Loopback is exactly what the validator forbids, so allow it for this test target only.
    checker = HttpChecker(resolve=lambda host: [ipaddress.ip_address("127.0.0.1")])
    return checker.run(monitor.target, {"timeout_seconds": 5, "expected_status": ["2xx"]}), None


class TestAgainstARealUnstableTarget:
    def test_a_target_that_breaks_and_recovers_makes_exactly_one_incident(self, user, target):
        monitor = make(user, target=target.url, status="pending")
        engine = Engine(runner=real_runner, sleep=lambda s: None)

        def do_pass():
            Monitor.objects.update(next_check_at=timezone.now() - timedelta(minutes=1))
            run_scheduler_once(engine=engine)

        do_pass()  # healthy -> up
        monitor.refresh_from_db()
        assert monitor.status == "up" and not Incident.objects.exists()

        target.mode = "down"
        for _ in range(4):  # it stays broken across several passes
            do_pass()
        monitor.refresh_from_db()
        assert monitor.status == "down"
        incident = Incident.objects.get()  # exactly one
        assert incident.ongoing and incident.status_code == 503

        target.mode = "up"
        do_pass()
        monitor.refresh_from_db()
        assert monitor.status == "up"
        incident.refresh_from_db()
        assert incident.resolution == "recovered" and Incident.objects.count() == 1

    def test_a_single_flaky_response_is_not_an_incident(self, user, target):
        monitor = make(user, target=target.url)
        target.mode = "blip"  # one 500, then fine
        Monitor.objects.update(next_check_at=timezone.now() - timedelta(minutes=1))
        summary = run_scheduler_once(engine=Engine(runner=real_runner, sleep=lambda s: None))
        assert summary.blips == 1
        monitor.refresh_from_db()
        assert monitor.status == "up" and not Incident.objects.exists()
