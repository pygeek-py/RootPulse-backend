"""Rollups (hourly, daily, fleet) and the analytics endpoints.

Rollup tests use a fixed clock and known synthetic checks so every number can be checked by
hand. The endpoint tests also prove the dashboards read rollups and not raw checks."""

import re
from datetime import UTC, date, datetime, timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from analytics import rollups
from analytics.models import CheckRollupDaily, CheckRollupHourly, FleetSummaryDaily
from incidents.models import Incident
from monitoring import signing
from monitoring.models import Check, Monitor

pytestmark = pytest.mark.django_db

NOW = datetime(2026, 10, 5, 14, 30, tzinfo=UTC)
FLEET = "/api/v1/analytics/fleet/"


def at(days=0, hours=0, minutes=0, base=NOW):
    return base - timedelta(days=days, hours=hours, minutes=minutes)


def make_monitor(user, name="Shop", **kwargs):
    fields = {
        "type": "http",
        "target": "https://shop.example.com",
        "interval_seconds": 300,
        "config": {},
        "status": "up",
        "next_check_at": NOW,
    }
    fields.update(kwargs)
    return Monitor.objects.create(user=user, name=name, **fields)


def add_check(monitor, when, success=True, duration=100, confirmation=False, **extra):
    fields = {
        "monitor": monitor,
        "region": "primary",
        "started_at": when,
        "success": success,
        "duration_ms": duration if success else None,
        "status_detail": "ok" if success else "timeout",
        "confirmation": confirmation,
    }
    fields.update(extra)
    return Check.objects.create(**fields)


def make_incident(monitor, started, ended=None, excluded=False):
    return Incident.objects.create(
        monitor=monitor,
        started_at=started,
        ended_at=ended,
        reason="timeout",
        resolution="recovered" if ended else "",
        excluded_from_reports=excluded,
    )


def hourly(monitor, hour):
    return CheckRollupHourly.objects.get(monitor=monitor, hour=hour)


H = datetime(2026, 10, 5, 12, tzinfo=UTC)  # a whole hour used by several tests


