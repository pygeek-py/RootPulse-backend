import csv
import io
import uuid
from datetime import timedelta

import pytest
from django.utils import timezone

from accounts.models import AuditLog
from incidents.models import Incident, IncidentComment, IncidentEvent
from monitoring.models import Check, Monitor

pytestmark = pytest.mark.django_db

URL = "/api/v1/incidents/"


def monitor_for(user, name="Site", **kwargs):
    fields = {
        "type": "http",
        "target": "https://example.com",
        "interval_seconds": 300,
        "config": {},
        "status": "up",
        "next_check_at": timezone.now(),
    }
    fields.update(kwargs)
    return Monitor.objects.create(user=user, name=name, **fields)


def incident(
    monitor, started_minutes_ago=60, duration_minutes=10, reason="status_mismatch", **kwargs
):
    started = timezone.now() - timedelta(minutes=started_minutes_ago)
    check = Check.objects.create(
        monitor=monitor,
        region="primary",
        started_at=started,
        success=False,
        status_detail=reason,
        status_code=503,
        duration_ms=90,
        ttfb_ms=80,
        dns_ms=3,
    )
    ended = None if duration_minutes is None else started + timedelta(minutes=duration_minutes)
    obj = Incident.objects.create(
        monitor=monitor,
        started_at=started,
        ended_at=ended,
        reason=reason,
        status_code=503,
        opened_by=check,
        resolution="recovered" if ended else "",
        **kwargs,
    )
    IncidentEvent.objects.create(
        incident=obj,
        kind="opened",
        created_at=started,
        metadata={"region": "primary", "status_code": 503},
    )
    if ended:
        IncidentEvent.objects.create(
            incident=obj,
            kind="resolved",
            created_at=ended,
            metadata={"duration_seconds": duration_minutes * 60},
        )
    return obj


class TestAccess:
    def test_everything_needs_a_sign_in(self, api, user):
        obj = incident(monitor_for(user))
        for method, path in [
            ("get", URL),
            ("get", f"{URL}{obj.id}/"),
            ("patch", f"{URL}{obj.id}/"),
            ("post", f"{URL}{obj.id}/comments/"),
            ("post", f"{URL}{obj.id}/postmortem/"),
            ("get", f"{URL}export/"),
        ]:
            assert getattr(api, method)(path).status_code == 401, path

    def test_users_only_see_their_own_incidents(self, auth_api, make_user):
        theirs = incident(monitor_for(make_user("other@example.com")))
        assert auth_api.get(URL).json()["count"] == 0
        for method, path in [
            ("get", f"{URL}{theirs.id}/"),
            ("patch", f"{URL}{theirs.id}/"),
            ("post", f"{URL}{theirs.id}/comments/"),
            ("post", f"{URL}{theirs.id}/postmortem/"),
        ]:
            assert getattr(auth_api, method)(path, {}, format="json").status_code == 404, path
        assert auth_api.get(f"{URL}export/").content.decode().count("\n") == 1  # headers only

    def test_incidents_cannot_be_created_or_deleted_through_the_api(self, auth_api, user):
        obj = incident(monitor_for(user))
        assert auth_api.post(URL, {}, format="json").status_code == 405
        assert auth_api.delete(f"{URL}{obj.id}/").status_code == 405
        assert auth_api.put(f"{URL}{obj.id}/", {}, format="json").status_code == 405


