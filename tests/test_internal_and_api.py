import json
from datetime import timedelta
from io import StringIO

import httpx
import pytest
from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APIClient

from monitoring import probers, signing
from monitoring.checks import result as r
from monitoring.models import Check, Monitor
from monitoring.services import summarize

pytestmark = pytest.mark.django_db

TRIGGER = "/internal/run-due-checks/"
SECRET = "test-scheduler-secret"
URL = "/api/v1/monitors/"


@pytest.fixture
def anon():
    return APIClient()


def make(user, **kwargs):
    fields = {
        "type": "http",
        "name": "Site",
        "target": "https://example.com",
        "interval_seconds": 300,
        "config": {},
        "next_check_at": timezone.now() - timedelta(minutes=1),
    }
    fields.update(kwargs)
    return Monitor.objects.create(user=user, **fields)


def check(monitor, **kwargs):
    fields = {
        "region": "primary",
        "started_at": timezone.now(),
        "success": True,
        "status_detail": "ok",
        "duration_ms": 100,
    }
    fields.update(kwargs)
    return Check.objects.create(monitor=monitor, **fields)


class TestScheduleTrigger:
    def test_rejects_unsigned_requests(self, anon):
        resp = anon.post(TRIGGER)
        assert resp.status_code == 401
        assert resp.json() == {"detail": "Unauthorized."}  # says nothing about why

    def test_rejects_a_wrong_stale_or_replayed_signature(self, anon):
        for header in (
            signing.sign("wrong-secret"),
            signing.sign(SECRET, now=1_000_000),  # decades old
            "t=1,v1=abc",
            "nonsense",
        ):
            assert anon.post(TRIGGER, HTTP_X_ROOTPULSE_SIGNATURE=header).status_code == 401

    def test_a_signature_for_one_body_cannot_be_used_for_another(self, anon):
        header = signing.sign(SECRET, b"")
        resp = anon.post(
            TRIGGER,
            data=b"extra",
            content_type="application/octet-stream",
            HTTP_X_ROOTPULSE_SIGNATURE=header,
        )
        assert resp.status_code == 401

    def test_get_is_not_allowed(self, anon):
        assert anon.get(TRIGGER).status_code in (401, 405)

    def test_a_signed_call_runs_a_pass_and_reports_a_summary(self, anon, user, monkeypatch):
        from monitoring import engine

        monkeypatch.setattr(
            engine,
            "run_primary",
            lambda m: (r.CheckResult(success=True, status_detail=r.OK, duration_ms=50), None),
        )
        monkeypatch.setattr(engine.time, "sleep", lambda s: None)
        monitor = make(user)

        resp = anon.post(TRIGGER, HTTP_X_ROOTPULSE_SIGNATURE=signing.sign(SECRET))

        assert resp.status_code == 200
        body = resp.json()
        assert body["claimed"] == 1 and body["checked"] == 1 and body["up"] == 1
        monitor.refresh_from_db()
        assert monitor.status == "up"

    def test_it_is_not_in_the_public_api_docs(self, client):
        schema = client.get("/api/v1/schema/", HTTP_ACCEPT="application/json").json()
        assert not any("internal" in path or "heartbeat" in path for path in schema["paths"])

    def test_no_secret_configured_means_nobody_gets_in(self, anon, settings):
        settings.SCHEDULER_SHARED_SECRET = ""
        assert anon.post(TRIGGER, HTTP_X_ROOTPULSE_SIGNATURE=signing.sign("")).status_code == 401


