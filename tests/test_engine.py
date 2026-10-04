import threading
from datetime import timedelta

import pytest
from django.db import connection
from django.utils import timezone

from monitoring import hooks, probers
from monitoring.checks import result as r
from monitoring.engine import Engine, PassSummary, claim_due, run_scheduler_once
from monitoring.models import Check, MaintenanceWindow, Monitor

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
        "status": "pending",
        "next_check_at": timezone.now() - timedelta(minutes=1),
    }
    fields.update(kwargs)
    return Monitor.objects.create(user=user, name=name, **fields)


def script(*outcomes):
    """A runner that returns the given results in order (repeating the last)."""
    calls = []

    def runner(monitor):
        index = min(len(calls), len(outcomes) - 1)
        calls.append(monitor.id)
        outcome = outcomes[index]
        return outcome if isinstance(outcome, tuple) else (outcome, None)

    runner.calls = calls
    return runner


def run(runner, **kwargs):
    return run_scheduler_once(engine=Engine(runner=runner, sleep=lambda s: None), **kwargs)


class TestClaiming:
    def test_only_due_monitors_are_claimed_and_pushed_one_interval_ahead(self, user):
        due = make(user, "due")
        later = make(user, "later", next_check_at=timezone.now() + timedelta(hours=1))
        now = timezone.now()

        claimed, skipped = claim_due(now, 100)

        assert list(claimed) == [due.id] and skipped == 0
        due.refresh_from_db()
        assert due.next_check_at == now + timedelta(seconds=300)
        later.refresh_from_db()
        assert later.next_check_at > now + timedelta(minutes=59)

    def test_a_second_pass_does_not_claim_what_the_first_took(self, user):
        make(user)
        now = timezone.now()
        assert len(claim_due(now, 100)[0]) == 1
        assert len(claim_due(now, 100)[0]) == 0  # a retried or overlapping trigger finds nothing

    def test_paused_and_never_due_monitors_are_ignored(self, user):
        make(user, "paused", status="paused", next_check_at=None)
        make(user, "never", next_check_at=None)
        assert claim_due(timezone.now(), 100) == ({}, 0)

    def test_oldest_first_and_capped_by_the_batch_size(self, user):
        now = timezone.now()
        oldest = make(user, "a", next_check_at=now - timedelta(hours=2))
        make(user, "b", next_check_at=now - timedelta(hours=1))
        make(user, "c", next_check_at=now - timedelta(minutes=5))
        claimed, _ = claim_due(now, 2)
        assert len(claimed) == 2 and oldest.id in claimed
        # The one left over is picked up by the next pass.
        assert len(claim_due(now, 2)[0]) == 1

    def test_monitors_in_a_maintenance_window_are_skipped_until_it_ends(self, user):
        now = timezone.now()
        monitor = make(user)
        window = MaintenanceWindow.objects.create(
            user=user,
            name="deploy",
            starts_at=now - timedelta(minutes=10),
            ends_at=now + timedelta(minutes=20),
        )
        window.monitors.add(monitor)

        claimed, skipped = claim_due(now, 100)

        assert claimed == {} and skipped == 1
        monitor.refresh_from_db()
        assert monitor.next_check_at == window.ends_at

    def test_a_finished_or_future_window_does_not_block_checks(self, user):
        now = timezone.now()
        monitor = make(user)
        for start, end in (
            (now - timedelta(hours=2), now - timedelta(hours=1)),
            (now + timedelta(hours=1), now + timedelta(hours=2)),
        ):
            MaintenanceWindow.objects.create(
                user=user, name="w", starts_at=start, ends_at=end
            ).monitors.add(monitor)
        assert len(claim_due(now, 100)[0]) == 1


@pytest.mark.skipif(connection.vendor != "postgresql", reason="row locks need Postgres")
@pytest.mark.django_db(transaction=True)
def test_concurrent_passes_never_double_claim(user):
    """The property the whole scheduler design rests on (docs/plan/03-monitoring-engine.md)."""
    for i in range(60):
        make(user, f"m{i}")
    now = timezone.now()
    claimed_by_thread: list[set] = []
    barrier = threading.Barrier(6)

    def worker():
        from django.db import connections

        barrier.wait()
        claimed, _ = claim_due(now, 25)
        claimed_by_thread.append(set(claimed))
        connections.close_all()

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    everything = [m for claimed in claimed_by_thread for m in claimed]
    assert len(everything) == len(set(everything)) == 60  # each claimed exactly once