class TestList:
    @pytest.fixture
    def three(self, user):
        site = monitor_for(user, "Site")
        api = monitor_for(user, "API", target="https://api.example.com")
        a = incident(site, started_minutes_ago=300, duration_minutes=5)
        b = incident(api, started_minutes_ago=120, duration_minutes=90)
        c = incident(site, started_minutes_ago=10, duration_minutes=None)
        return site, api, a, b, c

    def ids(self, resp):
        return [i["id"] for i in resp.json()["results"]]

    def test_newest_first_by_default_with_the_fields_the_ui_needs(self, auth_api, three):
        _, api, a, b, c = three
        resp = auth_api.get(URL)
        assert resp.json()["count"] == 3
        assert self.ids(resp) == [str(c.id), str(b.id), str(a.id)]
        row = resp.json()["results"][0]
        assert row["ongoing"] is True and row["ended_at"] is None
        assert (
            row["monitor"]["name"] == "Site" and row["monitor"]["target"] == "https://example.com"
        )
        assert 590 <= row["duration_seconds"] <= 660  # ongoing: runs up to now
        assert resp.json()["results"][2]["duration_seconds"] == 300

    def test_filter_by_status(self, auth_api, three):
        _, _, a, b, c = three
        assert self.ids(auth_api.get(URL, {"status": "ongoing"})) == [str(c.id)]
        assert set(self.ids(auth_api.get(URL, {"status": "resolved"}))) == {str(a.id), str(b.id)}
        assert auth_api.get(URL, {"status": "weird"}).status_code == 400

    def test_filter_by_monitor(self, auth_api, three):
        site, api, a, b, c = three
        assert self.ids(auth_api.get(URL, {"monitor_id": str(api.id)})) == [str(b.id)]
        assert auth_api.get(URL, {"monitor_id": "nope"}).status_code == 400
        assert auth_api.get(URL, {"monitor_id": str(uuid.uuid4())}).json()["count"] == 0

    def test_filter_by_date_range_inclusive_of_both_days(self, auth_api, user, three):
        site, _, a, b, c = three
        far = incident(site, started_minutes_ago=60 * 24 * 10, duration_minutes=5)
        today = timezone.now().date().isoformat()
        ten_days_ago = (timezone.now() - timedelta(days=10)).date().isoformat()
        assert str(far.id) not in self.ids(auth_api.get(URL, {"start_date": today}))
        assert self.ids(auth_api.get(URL, {"end_date": ten_days_ago})) == [str(far.id)]
        both = auth_api.get(URL, {"start_date": ten_days_ago, "end_date": today})
        assert both.json()["count"] == 4
        assert auth_api.get(URL, {"start_date": "yesterday"}).status_code == 400

    def test_dates_are_read_in_the_users_time_zone(self, auth_api, user):
        # 23:30 UTC on the 1st is already the 2nd in Auckland.
        user.timezone = "Pacific/Auckland"
        user.save()
        site = monitor_for(user)
        obj = incident(site)
        Incident.objects.filter(pk=obj.pk).update(
            started_at=timezone.now().replace(year=2026, month=6, day=1, hour=23, minute=30)
        )
        assert (
            auth_api.get(URL, {"start_date": "2026-06-02", "end_date": "2026-06-02"}).json()[
                "count"
            ]
            == 1
        )
        assert (
            auth_api.get(URL, {"start_date": "2026-06-01", "end_date": "2026-06-01"}).json()[
                "count"
            ]
            == 0
        )

    def test_sorting_by_start_and_duration(self, auth_api, three):
        _, _, a, b, c = three
        assert self.ids(auth_api.get(URL, {"sort": "started_at"})) == [
            str(a.id),
            str(b.id),
            str(c.id),
        ]
        # c is ongoing (~10 min and counting), a took 5 min, b took 90.
        assert self.ids(auth_api.get(URL, {"sort": "-duration"})) == [
            str(b.id),
            str(c.id),
            str(a.id),
        ]
        assert self.ids(auth_api.get(URL, {"sort": "duration"})) == [
            str(a.id),
            str(c.id),
            str(b.id),
        ]
        assert self.ids(auth_api.get(URL, {"sort": "junk"})) == [str(c.id), str(b.id), str(a.id)]

    def test_exclusion_filter_and_comment_counts(self, auth_api, user, three):
        site, _, a, b, c = three
        Incident.objects.filter(pk=a.pk).update(excluded_from_reports=True)
        IncidentComment.objects.create(incident=b, author=user, body="one")
        IncidentComment.objects.create(incident=b, author=user, body="two")
        assert self.ids(auth_api.get(URL, {"excluded": "true"})) == [str(a.id)]
        assert str(a.id) not in self.ids(auth_api.get(URL, {"excluded": "false"}))
        row = next(r for r in auth_api.get(URL).json()["results"] if r["id"] == str(b.id))
        assert row["comment_count"] == 2

    def test_pagination(self, auth_api, user):
        site = monitor_for(user)
        for i in range(30):
            incident(site, started_minutes_ago=1000 - i, duration_minutes=1)
        first = auth_api.get(URL).json()
        assert len(first["results"]) == 25 and first["next"]
        assert len(auth_api.get(URL, {"page": 2}).json()["results"]) == 5