class TestHeartbeat:
    def monitor(self, user, **kwargs):
        return make(
            user,
            type="cron",
            target="",
            heartbeat_token="tok-123",
            status="up",
            config={"grace_period_seconds": 300},
            interval_seconds=3600,
            **kwargs,
        )

    def test_a_ping_records_when_it_happened(self, anon, user):
        monitor = self.monitor(user)
        for method in (anon.get, anon.post):
            Monitor.objects.filter(pk=monitor.pk).update(last_heartbeat_at=None)
            resp = method("/api/v1/heartbeat/tok-123/")
            assert resp.status_code == 200 and resp.json() == {"status": "ok"}
            monitor.refresh_from_db()
            assert abs((timezone.now() - monitor.last_heartbeat_at).total_seconds()) < 5

    def test_an_unknown_token_is_a_404(self, anon, user):
        self.monitor(user)
        assert anon.get("/api/v1/heartbeat/nope/").status_code == 404

    def test_a_ping_for_a_paused_monitor_is_acknowledged_but_ignored(self, anon, user):
        monitor = self.monitor(user)
        Monitor.objects.filter(pk=monitor.pk).update(status="paused", next_check_at=None)
        resp = anon.get("/api/v1/heartbeat/tok-123/")
        assert resp.json() == {"status": "paused"}
        monitor.refresh_from_db()
        assert monitor.last_heartbeat_at is None

    def test_a_ping_for_a_down_monitor_asks_for_an_immediate_recheck(self, anon, user):
        monitor = self.monitor(user)
        Monitor.objects.filter(pk=monitor.pk).update(
            status="down", next_check_at=timezone.now() + timedelta(hours=1)
        )
        anon.get("/api/v1/heartbeat/tok-123/")
        monitor.refresh_from_db()
        assert monitor.next_check_at <= timezone.now()

    def test_pings_are_rate_limited(self, anon, user, monkeypatch):
        from rest_framework.settings import api_settings
        from rest_framework.throttling import ScopedRateThrottle

        self.monitor(user)
        rates = {**api_settings.DEFAULT_THROTTLE_RATES, "heartbeat": "3/min"}
        monkeypatch.setattr(ScopedRateThrottle, "THROTTLE_RATES", rates)
        codes = [anon.get("/api/v1/heartbeat/tok-123/").status_code for _ in range(5)]
        assert codes[:3] == [200, 200, 200] and codes[3] == 429

    def test_a_full_cycle_missed_then_recovered(self, anon, user, monkeypatch):
        """No ping -> down at the deadline; a ping -> back up on the next pass."""
        from monitoring.engine import run_scheduler_once

        monitor = self.monitor(user, last_heartbeat_at=timezone.now() - timedelta(hours=3))
        Monitor.objects.filter(pk=monitor.pk).update(
            created_at=timezone.now() - timedelta(hours=4),
            next_check_at=timezone.now() - timedelta(minutes=1),
        )
        run_scheduler_once()
        monitor.refresh_from_db()
        assert monitor.status == "down" and monitor.last_status_detail == r.HEARTBEAT_MISSED

        anon.get("/api/v1/heartbeat/tok-123/")  # the job finally runs
        run_scheduler_once()
        monitor.refresh_from_db()
        assert monitor.status == "up"


class TestRunnerDispatch:
    def test_every_type_is_dispatched_to_its_checker(self, user, monkeypatch):
        from monitoring import runner

        seen = {}

        def fake(name):
            def fn(*args, **kwargs):
                seen[name] = args
                return r.CheckResult(success=True, status_detail=r.OK)

            return fn

        class FakeHttp:
            def run(self, target, config, keyword=None):
                seen["http"] = (target, keyword)
                return r.CheckResult(success=True, status_detail=r.OK)

        monkeypatch.setattr(runner, "HttpChecker", FakeHttp)
        monkeypatch.setattr(runner, "run_tcp", fake("tcp"))
        monkeypatch.setattr(runner, "run_ssl", fake("ssl"))
        monkeypatch.setattr(runner, "run_domain", fake("domain"))
        monkeypatch.setattr(
            runner,
            "run_dns",
            lambda *a, **k: (
                seen.setdefault("dns", a) and r.CheckResult(success=True, status_detail=r.OK),
                {"A": []},
            ),
        )

        cases = [
            ("http", "https://example.com", {}),
            ("keyword", "https://example.com", {"keyword": "hi"}),
            ("ping", "example.com", {"port": 443}),
            ("port", "example.com", {"port": 22}),
            ("ssl", "example.com", {"port": 443, "warn_days": 14}),
            ("domain", "example.com", {"warn_days": 30}),
            ("dns", "example.com", {"record_types": ["A"]}),
        ]
        for kind, target, config in cases:
            monitor = make(user, type=kind, target=target, config=config, name=kind)
            result, _ = runner.run_primary(monitor)
            assert result.success is True, kind
        assert seen["http"] == ("https://example.com", "hi")
        assert seen["tcp"][1] == 22  # the port monitor's port
        assert seen["ssl"][:3] == ("example.com", 443, 14)

    def test_a_checker_that_explodes_becomes_an_inconclusive_result(self, user, monkeypatch):
        from monitoring import runner

        def boom(*a, **k):
            raise ValueError("kaboom")

        monkeypatch.setattr(runner, "run_tcp", boom)
        result, state = runner.run_primary(
            make(user, type="port", target="example.com", config={"port": 1})
        )
        assert result.success is None and result.status_detail == r.INTERNAL_ERROR