class TestHourlyRollup:
    def test_counts_and_response_statistics_are_exact(self, user):
        m = make_monitor(user)
        for i, ms in enumerate(range(100, 1100, 100)):  # 100..1000, ten successes
            add_check(m, H + timedelta(minutes=i), duration=ms)
        add_check(m, H + timedelta(minutes=20), success=False)
        add_check(m, H + timedelta(minutes=21), success=False)
        add_check(m, H + timedelta(minutes=22), success=None)

        rollups.compute_hours(m, H, H + timedelta(hours=1), now=NOW)

        row = hourly(m, H)
        assert (row.up, row.down, row.inconclusive) == (10, 2, 1)
        assert (row.response_count, row.response_sum_ms) == (10, 5500)
        assert (row.response_min_ms, row.response_max_ms) == (100, 1000)
        assert (row.p50_ms, row.p95_ms) == (500, 1000)  # nearest rank

    def test_reconfirmation_checks_are_not_counted(self, user):
        m = make_monitor(user)
        add_check(m, H + timedelta(minutes=1))
        add_check(m, H + timedelta(minutes=2), success=False)
        add_check(m, H + timedelta(minutes=3), success=False, confirmation=True)
        rollups.compute_hours(m, H, H + timedelta(hours=1), now=NOW)
        row = hourly(m, H)
        assert (row.up, row.down) == (1, 1)

    def test_checks_land_in_the_hour_they_started(self, user):
        m = make_monitor(user)
        add_check(m, H - timedelta(seconds=1))
        add_check(m, H)
        add_check(m, H + timedelta(minutes=59, seconds=59))
        add_check(m, H + timedelta(hours=1))
        rollups.compute_hours(m, H - timedelta(hours=1), H + timedelta(hours=2), now=NOW)
        counts = {r.hour: r.up for r in CheckRollupHourly.objects.filter(monitor=m)}
        assert counts == {
            H - timedelta(hours=1): 1,
            H: 2,
            H + timedelta(hours=1): 1,
        }

    def test_timing_breakdown_only_counts_checks_that_have_one(self, user):
        m = make_monitor(user)
        add_check(m, H + timedelta(minutes=1), dns_ms=10, tcp_ms=20, tls_ms=30, ttfb_ms=100)
        add_check(m, H + timedelta(minutes=2), dns_ms=30, tcp_ms=40, tls_ms=50, ttfb_ms=300)
        add_check(m, H + timedelta(minutes=3))  # e.g. a ping: no breakdown
        rollups.compute_hours(m, H, H + timedelta(hours=1), now=NOW)
        row = hourly(m, H)
        assert row.timing_count == 2
        assert (row.dns_sum_ms, row.tcp_sum_ms, row.tls_sum_ms, row.ttfb_sum_ms) == (
            40,
            60,
            80,
            400,
        )
        assert row.response_count == 3

    def test_an_hour_of_only_failures_has_no_response_figures(self, user):
        m = make_monitor(user)
        add_check(m, H + timedelta(minutes=1), success=False)
        rollups.compute_hours(m, H, H + timedelta(hours=1), now=NOW)
        row = hourly(m, H)
        assert (row.down, row.response_count, row.p50_ms, row.response_min_ms) == (1, 0, None, None)

    def test_recomputing_replaces_instead_of_adding(self, user):
        m = make_monitor(user)
        add_check(m, H + timedelta(minutes=1))
        for _ in range(3):
            rollups.compute_hours(m, H, H + timedelta(hours=1), now=NOW)
        assert CheckRollupHourly.objects.filter(monitor=m).count() == 1
        assert hourly(m, H).up == 1
        add_check(m, H + timedelta(minutes=2))
        rollups.compute_hours(m, H, H + timedelta(hours=1), now=NOW)
        assert hourly(m, H).up == 2

    def test_an_hour_that_lost_its_checks_loses_its_row(self, user):
        m = make_monitor(user)
        gone = add_check(m, H + timedelta(minutes=1))
        rollups.compute_hours(m, H, H + timedelta(hours=1), now=NOW)
        gone.delete()
        rollups.compute_hours(m, H, H + timedelta(hours=1), now=NOW)
        assert not CheckRollupHourly.objects.filter(monitor=m).exists()

    def test_other_monitors_are_untouched(self, user):
        a, b = make_monitor(user, "A"), make_monitor(user, "B")
        add_check(a, H + timedelta(minutes=1))
        add_check(b, H + timedelta(minutes=1), success=False)
        rollups.compute_hours(a, H, H + timedelta(hours=1), now=NOW)
        assert not CheckRollupHourly.objects.filter(monitor=b).exists()


class TestExcludedIncidents:
    def test_failures_inside_an_excluded_incident_dont_count_against_uptime(self, user):
        m = make_monitor(user)
        for minute in range(0, 20):
            add_check(m, H + timedelta(minutes=minute), success=minute >= 10)
        make_incident(m, H + timedelta(minutes=5), H + timedelta(minutes=12), excluded=True)
        rollups.compute_hours(m, H, H + timedelta(hours=1), now=NOW)
        row = hourly(m, H)
        assert (row.up, row.down, row.down_excluded) == (10, 10, 5 + 0)  # minutes 5..9 inside
        # (a failure at minute 0..4 is before the incident and still counts)

    def test_a_non_excluded_incident_counts_normally(self, user):
        m = make_monitor(user)
        add_check(m, H + timedelta(minutes=1), success=False)
        make_incident(m, H, H + timedelta(minutes=10))
        rollups.compute_hours(m, H, H + timedelta(hours=1), now=NOW)
        assert hourly(m, H).down_excluded == 0

    def test_toggling_exclusion_through_the_api_recomputes_the_numbers(self, auth_api, user):
        m = make_monitor(user)
        start = timezone.now().replace(minute=0, second=0, microsecond=0)
        for minute in range(0, 6):
            add_check(m, start + timedelta(minutes=minute), success=False)
        add_check(m, start + timedelta(minutes=6))
        incident = make_incident(m, start, start + timedelta(minutes=5))
        rollups.run_rollups_once()
        assert hourly(m, rollups.floor_hour(start)).down_excluded == 0

        resp = auth_api.patch(
            f"/api/v1/incidents/{incident.id}/", {"excluded_from_reports": True}, format="json"
        )
        assert resp.status_code == 200
        assert hourly(m, rollups.floor_hour(start)).down_excluded == 6

        auth_api.patch(
            f"/api/v1/incidents/{incident.id}/", {"excluded_from_reports": False}, format="json"
        )
        assert hourly(m, rollups.floor_hour(start)).down_excluded == 0


