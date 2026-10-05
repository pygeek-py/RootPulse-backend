"""Reports: CSV and PDF files for a date range.

The files are parsed back (CSV with the csv module, PDF with pypdf) and compared with numbers
worked out by hand and with what the analytics endpoints say for the same period, because a
report that disagrees with the dashboard is worse than none."""

import csv
import io
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from django.utils import timezone
from pypdf import PdfReader
from rest_framework.test import APIClient

from analytics import rollups
from analytics.queries import fleet_analytics
from incidents.models import Incident, IncidentComment
from monitoring.models import Check, Monitor
from reports import pdf, services
from statuspages.models import StatusPage, StatusPageAnnouncement, StatusPageComponent

pytestmark = pytest.mark.django_db

NOW = datetime(2026, 10, 5, 14, 30, tzinfo=UTC)
TODAY = NOW.date()


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


def add_check(monitor, when, success=True, duration=100):
    return Check.objects.create(
        monitor=monitor,
        region="primary",
        started_at=when,
        success=success,
        duration_ms=duration if success else None,
        status_detail="ok" if success else "timeout",
    )


def day_at(days_ago, hour=10, minute=0):
    return datetime.combine(TODAY - timedelta(days=days_ago), datetime.min.time(), UTC).replace(
        hour=hour, minute=minute
    )


def make_incident(monitor, started, ended=None, excluded=False, reason="timeout"):
    return Incident.objects.create(
        monitor=monitor,
        started_at=started,
        ended_at=ended,
        reason=reason,
        resolution="recovered" if ended else "",
        excluded_from_reports=excluded,
    )


def seed(user):
    """Shop: 5 days of 10 checks a day at 100 ms, with 2 failures and a 30-minute incident on day 2.
    Search: 3 checks yesterday, all up. Returns (shop, search)."""
    shop = make_monitor(user, "Shop")
    search = make_monitor(user, "Search", target="https://search.example.com")
    for d in range(5, 0, -1):
        for i in range(10):
            ok = not (d == 2 and i < 2)
            add_check(shop, day_at(d, 9, i * 5), success=ok)
    for i in range(3):
        add_check(search, day_at(1, 12, i * 5), duration=300)
    make_incident(shop, day_at(2, 9, 0), day_at(2, 9, 30))
    rollups.run_rollups_once(now=NOW)
    return shop, search


@pytest.fixture
def frozen(monkeypatch):
    """The API reads the clock in two places; pin both so dates in tests are exact."""
    monkeypatch.setattr(timezone, "now", lambda: NOW)


def span(days_back, to_back=0):
    return {
        "start_date": (TODAY - timedelta(days=days_back)).isoformat(),
        "end_date": (TODAY - timedelta(days=to_back)).isoformat(),
    }


def read_csv(response):
    return list(csv.reader(io.StringIO(response.content.decode("utf-8"))))


def pdf_text(content: bytes) -> str:
    reader = PdfReader(io.BytesIO(content))
    return "\n".join(page.extract_text() for page in reader.pages)


# --- the range -----------------------------------------------------------------------------


class TestRange:
    @pytest.mark.parametrize(
        ("start", "end", "field"),
        [
            (None, "2026-10-01", "start_date"),
            ("2026-10-01", None, "end_date"),
            ("yesterday", "2026-10-01", "start_date"),
            ("2026-10-01", "2026-13-40", "end_date"),
            ("2026-10-03", "2026-10-01", "end_date"),
            ("2026-10-01", "2026-10-06", "end_date"),
            ("2025-01-01", "2026-10-05", "start_date"),
        ],
    )
    def test_bad_ranges_are_refused_with_the_field_named(self, auth_api, frozen, start, end, field):
        params = {k: v for k, v in {"start_date": start, "end_date": end}.items() if v}
        for path in ("uptime.csv", "uptime.pdf"):
            r = auth_api.get(f"/api/v1/reports/{path}", params)
            assert r.status_code == 400 and field in r.json()["field_errors"], path

    def test_a_single_day_and_a_whole_year_are_fine(self, auth_api, frozen):
        assert auth_api.get("/api/v1/reports/uptime.csv", span(0)).status_code == 200
        assert auth_api.get("/api/v1/reports/uptime.csv", span(365)).status_code == 200