class TestExecution:
    def test_a_pending_monitor_goes_up_with_a_recorded_check(self, user):
        monitor = make(user)
        summary = run(script(UP))

        monitor.refresh_from_db()
        assert monitor.status == "up"
        assert monitor.last_check_at is not None
        assert monitor.last_response_ms == 120 and monitor.last_status_detail == "ok"
        check = Check.objects.get()
        assert (check.monitor, check.success, check.status_code, check.region) == (
            monitor,
            True,
            200,
            "primary",
        )
        assert not check.confirmation
        assert (summary.claimed, summary.checked, summary.up) == (1, 1, 1)

    def test_next_check_is_one_interval_after_the_claim(self, user):
        monitor = make(user, interval_seconds=600)
        before = timezone.now()
        run(script(UP))
        monitor.refresh_from_db()
        assert monitor.next_check_at >= before + timedelta(seconds=600)

    def test_a_confirmed_failure_marks_it_down(self, user):
        monitor = make(user, status="up")
        runner = script(DOWN, DOWN)  # the first check and the local re-check both fail
        summary = run(runner)

        monitor.refresh_from_db()
        assert monitor.status == "down"
        assert len(runner.calls) == 2
        checks = Check.objects.order_by("id")
        assert [c.confirmation for c in checks] == [False, True]
        assert summary.down == 1 and summary.blips == 0

    def test_a_failure_that_does_not_repeat_is_a_blip_and_changes_nothing(self, user):
        monitor = make(user, status="up")
        summary = run(script(DOWN, UP))

        monitor.refresh_from_db()
        assert monitor.status == "up"
        assert summary.blips == 1 and summary.down == 0
        # The failed check is still on the record.
        assert Check.objects.filter(success=False, confirmation=False).count() == 1

    def test_a_monitor_that_is_already_down_is_not_re_confirmed(self, user):
        monitor = make(user, status="down")
        runner = script(DOWN)
        run(runner)
        assert len(runner.calls) == 1  # just logged, no extra checks
        monitor.refresh_from_db()
        assert monitor.status == "down"

    def test_a_success_recovers_a_down_monitor(self, user):
        monitor = make(user, status="down")
        run(script(UP))
        monitor.refresh_from_db()
        assert monitor.status == "up"

    def test_an_inconclusive_check_never_changes_the_status(self, user):
        monitor = make(user, status="up")
        summary = run(script(UNSURE))
        monitor.refresh_from_db()
        assert monitor.status == "up" and summary.inconclusive == 1
        check = Check.objects.get()
        assert check.success is None and check.status_detail == r.BLOCKED_TARGET

    def test_a_pending_monitor_stays_pending_when_inconclusive(self, user):
        monitor = make(user)
        run(script(UNSURE))
        monitor.refresh_from_db()
        assert monitor.status == "pending"

    def test_a_crashing_runner_does_not_sink_the_pass(self, user):
        good = make(user, "good")
        bad = make(user, "bad", next_check_at=timezone.now() - timedelta(hours=1))

        def runner(monitor):
            if monitor.id == bad.id:
                raise RuntimeError("boom")
            return UP, None

        summary = run(runner)
        good.refresh_from_db()
        assert good.status == "up" and summary.errors == 1

    def test_the_status_change_hook_fires_only_on_a_real_change(self, user, monkeypatch):
        events = []
        monkeypatch.setattr(
            hooks, "status_changed", lambda m, old, new, check: events.append((old, new))
        )
        make(user)
        run(script(UP))  # pending -> up
        Monitor.objects.update(next_check_at=timezone.now() - timedelta(minutes=1))
        run(script(UP))  # up -> up: nothing
        Monitor.objects.update(next_check_at=timezone.now() - timedelta(minutes=1))
        run(script(DOWN, DOWN))  # up -> down
        assert events == [("pending", "up"), ("up", "down")]

    def test_a_monitor_paused_while_its_check_runs_stays_paused(self, user):
        monitor = make(user, status="up")

        def runner(m):
            Monitor.objects.filter(pk=m.pk).update(status="paused", next_check_at=None)
            return UP, None

        run(runner)
        monitor.refresh_from_db()
        assert monitor.status == "paused" and monitor.next_check_at is None

    def test_an_edit_made_while_the_check_runs_is_not_overwritten(self, user):
        monitor = make(user, status="up")

        def runner(m):
            # The user changes the target mid-check (this bumps updated_at, resetting to pending).
            m2 = Monitor.objects.get(pk=m.pk)
            m2.target = "https://new.example.com"
            m2.status = "pending"
            m2.save()
            return DOWN, None

        run(runner)
        monitor.refresh_from_db()
        assert monitor.status == "pending" and monitor.target == "https://new.example.com"

    def test_a_deleted_or_paused_monitor_is_skipped_quietly(self, user):
        monitor = make(user)
        engine = Engine(runner=script(UP), sleep=lambda s: None)
        summary = PassSummary()
        monitor_id = monitor.id
        monitor.delete()
        engine.execute(monitor_id, summary)
        assert summary.checked == 0