class TestDailyRollup:
    def seed_two_hours(self, user):
        m = make_monitor(user)
        h1, h2 = H, H + timedelta(hours=1)
        for i, ms in enumerate((100, 200, 300, 400)):  # hour 1: p50=200, p95=400
            add_check(m, h1 + timedelta(minutes=i), duration=ms)
        add_check(m, h1 + timedelta(minutes=10), success=False)
        for i, ms in enumerate((1000, 2000)):  # hour 2: p50=1000, p95=2000
            add_check(m, h2 + timedelta(minutes=i), duration=ms)
        rollups.compute_hours(m, h1, h2 + timedelta(hours=1), now=NOW)
        rollups.rebuild_days(m, {H.date()})
        return m

    def test_sums_the_hours(self, user):
        m = self.seed_two_hours(user)
        day = CheckRollupDaily.objects.get(monitor=m, day=H.date())
        assert (day.up, day.down) == (6, 1)
        assert (day.response_count, day.response_sum_ms) == (6, 4000)
        assert (day.response_min_ms, day.response_max_ms) == (100, 2000)

    def test_percentiles_are_a_response_weighted_mean_of_the_hours(self, user):
        m = self.seed_two_hours(user)
        day = CheckRollupDaily.objects.get(monitor=m, day=H.date())
        # (200*4 + 1000*2) / 6 = 466.67 ; (400*4 + 2000*2) / 6 = 933.33
        assert (day.p50_ms, day.p95_ms) == (467, 933)

    def test_is_idempotent_and_follows_the_hours(self, user):
        m = self.seed_two_hours(user)
        rollups.rebuild_days(m, {H.date()})
        rollups.rebuild_days(m, {H.date()})
        assert CheckRollupDaily.objects.filter(monitor=m).count() == 1
        CheckRollupHourly.objects.filter(monitor=m).delete()
        rollups.rebuild_days(m, {H.date()})
        assert not CheckRollupDaily.objects.filter(monitor=m).exists()


class TestFleetRollup:
    def test_combines_monitors_and_incidents(self, user):
        a, b = make_monitor(user, "A"), make_monitor(user, "B")
        for m, fails in ((a, 1), (b, 0)):
            for i in range(4):
                add_check(m, H + timedelta(minutes=i), success=i >= fails)
        rollups.run_rollups_once(now=NOW)
        make_incident(a, H, H + timedelta(minutes=30))
        rollups.rebuild_fleet_days(user, {H.date()}, now=NOW)

        row = FleetSummaryDaily.objects.get(user=user, day=H.date())
        assert (row.monitors, row.up, row.down) == (2, 7, 1)
        assert (row.incidents_opened, row.incident_downtime_seconds) == (1, 1800)

    def test_an_incident_across_midnight_splits_its_downtime_but_opens_once(self, user):
        m = make_monitor(user)
        start = datetime(2026, 10, 3, 23, 0, tzinfo=UTC)
        make_incident(m, start, start + timedelta(hours=3))  # 23:00 to 02:00
        days = {date(2026, 10, 3), date(2026, 10, 4)}
        rollups.rebuild_fleet_days(user, days, now=NOW)
        first = FleetSummaryDaily.objects.get(user=user, day=date(2026, 10, 3))
        second = FleetSummaryDaily.objects.get(user=user, day=date(2026, 10, 4))
        assert (first.incidents_opened, first.incident_downtime_seconds) == (1, 3600)
        assert (second.incidents_opened, second.incident_downtime_seconds) == (0, 7200)

    def test_an_ongoing_incident_counts_up_to_now(self, user):
        m = make_monitor(user)
        make_incident(m, NOW - timedelta(hours=2))
        rollups.rebuild_fleet_days(user, {NOW.date()}, now=NOW)
        assert FleetSummaryDaily.objects.get(user=user).incident_downtime_seconds == 7200

    def test_excluded_incidents_are_left_out(self, user):
        m = make_monitor(user)
        make_incident(m, H, H + timedelta(hours=1), excluded=True)
        rollups.rebuild_fleet_days(user, {H.date()}, now=NOW)
        assert not FleetSummaryDaily.objects.exists()

    def test_other_users_are_separate(self, user, make_user):
        other = make_user("bob@example.com")
        make_incident(make_monitor(user), H, H + timedelta(minutes=10))
        make_incident(make_monitor(other, "Theirs"), H, H + timedelta(minutes=50))
        rollups.rebuild_fleet_days(user, {H.date()}, now=NOW)
        rollups.rebuild_fleet_days(other, {H.date()}, now=NOW)
        assert FleetSummaryDaily.objects.get(user=user).incident_downtime_seconds == 600
        assert FleetSummaryDaily.objects.get(user=other).incident_downtime_seconds == 3000