# --- the figures ---------------------------------------------------------------------------


class TestFigures:
    def test_numbers_worked_out_by_hand(self, user):
        shop, search = seed(user)
        start, end = TODAY - timedelta(days=4), TODAY
        report = services.uptime_report(user, start, end, now=NOW)

        rows = {m["name"]: m for m in report["monitors"]}
        assert set(rows) == {"Shop", "Search"}
        # Days 5..1: 50 checks, 2 failed: 48 / 50.
        # (day 5 is outside the window, so 40 checks, 2 failed: 38 / 40.)
        assert (rows["Shop"]["checks"], rows["Shop"]["up"], rows["Shop"]["down"]) == (40, 38, 2)
        assert rows["Shop"]["uptime_percent"] == 95.0
        assert rows["Shop"]["avg_response_ms"] == 100
        assert rows["Shop"]["incidents"] == 1
        assert rows["Shop"]["downtime_seconds"] == 1800
        assert rows["Shop"]["mttr_seconds"] == 1800
        assert rows["Search"]["checks"] == 3 and rows["Search"]["uptime_percent"] == 100.0
        assert rows["Search"]["avg_response_ms"] == 300 and rows["Search"]["incidents"] == 0

        s = report["summary"]
        assert s["checks"] == 43 and s["incidents"] == 1 and s["downtime_seconds"] == 1800
        assert s["uptime_percent"] == round(100 * 41 / 43, 3)
        assert report["period"] == {"start": start.isoformat(), "end": end.isoformat(), "days": 5}

    def test_only_the_chosen_days_count(self, user):
        seed(user)
        only_day_two = services.uptime_report(
            user, TODAY - timedelta(days=2), TODAY - timedelta(days=2), now=NOW
        )
        shop = only_day_two["monitors"][0]
        assert shop["name"] == "Shop" and shop["checks"] == 10 and shop["down"] == 2
        assert [m["name"] for m in only_day_two["monitors"]] == ["Shop"]  # Search had no data

    def test_matches_the_dashboards_for_the_same_period(self, user):
        """The checkpoint: a report and the analytics page agree."""
        seed(user)
        fleet = fleet_analytics(user, "7d", now=NOW)["summary"]
        report = services.uptime_report(user, TODAY - timedelta(days=6), TODAY, now=NOW)["summary"]
        for key in (
            "uptime_percent",
            "checks",
            "avg_response_ms",
            "incidents",
            "downtime_seconds",
            "mttr_seconds",
            "longest_incident_seconds",
            "excluded_incidents",
        ):
            assert report[key] == fleet[key], key

    def test_an_incident_that_began_before_the_range_counts_only_its_time_inside(self, user):
        shop = make_monitor(user)
        add_check(shop, day_at(3))
        make_incident(shop, day_at(3, 23, 0), day_at(2, 1, 0))  # 23:00 to 01:00 across midnight
        rollups.run_rollups_once(now=NOW)
        inside = services.uptime_report(
            user, TODAY - timedelta(days=2), TODAY - timedelta(days=2), now=NOW
        )
        assert inside["monitors"][0]["downtime_seconds"] == 3600  # the hour after midnight
        assert inside["monitors"][0]["incidents"] == 0  # it started the day before
        assert inside["incidents"] == []  # and is listed under the day it began

    def test_an_ongoing_incident_counts_up_to_now(self, user):
        shop = make_monitor(user, status="down")
        add_check(shop, day_at(0, 8))
        make_incident(shop, day_at(0, 12, 30))
        rollups.run_rollups_once(now=NOW)
        report = services.uptime_report(user, TODAY, TODAY, now=NOW)
        assert report["monitors"][0]["downtime_seconds"] == 2 * 3600  # 12:30 to 14:30
        assert report["incidents"][0]["ongoing"] is True
        assert report["incidents"][0]["duration_seconds"] == 2 * 3600

    def test_excluded_incidents_are_left_out_and_counted(self, user):
        shop = make_monitor(user)
        add_check(shop, day_at(1))
        make_incident(shop, day_at(1, 10), day_at(1, 11), excluded=True)
        rollups.run_rollups_once(now=NOW)
        report = services.uptime_report(user, TODAY - timedelta(days=1), TODAY, now=NOW)
        assert report["summary"]["incidents"] == 0 and report["summary"]["downtime_seconds"] == 0
        assert report["summary"]["excluded_incidents"] == 1
        assert report["incidents"] == []

    def test_other_peoples_monitors_never_appear(self, user, make_user):
        seed(user)
        carol = make_user("carol@example.com")
        make_incident(make_monitor(carol, "Carols"), day_at(1), day_at(1, 11))
        report = services.uptime_report(user, TODAY - timedelta(days=6), TODAY, now=NOW)
        assert "Carols" not in str(report)

    def test_one_monitor_only(self, user):
        shop, _ = seed(user)
        report = services.uptime_report(
            user, TODAY - timedelta(days=6), TODAY, monitor_id=shop.id, now=NOW
        )
        assert [m["name"] for m in report["monitors"]] == ["Shop"]
        assert report["summary"]["checks"] == 50 - 0  # all five days of Shop

    def test_no_data_gives_an_empty_report_not_an_error(self, user):
        report = services.uptime_report(user, TODAY - timedelta(days=6), TODAY, now=NOW)
        assert report["monitors"] == [] and report["summary"]["uptime_percent"] is None