class TestChecksApi:
    def test_lists_checks_newest_first_with_cursor_pagination(self, auth_api, user):
        monitor = make(user)
        base = timezone.now()
        for i in range(60):
            check(monitor, started_at=base - timedelta(minutes=i), duration_ms=i)

        first = auth_api.get(f"{URL}{monitor.id}/checks/").json()
        assert len(first["results"]) == 50 and first["next"] and first["previous"] is None
        assert first["results"][0]["duration_ms"] == 0  # newest first

        second = auth_api.get(first["next"]).json()
        assert len(second["results"]) == 10 and second["next"] is None

    def test_filters(self, auth_api, user):
        monitor = make(user)
        now = timezone.now()
        check(monitor, region="primary", started_at=now - timedelta(hours=3))
        check(monitor, region="tokyo", confirmation=True, started_at=now - timedelta(hours=1))
        check(monitor, region="primary", started_at=now - timedelta(minutes=5))
        base = f"{URL}{monitor.id}/checks/"

        assert len(auth_api.get(base, {"region": "tokyo"}).json()["results"]) == 1
        assert len(auth_api.get(base, {"confirmation": "true"}).json()["results"]) == 1
        assert len(auth_api.get(base, {"confirmation": "false"}).json()["results"]) == 2
        since = (now - timedelta(hours=2)).isoformat()
        assert len(auth_api.get(base, {"since": since}).json()["results"]) == 2
        assert len(auth_api.get(base, {"until": since}).json()["results"]) == 1
        assert auth_api.get(base, {"since": "yesterday-ish"}).status_code == 400

    def test_exposes_timing_and_detail(self, auth_api, user):
        monitor = make(user)
        check(
            monitor,
            dns_ms=5,
            tcp_ms=20,
            tls_ms=40,
            ttfb_ms=90,
            status_code=200,
            detail={"redirects": 1},
        )
        row = auth_api.get(f"{URL}{monitor.id}/checks/").json()["results"][0]
        assert (row["dns_ms"], row["tcp_ms"], row["tls_ms"], row["ttfb_ms"]) == (5, 20, 40, 90)
        assert row["detail"] == {"redirects": 1} and row["status_code"] == 200

    def test_is_private_to_the_owner(self, auth_api, make_user):
        other = make_user("someone@example.com")
        theirs = make(other)
        check(theirs)
        assert auth_api.get(f"{URL}{theirs.id}/checks/").status_code == 404

    def test_requires_sign_in(self, anon, user):
        assert anon.get(f"{URL}{make(user).id}/checks/").status_code == 401