class TestRunRollups:
    def test_backfills_from_the_first_check(self, user):
        m = make_monitor(user)
        for days_ago in range(20, 0, -1):
            add_check(m, at(days=days_ago, hours=1))
        summary = rollups.run_rollups_once(now=NOW)
        assert summary.hours == 20 and summary.days == 20 and summary.failed == 0
        assert CheckRollupHourly.objects.filter(monitor=m).count() == 20
        assert CheckRollupDaily.objects.filter(monitor=m).count() == 20

    def test_running_it_twice_changes_nothing(self, user):
        m = make_monitor(user)
        for i in range(30):
            add_check(m, at(hours=3, minutes=-i * 2), success=i % 5 != 0, duration=100 + i)
        rollups.run_rollups_once(now=NOW)
        snapshot = lambda: (  # noqa: E731
            list(CheckRollupHourly.objects.order_by("hour").values_list("up", "down", "p95_ms")),
            list(CheckRollupDaily.objects.order_by("day").values_list("up", "down", "p95_ms")),
            list(FleetSummaryDaily.objects.order_by("day").values_list("up", "down")),
        )
        first = snapshot()
        rollups.run_rollups_once(now=NOW)
        rollups.run_rollups_once(now=NOW)
        assert snapshot() == first

    def test_a_late_check_is_picked_up_by_the_next_pass(self, user):
        m = make_monitor(user)
        add_check(m, at(minutes=20))  # 14:10
        rollups.run_rollups_once(now=NOW)
        add_check(m, at(minutes=5))  # 14:25, the same hour, written late
        rollups.run_rollups_once(now=NOW)
        assert CheckRollupHourly.objects.get(monitor=m).up == 2

    def test_full_rebuilds_everything(self, user):
        m = make_monitor(user)
        old = at(days=5)
        add_check(m, old)
        add_check(m, at(minutes=5))
        rollups.run_rollups_once(now=NOW)
        CheckRollupHourly.objects.filter(monitor=m, hour=rollups.floor_hour(old)).update(up=99)
        rollups.run_rollups_once(now=NOW)  # only recent hours: the old one is not revisited
        assert hourly(m, rollups.floor_hour(old)).up == 99
        rollups.run_rollups_once(now=NOW, full=True)
        assert hourly(m, rollups.floor_hour(old)).up == 1

    def test_one_broken_monitor_does_not_stop_the_rest(self, user, monkeypatch):
        bad, good = make_monitor(user, "Bad"), make_monitor(user, "Good")
        add_check(bad, at(hours=1))
        add_check(good, at(hours=1))
        real = rollups.compute_hours

        def flaky(monitor, *args, **kwargs):
            if monitor.pk == bad.pk:
                raise RuntimeError("boom")
            return real(monitor, *args, **kwargs)

        monkeypatch.setattr(rollups, "compute_hours", flaky)
        summary = rollups.run_rollups_once(now=NOW)
        assert summary.failed == 1
        assert CheckRollupHourly.objects.filter(monitor=good).exists()
        assert not CheckRollupHourly.objects.filter(monitor=bad).exists()

    def test_monitors_with_no_checks_are_fine(self, user):
        make_monitor(user)
        summary = rollups.run_rollups_once(now=NOW)
        assert (summary.monitors, summary.hours, summary.failed) == (1, 0, 0)