class TestDetail:
    def test_has_the_timeline_comments_and_the_opening_check(self, auth_api, user):
        obj = incident(monitor_for(user))
        IncidentComment.objects.create(incident=obj, author=user, body="Rolled back the deploy.")

        body = auth_api.get(f"{URL}{obj.id}/").json()

        assert [e["kind"] for e in body["events"]] == ["opened", "resolved"]
        assert body["comments"][0]["body"] == "Rolled back the deploy."
        assert (
            body["comments"][0]["is_mine"] is True
            and body["comments"][0]["author_email"] == user.email
        )
        assert body["comments"][0]["visible_on_status_page"] is False
        check = body["opening_check"]
        assert (check["status_detail"], check["status_code"], check["ttfb_ms"]) == (
            "status_mismatch",
            503,
            80,
        )
        assert body["monitor"]["name"] == "Site" and body["root_cause_stage"] == ""

    def test_unknown_ids_are_404s(self, auth_api):
        from django.urls import Resolver404, resolve

        assert auth_api.get(f"{URL}{uuid.uuid4()}/").status_code == 404
        # A malformed id never reaches the view (a clean 404 from the router, not a 500).
        with pytest.raises(Resolver404):
            resolve("/api/v1/incidents/not-a-uuid/")


class TestExcludeFromReports:
    def test_toggling_records_a_timeline_event_and_an_audit_entry(self, auth_api, user):
        obj = incident(monitor_for(user))
        resp = auth_api.patch(f"{URL}{obj.id}/", {"excluded_from_reports": True}, format="json")
        assert resp.status_code == 200 and resp.json()["excluded_from_reports"] is True
        assert resp.json()["events"][-1]["kind"] == "excluded"
        assert AuditLog.objects.filter(action="incident_excluded", user=user).exists()

        resp = auth_api.patch(f"{URL}{obj.id}/", {"excluded_from_reports": False}, format="json")
        assert resp.json()["events"][-1]["kind"] == "included"

    def test_setting_the_same_value_again_is_a_no_op(self, auth_api, user):
        obj = incident(monitor_for(user))
        auth_api.patch(f"{URL}{obj.id}/", {"excluded_from_reports": False}, format="json")
        assert obj.events.count() == 2  # opened, resolved; nothing new

    def test_nothing_else_about_an_incident_can_be_edited(self, auth_api, user):
        obj = incident(monitor_for(user))
        auth_api.patch(
            f"{URL}{obj.id}/",
            {
                "reason": "hacked",
                "ended_at": None,
                "resolution": "x",
                "started_at": "2020-01-01T00:00:00Z",
            },
            format="json",
        )
        obj.refresh_from_db()
        assert (
            obj.reason == "status_mismatch"
            and obj.ended_at is not None
            and obj.resolution == "recovered"
        )


class TestComments:
    def test_add_list_edit_and_delete(self, auth_api, user):
        obj = incident(monitor_for(user))
        base = f"{URL}{obj.id}/comments/"

        created = auth_api.post(base, {"body": "  Checking the logs  "}, format="json")
        assert created.status_code == 201
        body = created.json()
        assert body["body"] == "Checking the logs" and body["visible_on_status_page"] is False
        comment_id = body["id"]

        shown = auth_api.post(
            base, {"body": "Fixed.", "visible_on_status_page": True}, format="json"
        )
        assert shown.json()["visible_on_status_page"] is True
        assert auth_api.get(f"{URL}{obj.id}/").json()["comments"][1]["body"] == "Fixed."

        edited = auth_api.patch(f"{base}{comment_id}/", {"body": "Checked the logs"}, format="json")
        assert edited.status_code == 200 and edited.json()["body"] == "Checked the logs"
        hidden = auth_api.patch(
            f"{base}{shown.json()['id']}/", {"visible_on_status_page": False}, format="json"
        )
        assert hidden.json()["visible_on_status_page"] is False

        assert auth_api.delete(f"{base}{comment_id}/").status_code == 204
        assert obj.comments.count() == 1

    @pytest.mark.parametrize("body", ["", "   ", "x" * 5001])
    def test_invalid_bodies_are_rejected(self, auth_api, user, body):
        obj = incident(monitor_for(user))
        resp = auth_api.post(f"{URL}{obj.id}/comments/", {"body": body}, format="json")
        assert resp.status_code == 400 and "body" in resp.json()["field_errors"]

    def test_you_cannot_touch_someone_elses_comment(self, auth_api, user, make_user):
        obj = incident(monitor_for(user))
        other = make_user("other@example.com")
        theirs = IncidentComment.objects.create(incident=obj, author=other, body="not yours")
        path = f"{URL}{obj.id}/comments/{theirs.id}/"
        assert auth_api.patch(path, {"body": "x"}, format="json").status_code == 404
        assert auth_api.delete(path).status_code == 404
        assert IncidentComment.objects.filter(pk=theirs.pk).exists()

    def test_a_comment_belongs_to_its_own_incident_only(self, auth_api, user):
        site = monitor_for(user)
        one, two = incident(site), incident(site, started_minutes_ago=500)
        comment = IncidentComment.objects.create(incident=one, author=user, body="on one")
        assert auth_api.delete(f"{URL}{two.id}/comments/{comment.id}/").status_code == 404