# --- CSV -----------------------------------------------------------------------------------


class TestUptimeCsv:
    def test_rows_and_totals(self, auth_api, user, frozen):
        seed(user)
        r = auth_api.get("/api/v1/reports/uptime.csv", span(4))
        assert r.status_code == 200 and r["Content-Type"].startswith("text/csv")
        assert r["Content-Disposition"] == (
            f'attachment; filename="rootpulse-uptime-{span(4)["start_date"]}_to_'
            f'{span(4)["end_date"]}.csv"'
        )
        assert r["Cache-Control"] == "no-store"
        rows = read_csv(r)
        header = rows[0]
        assert header[:4] == ["monitor", "type", "target", "checks"]
        by_name = {row[0]: dict(zip(header, row, strict=True)) for row in rows[1:]}
        assert by_name["Shop"]["checks"] == "40" and by_name["Shop"]["uptime_percent"] == "95.0"
        assert by_name["Shop"]["downtime_seconds"] == "1800"
        assert by_name["Shop"]["target"] == "https://shop.example.com"
        total = by_name["All monitors"]
        assert total["checks"] == "43" and total["incidents"] == "1"

    def test_it_agrees_with_the_analytics_endpoint(self, auth_api, user, frozen):
        seed(user)
        fleet = auth_api.get("/api/v1/analytics/fleet/", {"range": "7d"}).json()["summary"]
        rows = read_csv(auth_api.get("/api/v1/reports/uptime.csv", span(6)))
        total = dict(zip(rows[0], rows[-1], strict=True))
        assert float(total["uptime_percent"]) == fleet["uptime_percent"]
        assert int(total["incidents"]) == fleet["incidents"]
        assert int(total["downtime_seconds"]) == fleet["downtime_seconds"]

    def test_spreadsheet_formulas_are_defused(self, auth_api, user, frozen):
        m = make_monitor(user, '=HYPERLINK("http://evil")', target="@cmd")
        add_check(m, day_at(1))
        rollups.run_rollups_once(now=NOW)
        rows = read_csv(auth_api.get("/api/v1/reports/uptime.csv", span(2)))
        assert rows[1][0].startswith("'=") and rows[1][2] == "'@cmd"

    def test_monitor_filter_and_bad_ids(self, auth_api, user, frozen):
        shop, _ = seed(user)
        rows = read_csv(
            auth_api.get("/api/v1/reports/uptime.csv", {**span(4), "monitor_id": str(shop.id)})
        )
        assert [r[0] for r in rows[1:]] == ["Shop", "All monitors"]
        r = auth_api.get("/api/v1/reports/uptime.csv", {**span(4), "monitor_id": "nope"})
        assert r.status_code == 400
        stranger = auth_api.get(
            "/api/v1/reports/uptime.csv", {**span(4), "monitor_id": str(uuid.uuid4())}
        )
        assert [r[0] for r in read_csv(stranger)[1:]] == ["All monitors"]

    def test_a_signed_out_caller_gets_nothing(self, user, frozen):
        anon = APIClient(HTTP_ORIGIN="http://localhost:3000")
        for path in ("uptime.csv", "uptime.pdf", "incidents.csv"):
            assert anon.get(f"/api/v1/reports/{path}", span(4)).status_code == 401
        assert anon.get(f"/api/v1/reports/status-page/{uuid.uuid4()}.pdf").status_code == 401

    def test_downloads_are_rate_limited(self, auth_api, frozen):
        codes = [auth_api.get("/api/v1/reports/uptime.csv", span(1)).status_code for _ in range(22)]
        assert codes[:20] == [200] * 20 and 429 in codes[20:]