class TestRetention:
    def test_old_raw_checks_are_deleted_once_folded_in_and_their_rollups_survive(self, user):
        m = make_monitor(user)
        old, recent = at(days=120), at(days=2)
        add_check(m, old)
        add_check(m, old + timedelta(minutes=5), success=False)
        add_check(m, recent)
        summary = rollups.run_rollups_once(now=NOW)

        assert summary.pruned == 2
        assert list(Check.objects.values_list("started_at", flat=True)) == [recent]
        old_hour = CheckRollupHourly.objects.get(monitor=m, hour=rollups.floor_hour(old))
        assert (old_hour.up, old_hour.down) == (1, 1)
        assert CheckRollupDaily.objects.filter(monitor=m, day=old.date()).exists()

    def test_old_rollups_are_never_recomputed_from_missing_raw_rows(self, user):
        m = make_monitor(user)
        add_check(m, at(days=120))
        rollups.run_rollups_once(now=NOW)  # folded in, raw pruned
        rollups.run_rollups_once(now=NOW, full=True)
        rollups.compute_hours(m, at(days=121), at(days=119), now=NOW)
        assert (
            CheckRollupHourly.objects.get(monitor=m, hour=rollups.floor_hour(at(days=120))).up == 1
        )

    def test_pruning_can_be_switched_off(self, user):
        m = make_monitor(user)
        add_check(m, at(days=120))
        assert rollups.run_rollups_once(now=NOW, prune=False).pruned == 0
        assert Check.objects.count() == 1

    def test_a_monitor_that_failed_is_not_pruned(self, user, monkeypatch):
        m = make_monitor(user)
        add_check(m, at(days=120))
        monkeypatch.setattr(
            rollups, "compute_hours", lambda *a, **k: (_ for _ in ()).throw(RuntimeError())
        )
        rollups.run_rollups_once(now=NOW)
        assert Check.objects.count() == 1  # its data wasn't folded in, so it is kept


def seed_rollup_rows(monitor, days, *, up=288, down=0, skip_recent=False):
    """Weeks of history as rollups only: no raw checks exist at all. `skip_recent` leaves
    today and yesterday empty, because a monitor's analytics rebuild those two days from
    hourly rows (which a rollup-only seed doesn't have)."""
    today = timezone.now().astimezone(UTC).date()
    for n in range(2 if skip_recent else 0, days + (2 if skip_recent else 0)):
        day = today - timedelta(days=n)
        CheckRollupDaily.objects.create(
            monitor=monitor, day=day, up=up, down=down, response_count=up,
            response_sum_ms=up * 200, p50_ms=190, p95_ms=400,
        )  # fmt: skip