class TestSummary:
    def test_uptime_and_response_times_over_the_last_day(self, user):
        monitor = make(user)
        now = timezone.now()
        for ms in (100, 200, 300, 400):
            check(monitor, duration_ms=ms, started_at=now - timedelta(hours=1))
        check(
            monitor,
            success=False,
            status_detail="timeout",
            duration_ms=None,
            started_at=now - timedelta(hours=2),
        )

        summary = summarize(monitor)

        assert summary["checks"] == 5 and summary["uptime_percent"] == 80.0
        assert summary["avg_response_ms"] == 250 and summary["p95_response_ms"] == 400

    def test_it_ignores_rechecks_other_regions_inconclusive_and_old_checks(self, user):
        monitor = make(user)
        now = timezone.now()
        check(monitor, duration_ms=100)
        check(monitor, region="tokyo", success=False)
        check(monitor, confirmation=True, success=False)
        check(monitor, success=None, status_detail="blocked_target")
        check(monitor, success=False, started_at=now - timedelta(hours=30))
        summary = summarize(monitor)
        assert summary["checks"] == 1 and summary["uptime_percent"] == 100.0

    def test_no_data_is_null_not_zero(self, user):
        summary = summarize(make(user))
        assert (
            summary["checks"] == 0
            and summary["uptime_percent"] is None
            and summary["avg_response_ms"] is None
        )

    def test_the_detail_endpoint_includes_it_and_the_list_does_not(self, auth_api, user):
        monitor = make(user)
        check(monitor)
        detail = auth_api.get(f"{URL}{monitor.id}/").json()
        assert detail["summary"]["checks"] == 1
        listed = auth_api.get(URL).json()["results"][0]
        assert "summary" not in listed
        assert listed["last_response_ms"] is None and "last_status_detail" in listed


class TestSingleMonitorShape:
    """pause, resume, edit and create return what retrieve returns, summary included."""

    def test_every_single_monitor_response_has_the_summary(self, auth_api, user):
        monitor = make(user)
        check(monitor)
        base = f"{URL}{monitor.id}/"
        responses = {
            "retrieve": auth_api.get(base),
            "patch": auth_api.patch(base, {"name": "Renamed"}, format="json"),
            "pause": auth_api.post(f"{base}pause/"),
            "resume": auth_api.post(f"{base}resume/"),
        }
        for name, resp in responses.items():
            assert resp.status_code == 200, name
            assert resp.json()["summary"]["checks"] == 1, name

    def test_create_includes_it_too(self, auth_api):
        resp = auth_api.post(
            URL, {"type": "http", "name": "New", "target": "https://example.com"}, format="json"
        )
        assert resp.status_code == 201 and resp.json()["summary"]["checks"] == 0


class TestCommand:
    def test_runs_one_pass_and_prints_a_summary(self, user, monkeypatch):
        from monitoring import engine

        monkeypatch.setattr(
            engine, "run_primary", lambda m: (r.CheckResult(success=True, status_detail=r.OK), None)
        )
        monkeypatch.setattr(engine.time, "sleep", lambda s: None)
        make(user)
        out = StringIO()
        call_command("run_scheduler_once", stdout=out)
        assert "claimed=1 checked=1 up=1" in out.getvalue()