class TestIncidentsCsv:
    def test_it_is_the_incident_export_under_reports(self, auth_api, user):
        shop, _ = seed(user)
        make_incident(shop, day_at(1, 8), day_at(1, 9), reason="dns_error")
        params = {"monitor_id": str(shop.id)}
        a = auth_api.get("/api/v1/reports/incidents.csv", params)
        b = auth_api.get("/api/v1/incidents/export/", params)
        assert a.status_code == 200 and a["Content-Type"].startswith("text/csv")
        assert read_csv(a) == read_csv(b) and len(read_csv(a)) == 3

    def test_the_date_filters_pick_the_incidents_in_the_range(self, auth_api, user):
        shop, _ = seed(user)
        rows = read_csv(
            auth_api.get(
                "/api/v1/reports/incidents.csv",
                {
                    "start_date": (TODAY - timedelta(days=2)).isoformat(),
                    "end_date": (TODAY - timedelta(days=2)).isoformat(),
                },
            )
        )
        assert len(rows) == 2  # header and the one incident from day 2
        assert rows[1][1] == "Shop" and rows[1][6] == "1800"

    def test_only_your_incidents(self, auth_api, user, make_user):
        make_incident(
            make_monitor(make_user("carol@example.com"), "Carols"), day_at(1), day_at(1, 12)
        )
        assert len(read_csv(auth_api.get("/api/v1/reports/incidents.csv"))) == 1


# --- PDF -----------------------------------------------------------------------------------