class TestPostmortem:
    def test_is_prefilled_from_the_incident(self, auth_api, user):
        site = monitor_for(user, "Production API", target="https://api.example.com")
        obj = incident(site, duration_minutes=14)
        IncidentComment.objects.create(incident=obj, author=user, body="Rolled back deploy 41.")

        resp = auth_api.post(f"{URL}{obj.id}/postmortem/")

        assert resp.status_code == 200
        text = resp.json()["markdown"]
        assert text.startswith("# Postmortem: Production API outage, ")
        for expected in (
            "14 minutes",
            "an unexpected HTTP status (503) came back",
            "Outage detected",
            "Recovered: a check succeeded",
            f"Note from {user.email}: Rolled back deploy 41.",
            "## Root cause",
            "## Action items",
            "https://api.example.com",
        ):
            assert expected in text, expected
        assert resp.json()["filename"].startswith("postmortem-production-api-")
        assert resp.json()["filename"].endswith(".md")

    def test_the_timeline_is_in_order_and_in_the_users_zone(self, auth_api, user):
        user.timezone = "Asia/Tokyo"
        user.save()
        obj = incident(monitor_for(user))
        text = auth_api.post(f"{URL}{obj.id}/postmortem/").json()["markdown"]
        assert "## Timeline (Asia/Tokyo)" in text
        assert text.index("Outage detected") < text.index("Recovered")

    def test_an_ongoing_incident_has_to_be_resolved_first(self, auth_api, user):
        obj = incident(monitor_for(user), duration_minutes=None)
        resp = auth_api.post(f"{URL}{obj.id}/postmortem/")
        assert resp.status_code == 400 and "resolved" in resp.json()["detail"]


class TestExport:
    def rows(self, resp):
        return list(csv.DictReader(io.StringIO(resp.content.decode())))

    def test_exports_every_matching_incident_as_csv(self, auth_api, user):
        site = monitor_for(user)
        incident(site, duration_minutes=10)
        incident(site, started_minutes_ago=5, duration_minutes=None)
        resp = auth_api.get(f"{URL}export/")
        assert resp.status_code == 200 and resp["Content-Type"].startswith("text/csv")
        assert resp["Content-Disposition"].startswith('attachment; filename="incidents-')
        rows = self.rows(resp)
        assert len(rows) == 2
        assert {r["status"] for r in rows} == {"ongoing", "resolved"}
        resolved = next(r for r in rows if r["status"] == "resolved")
        assert resolved["duration_seconds"] == "600" and resolved["reason"] == "status_mismatch"
        assert resolved["http_status"] == "503" and resolved["monitor"] == "Site"

    def test_honours_the_same_filters_as_the_list(self, auth_api, user):
        site = monitor_for(user)
        incident(site, duration_minutes=10)
        incident(site, started_minutes_ago=5, duration_minutes=None)
        assert len(self.rows(auth_api.get(f"{URL}export/", {"status": "ongoing"}))) == 1

    def test_formulas_in_names_cannot_run_in_a_spreadsheet(self, auth_api, user):
        evil = monitor_for(user, '=HYPERLINK("http://evil.example","click")', target="@cmd")
        incident(evil)
        row = self.rows(auth_api.get(f"{URL}export/"))[0]
        assert row["monitor"].startswith("'=")
        assert row["target"].startswith("'@")


class TestSchema:
    def test_the_incident_endpoints_are_documented(self, client):
        schema = client.get("/api/v1/schema/", HTTP_ACCEPT="application/json").json()
        for path in (
            "/api/v1/incidents/",
            "/api/v1/incidents/{id}/",
            "/api/v1/incidents/export/",
            "/api/v1/incidents/{id}/postmortem/",
            "/api/v1/incidents/{id}/comments/",
        ):
            assert path in schema["paths"], path


class TestCors:
    def test_the_export_filename_is_readable_by_the_frontend(self, api, user):
        """A cross-origin page can only read headers the API exposes; without this the
        browser can't see the suggested CSV filename."""
        resp = api.options(
            f"{URL}export/",
            HTTP_ORIGIN="http://localhost:3000",
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="GET",
        )
        assert resp.status_code == 200
        from django.conf import settings

        assert "Content-Disposition" in settings.CORS_EXPOSE_HEADERS