class TestConfirmation:
    def configure(self, monkeypatch, names=("tokyo", "frankfurt")):
        monkeypatch.setattr(
            probers,
            "configured_probers",
            lambda: [probers.Prober(n, f"https://{n}.example") for n in names],
        )

    def engine(self, runner, answers):
        calls = []

        def probe(configured, kind, target, config):
            calls.append((kind, target))
            return [(p, answers.get(p.name)) for p in configured]

        engine = Engine(runner=runner, probe=probe, sleep=lambda s: None)
        engine.probe_calls = calls
        return engine

    def test_two_of_three_regions_agreeing_confirms(self, user, monkeypatch):
        self.configure(monkeypatch)
        monitor = make(user, status="up")
        runner = script(DOWN)
        engine = self.engine(runner, {"tokyo": DOWN, "frankfurt": UP})  # primary + tokyo down
        run_scheduler_once(engine=engine)

        monitor.refresh_from_db()
        assert monitor.status == "down"
        assert len(runner.calls) == 1  # no local re-check when probers answered
        regions = sorted(Check.objects.values_list("region", flat=True))
        assert regions == ["frankfurt", "primary", "tokyo"]
        assert Check.objects.filter(confirmation=True).count() == 2

    def test_one_region_alone_is_a_blip(self, user, monkeypatch):
        self.configure(monkeypatch)
        monitor = make(user, status="up")
        engine = self.engine(script(DOWN), {"tokyo": UP, "frankfurt": UP})
        summary = run_scheduler_once(engine=engine)
        monitor.refresh_from_db()
        assert monitor.status == "up" and summary.blips == 1

    def test_a_prober_that_cannot_be_reached_abstains(self, user, monkeypatch):
        self.configure(monkeypatch)
        monitor = make(user, status="up")
        # tokyo is unreachable (None); frankfurt agrees: 2 of the 2 that answered.
        engine = self.engine(script(DOWN), {"tokyo": None, "frankfurt": DOWN})
        run_scheduler_once(engine=engine)
        monitor.refresh_from_db()
        assert monitor.status == "down"

    def test_when_a_single_prober_answers_both_must_agree(self, user, monkeypatch):
        self.configure(monkeypatch)
        monitor = make(user, status="up")
        engine = self.engine(script(DOWN), {"tokyo": None, "frankfurt": UP})
        run_scheduler_once(engine=engine)
        monitor.refresh_from_db()
        assert monitor.status == "up"

    def test_with_no_prober_answering_it_falls_back_to_a_local_recheck(self, user, monkeypatch):
        self.configure(monkeypatch)
        monitor = make(user, status="up")
        runner = script(DOWN, DOWN)
        engine = self.engine(runner, {"tokyo": None, "frankfurt": None})
        run_scheduler_once(engine=engine)
        monitor.refresh_from_db()
        assert monitor.status == "down" and len(runner.calls) == 2

    def test_probers_are_only_asked_about_types_where_regions_can_disagree(self, user, monkeypatch):
        self.configure(monkeypatch)
        engine = self.engine(script(DOWN, DOWN), {})
        make(user, "ssl", type="ssl", target="example.com", config={"warn_days": 14})
        run_scheduler_once(engine=engine)
        assert engine.probe_calls == []  # SSL is re-checked locally instead

    @pytest.mark.parametrize("kind", ["http", "keyword", "ping", "port"])
    def test_multi_region_types(self, user, monkeypatch, kind):
        self.configure(monkeypatch)
        make(user, kind, type=kind, target="example.com", config={"port": 80, "keyword": "x"})
        engine = self.engine(script(DOWN), {"tokyo": DOWN, "frankfurt": DOWN})
        run_scheduler_once(engine=engine)
        assert engine.probe_calls == [(kind, "example.com")]

    @pytest.mark.parametrize(
        "kind,config",
        [
            ("domain", {"warn_days": 30}),
            ("dns", {"record_types": ["A"]}),
            ("cron", {"grace_period_seconds": 300}),
        ],
    )
    def test_deterministic_types_are_confirmed_immediately(self, user, kind, config):
        monitor = make(
            user,
            type=kind,
            target="example.com" if kind != "cron" else "",
            config=config,
            status="up",
            **({"heartbeat_token": "tok"} if kind == "cron" else {}),
        )
        runner = script(DOWN)
        run(runner)
        monitor.refresh_from_db()
        assert monitor.status == "down" and len(runner.calls) == 1