class TestProberClient:
    SECRET = "prober-secret"

    @pytest.fixture(autouse=True)
    def configure(self, settings):
        settings.PROBER_SHARED_SECRET = self.SECRET
        settings.PROBER_URLS = (
            "tokyo=https://tokyo.example.workers.dev, frankfurt=https://fra.example.workers.dev/"
        )

    def test_parses_the_configured_probers(self):
        names = [(p.name, p.url) for p in probers.configured_probers()]
        assert names == [
            ("tokyo", "https://tokyo.example.workers.dev"),
            ("frankfurt", "https://fra.example.workers.dev"),
        ]

    def test_none_without_a_shared_secret(self, settings):
        settings.PROBER_SHARED_SECRET = ""
        assert probers.configured_probers() == []

    def reply(self, result, secret=None, status=200):
        body = json.dumps({"region": "tokyo", "result": result}).encode()

        def handler(request: httpx.Request):
            # The request itself must be signed.
            assert signing.verify(self.SECRET, request.headers[signing.HEADER], request.content)
            assert json.loads(request.content)["target"] == "https://example.com"
            return httpx.Response(
                status,
                content=body,
                headers={signing.HEADER: signing.sign(secret or self.SECRET, body)},
            )

        return httpx.MockTransport(handler)

    def probe(self, transport):
        prober = probers.Prober("tokyo", "https://tokyo.example.workers.dev")
        return probers.probe_one(prober, "http", "https://example.com", {}, transport=transport)

    def test_a_signed_reply_is_parsed_into_a_result(self):
        result = self.probe(
            self.reply({"success": False, "status_detail": "timeout", "duration_ms": 10000})
        )
        assert (result.success, result.status_detail, result.duration_ms) == (
            False,
            "timeout",
            10000,
        )

    def test_a_reply_signed_with_the_wrong_secret_is_ignored(self):
        assert (
            self.probe(self.reply({"success": True, "status_detail": "ok"}, secret="attacker"))
            is None
        )

    def test_an_unsigned_reply_is_ignored(self):
        transport = httpx.MockTransport(
            lambda req: httpx.Response(200, json={"result": {"success": True}})
        )
        assert self.probe(transport) is None

    def test_errors_and_garbage_make_the_prober_abstain(self):
        assert self.probe(self.reply({}, status=500)) is None
        boom = httpx.MockTransport(
            lambda req: (_ for _ in ()).throw(httpx.ConnectError("down", request=req))
        )
        assert self.probe(boom) is None

    def test_a_malicious_reply_cannot_inject_unknown_fields(self):
        result = self.probe(
            self.reply(
                {"success": "yes", "status_detail": "x" * 500, "duration_ms": "fast", "evil": 1}
            )
        )
        assert (
            result.success is None
            and len(result.status_detail) == 64
            and result.duration_ms is None
        )

    def test_all_probers_are_asked_in_parallel(self):
        transport = self.reply({"success": True, "status_detail": "ok"})
        answers = probers.probe_all(
            probers.configured_probers(), "http", "https://example.com", {}, transport=transport
        )
        assert [p.name for p, _ in answers] == ["tokyo", "frankfurt"]
        assert all(res.success for _, res in answers)


class TestCommandLoop:
    """--loop is for local development; it must survive a database that drops idle connections."""

    def run_loop(self, monkeypatch, passes):
        import time as time_module

        from django.db import connections

        from monitoring.management.commands import run_scheduler_once as command

        closed = []
        monkeypatch.setattr(connections, "close_all", lambda: closed.append(1))
        outcomes = iter(passes)

        def fake_pass(batch_size=None):
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr(command, "run_scheduler_once", fake_pass)

        sleeps = []

        def fake_sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) >= len(passes):
                raise KeyboardInterrupt

        monkeypatch.setattr(time_module, "sleep", fake_sleep)
        monkeypatch.setattr(command.time, "sleep", fake_sleep)
        out, err = StringIO(), StringIO()
        with pytest.raises(KeyboardInterrupt):
            call_command("run_scheduler_once", loop=60, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue(), closed, sleeps

    def test_releases_its_connection_between_passes(self, monkeypatch):
        from monitoring.engine import PassSummary

        out, err, closed, sleeps = self.run_loop(monkeypatch, [PassSummary(), PassSummary()])
        assert out.count("claimed=") == 2
        assert len(closed) == 2 and sleeps == [60, 60]

    def test_a_dropped_connection_does_not_end_the_loop(self, monkeypatch):
        from django.db import OperationalError

        from monitoring.engine import PassSummary

        out, err, closed, _ = self.run_loop(
            monkeypatch,
            [OperationalError("server closed the connection unexpectedly"), PassSummary()],
        )
        assert "pass failed, will retry" in err
        assert "claimed=" in out  # the next pass ran

    def test_without_loop_a_database_error_is_not_swallowed(self, monkeypatch):
        from django.db import OperationalError

        from monitoring.management.commands import run_scheduler_once as command

        def boom(batch_size=None):
            raise OperationalError("down")

        monkeypatch.setattr(command, "run_scheduler_once", boom)
        with pytest.raises(OperationalError):
            call_command("run_scheduler_once")