class TestUptimePdf:
    def test_it_is_a_real_pdf_with_the_period_and_the_numbers(self, auth_api, user, frozen):
        seed(user)
        r = auth_api.get("/api/v1/reports/uptime.pdf", span(4))
        assert r.status_code == 200 and r["Content-Type"] == "application/pdf"
        assert r.content.startswith(b"%PDF-")
        assert r["Content-Disposition"].endswith('.pdf"')
        text = pdf_text(r.content)
        assert "Uptime report" in text
        assert f"{span(4)['start_date']} to {span(4)['end_date']} (5 days, UTC days)" in text
        for expected in ("Shop", "Search", "95%", "100%", "30 min", "Timed out", "Resolved"):
            assert expected in text, expected
        # the summary says what the CSV says
        assert "Checks 43" in text.replace("\n", " ") or "43" in text

    def test_the_metadata_names_the_report(self, auth_api, user, frozen):
        seed(user)
        reader = PdfReader(io.BytesIO(auth_api.get("/api/v1/reports/uptime.pdf", span(4)).content))
        assert (
            reader.metadata.title
            == f"Uptime report {span(4)['start_date']} to {span(4)['end_date']}"
        )
        assert reader.metadata.author == "RootPulse"

    def test_an_empty_period_still_makes_a_valid_report(self, auth_api, frozen):
        r = auth_api.get("/api/v1/reports/uptime.pdf", span(2))
        text = pdf_text(r.content)
        assert "No monitor had checks or incidents in this period." in text
        assert "No incidents in this period." in text

    def test_markup_and_odd_characters_are_shown_as_text_and_do_not_break_it(
        self, auth_api, user, frozen
    ):
        m = make_monitor(user, "<b>Bold</b> & <i>co</i> 日本語 café")
        add_check(m, day_at(1))
        make_incident(m, day_at(1, 9), day_at(1, 10))
        rollups.run_rollups_once(now=NOW)
        r = auth_api.get("/api/v1/reports/uptime.pdf", span(2))
        assert r.status_code == 200
        text = pdf_text(r.content)
        assert "<b>Bold</b> & <i>co</i>" in text and "café" in text
        assert "日" not in text  # drawn as ? rather than as a missing-glyph box

    def test_times_are_shown_in_the_users_time_zone(self, auth_api, user, frozen):
        user.timezone = "Asia/Tokyo"
        user.save()
        m = make_monitor(user)
        add_check(m, day_at(1))
        make_incident(m, day_at(1, 9), day_at(1, 10))
        rollups.run_rollups_once(now=NOW)
        text = pdf_text(auth_api.get("/api/v1/reports/uptime.pdf", span(2)).content)
        assert "18:00 JST" in text  # 09:00 UTC

    def test_a_long_incident_list_runs_over_pages_and_says_when_it_is_cut(self, user):
        m = make_monitor(user, "Flappy")
        add_check(m, day_at(1))
        base = day_at(1, 0, 0)
        Incident.objects.bulk_create(
            Incident(
                monitor=m,
                started_at=base + timedelta(minutes=2 * i),
                ended_at=base + timedelta(minutes=2 * i + 1),
                reason="timeout",
                resolution="recovered",
            )
            for i in range(520)
        )
        rollups.run_rollups_once(now=NOW)
        report = services.uptime_report(user, TODAY - timedelta(days=1), TODAY, now=NOW)
        assert report["incidents_truncated"] and len(report["incidents"]) == 500
        content = pdf.uptime_report_pdf(report, "UTC")
        reader = PdfReader(io.BytesIO(content))
        assert len(reader.pages) > 3
        assert "most recent 500 incidents" in pdf_text(content)

    def test_excluded_incidents_are_mentioned_not_listed(self, auth_api, user, frozen):
        m = make_monitor(user, "Quiet")
        add_check(m, day_at(1))
        make_incident(m, day_at(1, 9), day_at(1, 10), excluded=True, reason="dns_error")
        rollups.run_rollups_once(now=NOW)
        text = pdf_text(auth_api.get("/api/v1/reports/uptime.pdf", span(2)).content).replace(
            "\n", " "
        )
        assert "1 incident(s) you marked as excluded" in text
        assert "DNS lookup failed" not in text