class TestMonitorAnalyticsEndpoint:
    def url(self, monitor, range_key=None):
        base = f"/api/v1/monitors/{monitor.id}/analytics/"
        return f"{base}?range={range_key}" if range_key else base

    @pytest.mark.parametrize(
        ("range_key", "points", "grain"),
        [("24h", 24, "hour"), ("7d", 168, "hour"), ("30d", 30, "day"), ("90d", 90, "day")],
    )
    def test_shapes_each_range(self, auth_api, user, range_key, points, grain):
        m = make_monitor(user)
        body = auth_api.get(self.url(m, range_key)).json()
        assert body["range"] == range_key and body["granularity"] == grain
        assert len(body["series"]) == points
        assert body["summary"]["uptime_percent"] is None  # no data yet is not "100%"
        assert all(p["checks"] == 0 and p["uptime_percent"] is None for p in body["series"])

    def test_defaults_to_seven_days(self, auth_api, user):
        assert auth_api.get(self.url(make_monitor(user))).json()["range"] == "7d"

    def test_rejects_unknown_ranges(self, auth_api, user):
        resp = auth_api.get(self.url(make_monitor(user), "1y"))
        assert resp.status_code == 400 and "range" in resp.json()["field_errors"]

    def test_is_private_to_the_owner(self, auth_api, make_user):
        other = make_monitor(make_user("bob@example.com"), "Theirs")
        assert auth_api.get(self.url(other)).status_code == 404

    def test_requires_sign_in(self, api, user):
        assert api.get(self.url(make_monitor(user))).status_code == 401

    def test_is_current_without_waiting_for_the_rollup_job(self, auth_api, user):
        m = make_monitor(user)
        now = timezone.now()
        for i in range(4):
            add_check(m, now - timedelta(minutes=i + 1), success=i != 0, duration=100 * (i + 1))
        summary = auth_api.get(self.url(m, "24h")).json()["summary"]
        assert summary["checks"] == 4 and summary["uptime_percent"] == 75.0
        assert summary["avg_response_ms"] == 300  # (200 + 300 + 400) / 3

    def test_reports_uptime_response_times_and_the_timing_breakdown(self, auth_api, user):
        m = make_monitor(user)
        now = timezone.now()
        add_check(
            m, now - timedelta(minutes=5), duration=120, dns_ms=10, tcp_ms=20, tls_ms=30, ttfb_ms=60
        )
        add_check(
            m, now - timedelta(minutes=4), duration=180, dns_ms=30, tcp_ms=40, tls_ms=50, ttfb_ms=60
        )
        body = auth_api.get(self.url(m, "24h")).json()
        assert body["timing"] == {
            "samples": 2,
            "dns_ms": 20,
            "tcp_ms": 30,
            "tls_ms": 40,
            "ttfb_ms": 60,
        }
        # Both checks are one hour's p95 (180) if they share a clock hour, otherwise the
        # response-weighted mean of two hours (150): either is right depending on when this runs.
        assert body["summary"]["p95_ms"] in (150, 180)

    def test_incident_figures_come_from_incidents_and_skip_excluded_ones(self, auth_api, user):
        m = make_monitor(user)
        now = timezone.now()
        make_incident(m, now - timedelta(hours=10), now - timedelta(hours=9, minutes=50))  # 10 min
        make_incident(m, now - timedelta(hours=6), now - timedelta(hours=5, minutes=30))  # 30 min
        make_incident(m, now - timedelta(hours=3), now - timedelta(hours=2), excluded=True)
        s = auth_api.get(self.url(m, "24h")).json()["summary"]
        assert s["incidents"] == 2
        assert s["downtime_seconds"] == 2400
        assert s["mttr_seconds"] == 1200  # mean of 10 and 30 minutes
        assert s["longest_incident_seconds"] == 1800
        assert s["excluded_incidents"] == 1

    def test_an_incident_that_began_before_the_range_only_counts_its_overlap(self, auth_api, user):
        m = make_monitor(user)
        now = timezone.now()
        make_incident(m, now - timedelta(hours=30), now - timedelta(hours=22))
        s = auth_api.get(self.url(m, "24h")).json()["summary"]
        # The range starts "24 hours before the request", a moment after this test built its
        # data (seconds, on a distant database), so the overlap is two hours less that moment.
        assert 2 * 3600 - 60 <= s["downtime_seconds"] <= 2 * 3600 and s["incidents"] == 0

    def test_an_ongoing_incident_counts_up_to_now(self, auth_api, user):
        m = make_monitor(user)
        make_incident(m, timezone.now() - timedelta(hours=1))
        s = auth_api.get(self.url(m, "24h")).json()["summary"]
        assert 3590 <= s["downtime_seconds"] <= 3700 and s["mttr_seconds"] is None

    def test_a_month_of_history_is_served_from_rollups_alone(self, auth_api, user):
        m = make_monitor(user)
        seed_rollup_rows(m, 28, up=287, down=1, skip_recent=True)
        assert not Check.objects.exists()
        body = auth_api.get(self.url(m, "30d")).json()
        assert body["summary"]["checks"] == 28 * 288
        assert body["summary"]["uptime_percent"] == round(100 * 287 / 288, 3)
        assert body["summary"]["avg_response_ms"] == 200
        assert sum(p["down"] for p in body["series"]) == 28

    def test_never_reads_raw_checks_outside_the_last_two_days(self, auth_api, user):
        m = make_monitor(user)
        seed_rollup_rows(m, 88, skip_recent=True)
        with CaptureQueriesContext(connection) as queries:
            assert auth_api.get(self.url(m, "90d")).status_code == 200
        touching_checks = [q["sql"] for q in queries if 'FROM "monitoring_check"' in q["sql"]]
        floor = timezone.now() - timedelta(hours=48)
        for sql in touching_checks:
            stamps = re.findall(r"'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", sql)
            assert stamps, "a raw-check query had no time bound"
            assert all(datetime.fromisoformat(s).replace(tzinfo=UTC) >= floor for s in stamps), sql