class TestEngineState:
    def test_dns_state_is_saved_for_the_next_check(self, user):
        monitor = make(user, type="dns", target="example.com", config={"record_types": ["A"]})
        outcome = (
            r.CheckResult(success=True, status_detail=r.DNS_BASELINE),
            {"dns": {"A": ["1.1.1.1"]}},
        )
        run(script(outcome))
        monitor.refresh_from_db()
        assert monitor.state == {"dns": {"A": ["1.1.1.1"]}}

    def test_a_healthy_heartbeat_is_next_due_at_its_deadline(self, user):
        created = timezone.now() - timedelta(hours=2)
        monitor = make(
            user,
            type="cron",
            target="",
            interval_seconds=3600,
            config={"grace_period_seconds": 300},
            heartbeat_token="tok",
            last_heartbeat_at=timezone.now() - timedelta(minutes=10),
        )
        Monitor.objects.filter(pk=monitor.pk).update(created_at=created)
        run(script(UP))
        monitor.refresh_from_db()
        expected = monitor.last_heartbeat_at + timedelta(seconds=3900)
        assert abs((monitor.next_check_at - expected).total_seconds()) < 1


class TestPass:
    def test_it_summarises_and_reports_timing(self, user):
        make(user, "a")
        make(user, "b")
        summary = run(script(UP, DOWN, DOWN))
        data = summary.as_dict()
        assert data["claimed"] == 2 and data["checked"] == 2
        assert data["duration_ms"] >= 0 and "details" not in data

    def test_nothing_due_is_a_cheap_no_op(self, user):
        summary = run(script(UP))
        assert summary.claimed == 0 and summary.checked == 0

    @pytest.mark.django_db(transaction=True)  # worker threads need committed rows
    def test_checks_run_concurrently_in_a_pool(self, user):
        pytest.importorskip("concurrent.futures")
        if connection.vendor == "sqlite":
            pytest.skip("sqlite in-memory databases aren't shared between threads")
        for i in range(8):
            make(user, f"m{i}")
        summary = run(script(UP), workers=4)
        assert summary.checked == 8

    def test_monitors_left_over_when_time_runs_out_are_handed_back(self, user):
        monitors = [make(user, f"m{i}") for i in range(3)]
        originals = {m.id: m.next_check_at for m in monitors}
        runner = script(UP)
        summary = run(runner, time_budget=-1)  # already out of time
        assert summary.released == 3 and summary.checked == 0 and runner.calls == []
        for monitor in Monitor.objects.all():
            assert monitor.next_check_at == originals[monitor.id]  # due again next pass