class TestStatusPagePdf:
    def make_page(self, user, **kwargs):
        page = StatusPage.objects.create(
            user=user, name="Acme status", slug="acme", is_public=False, **kwargs
        )
        return page

    def test_a_snapshot_of_the_page(self, auth_api, user):
        page = self.make_page(user, branding={"description": "Live status of Acme"})
        up = make_monitor(user, "Internal web", status="up")
        down = make_monitor(user, "Internal api", status="down")
        StatusPageComponent.objects.create(
            page=page, monitor=up, display_name="Website", group="Public", order=0
        )
        StatusPageComponent.objects.create(page=page, monitor=down, display_name="API", order=1)
        incident = make_incident(down, timezone.now() - timedelta(hours=2), None)
        IncidentComment.objects.create(
            incident=incident, author=user, body="We are on it.", visible_on_status_page=True
        )
        IncidentComment.objects.create(
            incident=incident, author=user, body="Private note", visible_on_status_page=False
        )
        StatusPageAnnouncement.objects.create(
            page=page, kind="incident", title="API trouble", body="Investigating."
        )
        r = auth_api.get(f"/api/v1/reports/status-page/{page.id}.pdf")
        assert r.status_code == 200 and r["Content-Type"] == "application/pdf"
        assert r["Content-Disposition"] == 'attachment; filename="acme-status.pdf"'
        text = pdf_text(r.content)
        for expected in (
            "Acme status",
            "Live status of Acme",
            "Partial outage",
            "Website",
            "Public",
            "Operational",
            "API",
            "Outage",
            "API trouble",
            "Investigating.",
            "We are on it.",
            "Ongoing",
        ):
            assert expected in text, expected
        # nothing a visitor couldn't see
        for hidden in ("Private note", "Internal web", "Internal api", "shop.example.com"):
            assert hidden not in text, hidden

    def test_a_draft_and_a_protected_page_can_still_be_exported_by_the_owner(self, auth_api, user):
        page = self.make_page(user)
        r = auth_api.get(f"/api/v1/reports/status-page/{page.id}.pdf")
        assert r.status_code == 200
        assert "Nothing is listed on this page yet." in pdf_text(r.content)

    def test_the_password_never_ends_up_in_the_file(self, auth_api, user):
        from statuspages import services as page_services

        page = self.make_page(user)
        page_services.set_password(page, "correct horse")
        page.save()
        content = auth_api.get(f"/api/v1/reports/status-page/{page.id}.pdf").content
        assert b"correct horse" not in content and page.password_hash.encode() not in content

    def test_someone_elses_page_is_a_404(self, auth_api, user, make_user):
        page = self.make_page(make_user("carol@example.com"))
        assert auth_api.get(f"/api/v1/reports/status-page/{page.id}.pdf").status_code == 404
        assert auth_api.get(f"/api/v1/reports/status-page/{uuid.uuid4()}.pdf").status_code == 404


class TestPdfText:
    @pytest.mark.parametrize(
        ("raw", "shown"),
        [
            ("plain", "plain"),
            ("a < b & c > d", "a &lt; b &amp; c &gt; d"),
            ("<b>x</b>", "&lt;b&gt;x&lt;/b&gt;"),
            ("café", "café"),
            ("日本", "??"),
            ("tab\x00nul\x07bell", "tabnulbell"),
            (None, ""),
        ],
    )
    def test_text_is_escaped_and_drawable(self, raw, shown):
        assert pdf.text(raw) == shown

    @pytest.mark.parametrize(
        ("seconds", "words"),
        [
            (None, "-"),
            (0, "0 s"),
            (45, "45 s"),
            (90, "1 min 30 s"),
            (840, "14 min"),
            (5400, "1 h 30 min"),
            (7200, "2 h"),
            (200000, "2 d 7 h"),
        ],
    )
    def test_durations(self, seconds, words):
        assert pdf.duration(seconds) == words

    @pytest.mark.parametrize(
        ("value", "words"), [(None, "No data"), (100.0, "100%"), (99.95, "99.95%"), (95.0, "95%")]
    )
    def test_percentages(self, value, words):
        assert pdf.percent(value) == words

    def test_an_unknown_time_zone_falls_back_to_utc(self):
        assert pdf.when(NOW, "Not/AZone").endswith("UTC")