class TestFleetEndpoint:
    def seed(self, user):
        shop, api_, blog = (make_monitor(user, n) for n in ("Shop", "API", "Blog"))
        now = timezone.now()
        make_incident(shop, now - timedelta(days=3, hours=1), now - timedelta(days=3))  # 1 h
        make_incident(api_, now - timedelta(days=9, hours=5), now - timedelta(days=9))  # 5 h
        make_incident(api_, now - timedelta(days=2, hours=1), now - timedelta(days=2, minutes=30))
        make_incident(blog, now - timedelta(days=1, hours=2), now - timedelta(days=1))  # 2 h
        make_incident(
            blog, now - timedelta(days=5, hours=9), now - timedelta(days=5), excluded=True
        )
        for m, down in ((shop, 1), (api_, 6), (blog, 2)):
            seed_rollup_rows(m, 30, up=288 - down, down=down)
        for u in {shop.user}:
            rollups.rebuild_fleet_days(
                u, {(now - timedelta(days=n)).astimezone(UTC).date() for n in range(31)}
            )
        return shop, api_, blog

    def test_names_the_monitor_that_caused_the_most_downtime(self, auth_api, user):
        self.seed(user)
        body = auth_api.get(f"{FLEET}?range=30d").json()
        offenders = body["top_offenders"]
        assert [o["monitor"]["name"] for o in offenders] == ["API", "Blog", "Shop"]
        assert [o["downtime_seconds"] for o in offenders] == [5 * 3600 + 1800, 2 * 3600, 3600]
        assert [o["incidents"] for o in offenders] == [2, 1, 1]
        assert offenders[0]["uptime_percent"] == round(100 * (288 - 6) / 288, 3)

    def test_a_shorter_range_changes_who_is_on_top(self, auth_api, user):
        self.seed(user)
        offenders = auth_api.get(f"{FLEET}?range=7d").json()["top_offenders"]
        # The 5-hour API outage was nine days ago, outside the week.
        assert [o["monitor"]["name"] for o in offenders] == ["Blog", "Shop", "API"]

    def test_excluded_incidents_are_not_blamed(self, auth_api, user):
        self.seed(user)
        body = auth_api.get(f"{FLEET}?range=30d").json()
        blog = next(o for o in body["top_offenders"] if o["monitor"]["name"] == "Blog")
        assert blog["downtime_seconds"] == 2 * 3600  # not 11 hours
        assert body["summary"]["excluded_incidents"] == 1

    def test_summary_and_daily_series(self, auth_api, user):
        self.seed(user)
        body = auth_api.get(f"{FLEET}?range=30d").json()
        assert (
            len(body["daily"]) == 30
            and body["daily"][-1]["date"] == timezone.now().astimezone(UTC).date().isoformat()
        )
        assert body["summary"]["incidents"] == 4
        assert body["summary"]["downtime_seconds"] == 5 * 3600 + 1800 + 2 * 3600 + 3600
        assert body["summary"]["monitors"] == 3
        assert body["summary"]["checks"] == 3 * 30 * 288
        assert sum(d["incidents"] for d in body["daily"]) == 4
        assert body["data_as_of"]

    @pytest.mark.parametrize(("range_key", "days"), [("7d", 7), ("30d", 30), ("90d", 90)])
    def test_ranges(self, auth_api, range_key, days):
        assert len(auth_api.get(f"{FLEET}?range={range_key}").json()["daily"]) == days

    def test_defaults_to_thirty_days_and_rejects_others(self, auth_api):
        assert auth_api.get(FLEET).json()["range"] == "30d"
        assert auth_api.get(f"{FLEET}?range=24h").status_code == 400

    def test_a_new_account_gets_an_empty_but_valid_answer(self, auth_api):
        body = auth_api.get(FLEET).json()
        assert body["top_offenders"] == [] and body["summary"]["uptime_percent"] is None
        assert body["summary"]["monitors"] == 0 and body["data_as_of"] is None

    def test_only_your_own_monitors_appear(self, auth_api, user, make_user):
        other = make_user("bob@example.com")
        make_incident(
            make_monitor(other, "Theirs"), timezone.now() - timedelta(hours=5), timezone.now()
        )
        make_incident(
            make_monitor(user, "Mine"), timezone.now() - timedelta(hours=1), timezone.now()
        )
        names = [o["monitor"]["name"] for o in auth_api.get(FLEET).json()["top_offenders"]]
        assert names == ["Mine"]

    def test_requires_sign_in(self, api):
        assert api.get(FLEET).status_code == 401

    def test_never_touches_raw_checks_and_uses_a_fixed_number_of_queries(self, auth_api, user):
        self.seed(user)
        with CaptureQueriesContext(connection) as small:
            auth_api.get(f"{FLEET}?range=7d")
        with CaptureQueriesContext(connection) as large:
            auth_api.get(f"{FLEET}?range=90d")
        assert not [q for q in large if 'FROM "monitoring_check"' in q["sql"]]
        assert len(large) <= 12 and abs(len(large) - len(small)) <= 1  # not one query per day


class TestInternalTriggerAndCommands:
    URL = "/internal/run-rollups/"

    def test_unsigned_calls_are_refused(self, api):
        assert api.post(self.URL).status_code == 401
        resp = api.post(self.URL, HTTP_X_ROOTPULSE_SIGNATURE="t=1,v1=bad")
        assert resp.status_code == 401

    def test_a_signed_call_runs_the_job(self, api, user):
        m = make_monitor(user)
        add_check(m, timezone.now() - timedelta(minutes=5))
        resp = api.post(self.URL, HTTP_X_ROOTPULSE_SIGNATURE=signing.sign("test-scheduler-secret"))
        assert resp.status_code == 200
        assert resp.json()["monitors"] == 1 and resp.json()["hours"] >= 1
        assert CheckRollupHourly.objects.filter(monitor=m).exists()

    def test_it_is_not_in_the_public_docs(self, client):
        schema = client.get("/api/v1/schema/", HTTP_ACCEPT="application/json").json()
        assert not any("run-rollups" in path for path in schema["paths"])

    def test_the_command_reports_what_it_did(self, user):
        add_check(make_monitor(user), timezone.now() - timedelta(minutes=5))
        out = StringIO()
        call_command("run_rollups_once", stdout=out)
        assert "monitors=1" in out.getvalue() and "failed=0" in out.getvalue()

    def test_run_engine_includes_rollups(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            "monitoring.management.commands.run_engine.run_rollups_once", lambda: calls.append(1)
        )
        call_command("run_engine", stdout=StringIO())
        assert calls == [1]


class TestPercentileHelper:
    @pytest.mark.parametrize(
        ("values", "q", "expected"),
        [
            ([], 0.5, None),
            ([7], 0.95, 7),
            ([1, 2, 3, 4], 0.5, 2),
            ([1, 2, 3, 4], 0.95, 4),
            (list(range(1, 101)), 0.95, 95),
        ],
    )
    def test_nearest_rank(self, values, q, expected):
        assert rollups.percentile(values, q) == expected
