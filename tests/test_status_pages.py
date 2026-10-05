"""Status pages: building one, what the public can and cannot see, subscribers and their email.

The public page is the only unauthenticated surface that shows a user's data, so the access
rules are tested head on: unpublished and missing pages look the same, a password-protected page
gives away nothing until unlocked, and nothing technical about a monitor can appear on the page.
"""

import smtplib
from datetime import UTC, datetime, timedelta

import pytest
from django.core import mail
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import AuditLog
from analytics.models import CheckRollupDaily
from incidents import services as incident_services
from incidents.models import Incident, IncidentComment
from monitoring.models import Check, MaintenanceWindow, Monitor
from statuspages import mail as page_mail
from statuspages import services
from statuspages.models import (
    StatusPage,
    StatusPageAnnouncement,
    StatusPageComponent,
    StatusPageEmail,
    StatusPageSubscriber,
)

pytestmark = pytest.mark.django_db

PAGES = "/api/v1/status-pages/"
ORIGIN = "http://localhost:3000"


def public(slug, tail=""):
    return f"/api/v1/public/status-pages/{slug}/{tail}"


def monitor_for(user, name="Shop", **kwargs):
    fields = {
        "type": "http",
        "target": "https://secret-internal.example.com/health",
        "interval_seconds": 300,
        "config": {},
        "status": "up",
        "next_check_at": timezone.now(),
    }
    fields.update(kwargs)
    return Monitor.objects.create(user=user, name=name, **fields)


def page_for(user, slug="acme", *, public=True, password=None, **kwargs):
    page = StatusPage(user=user, name="Acme status", slug=slug, is_public=public, **kwargs)
    services.set_password(page, password)
    page.save()
    return page


def show(page, monitor, name=None, group="", order=0):
    return StatusPageComponent.objects.create(
        page=page, monitor=monitor, display_name=name or monitor.name, group=group, order=order
    )


def failing_check(monitor, minutes_ago=30):
    return Check.objects.create(
        monitor=monitor,
        region="primary",
        started_at=timezone.now() - timedelta(minutes=minutes_ago),
        success=False,
        status_detail="status_mismatch",
        status_code=503,
        duration_ms=90,
    )


def make_incident(monitor, started_minutes_ago=60, duration_minutes=10, **kwargs):
    started = timezone.now() - timedelta(minutes=started_minutes_ago)
    ended = None if duration_minutes is None else started + timedelta(minutes=duration_minutes)
    return Incident.objects.create(
        monitor=monitor,
        started_at=started,
        ended_at=ended,
        reason="status_mismatch",
        status_code=503,
        resolution="recovered" if ended else "",
        **kwargs,
    )


def subscriber_for(page, email="reader@example.com", confirmed=True):
    return StatusPageSubscriber.objects.create(
        page=page, email=email, confirmed_at=timezone.now() if confirmed else None
    )


@pytest.fixture
def bob_api(make_user, sign_in):
    bob = make_user("bob@example.com")
    client = APIClient(HTTP_ORIGIN=ORIGIN)
    client.credentials(
        HTTP_AUTHORIZATION=f"Bearer {sign_in(client, bob.email).json()['access_token']}"
    )
    client.user = bob
    return client


@pytest.fixture
def visitor():
    """Someone who isn't signed in."""
    return APIClient(HTTP_ORIGIN=ORIGIN)


# --- owner: the page ---------------------------------------------------------------------


class TestOwnerPage:
    def test_create_starts_as_a_private_draft(self, auth_api):
        r = auth_api.post(PAGES, {"name": "  Acme   status ", "slug": "Acme-Status"}, format="json")
        assert r.status_code == 201
        body = r.json()
        assert body["name"] == "Acme status" and body["slug"] == "acme-status"
        assert body["is_public"] is False and body["has_password"] is False
        assert body["url"].endswith("/s/acme-status")
        assert body["component_count"] == 0 and body["subscriber_count"] == 0
        assert "password" not in body

    def test_one_page_per_user_for_now(self, auth_api):
        assert (
            auth_api.post(PAGES, {"name": "A", "slug": "first"}, format="json").status_code == 201
        )
        r = auth_api.post(PAGES, {"name": "B", "slug": "second"}, format="json")
        assert r.status_code == 400 and "one status page" in r.json()["detail"]

    @pytest.mark.parametrize(
        "slug", ["ab", "-bad", "bad-", "has space", "UP_per", "a/b", "ünï", "x" * 41]
    )
    def test_bad_addresses_are_refused(self, auth_api, slug):
        r = auth_api.post(PAGES, {"name": "A", "slug": slug}, format="json")
        assert r.status_code == 400 and "slug" in r.json()["field_errors"]

    def test_an_address_someone_else_has_is_taken(self, auth_api, bob_api, user):
        page_for(user, "acme")
        r = bob_api.post(PAGES, {"name": "B", "slug": "acme"}, format="json")
        assert r.status_code == 400
        assert r.json()["field_errors"]["slug"] == ["That address is taken."]

    def test_a_blank_name_is_refused(self, auth_api):
        r = auth_api.post(PAGES, {"name": "   ", "slug": "acme"}, format="json")
        assert r.status_code == 400 and "name" in r.json()["field_errors"]

    def test_list_and_retrieve_show_counts(self, auth_api, user):
        page = page_for(user)
        show(page, monitor_for(user))
        subscriber_for(page, "a@example.com")
        subscriber_for(page, "b@example.com", confirmed=False)
        listed = auth_api.get(PAGES).json()
        assert len(listed) == 1
        assert listed[0]["component_count"] == 1 and listed[0]["subscriber_count"] == 1
        assert auth_api.get(f"{PAGES}{page.id}/").json()["slug"] == "acme"

    def test_edit_publish_and_rename_the_address(self, auth_api, user):
        page = page_for(user, public=False)
        r = auth_api.patch(
            f"{PAGES}{page.id}/", {"is_public": True, "slug": "new-name"}, format="json"
        )
        assert r.status_code == 200 and r.json()["is_public"] is True
        page.refresh_from_db()
        assert page.slug == "new-name"

    def test_delete_removes_everything_on_it(self, auth_api, user):
        page = page_for(user)
        show(page, monitor_for(user))
        subscriber_for(page)
        assert auth_api.delete(f"{PAGES}{page.id}/").status_code == 204
        assert not StatusPage.objects.exists() and not StatusPageSubscriber.objects.exists()
        assert Monitor.objects.count() == 1  # the monitor itself is untouched
        assert AuditLog.objects.filter(action="status_page_deleted").exists()

    def test_signed_out_callers_are_refused(self, visitor, user):
        page = page_for(user)
        assert visitor.get(PAGES).status_code == 401
        assert visitor.get(f"{PAGES}{page.id}/").status_code == 401
        assert visitor.post(PAGES, {"name": "A", "slug": "abc"}, format="json").status_code == 401


class TestBranding:
    def test_branding_is_saved_and_merged(self, auth_api, user):
        page = page_for(user)
        r = auth_api.patch(
            f"{PAGES}{page.id}/",
            {"branding": {"accent": "#2563EB", "description": "  Our services "}},
            format="json",
        )
        assert r.status_code == 200
        assert r.json()["branding"]["accent"] == "#2563eb"
        r = auth_api.patch(
            f"{PAGES}{page.id}/",
            {"branding": {"logo_url": "https://example.com/logo.png"}},
            format="json",
        )
        branding = r.json()["branding"]
        assert branding["accent"] == "#2563eb" and branding["description"] == "Our services"
        assert branding["logo_url"] == "https://example.com/logo.png"

    @pytest.mark.parametrize(
        "branding",
        [
            {"accent": "red"},
            {"accent": "#12"},
            {"accent": "url(javascript:alert(1))"},
            {"logo_url": "http://example.com/logo.png"},
            {"logo_url": "javascript:alert(1)"},
            {"support_url": "ftp://example.com"},
            {"description": "x" * 281},
        ],
    )
    def test_nothing_that_could_carry_script_or_style_is_accepted(self, auth_api, user, branding):
        page = page_for(user)
        r = auth_api.patch(f"{PAGES}{page.id}/", {"branding": branding}, format="json")
        assert r.status_code == 400

    def test_unknown_branding_keys_are_not_stored(self, auth_api, user):
        page = page_for(user)
        auth_api.patch(
            f"{PAGES}{page.id}/", {"branding": {"css": "body{display:none}"}}, format="json"
        )
        page.refresh_from_db()
        assert "css" not in page.branding


class TestPassword:
    def test_set_and_clear_a_password(self, auth_api, user):
        page = page_for(user)
        url = f"{PAGES}{page.id}/"
        r = auth_api.patch(url, {"password": "correct horse"}, format="json")
        assert r.json()["has_password"] is True and "password" not in r.json()
        page.refresh_from_db()
        assert page.password_hash and "correct horse" not in page.password_hash
        assert services.password_matches(page, "correct horse")

        r = auth_api.patch(url, {"password": None}, format="json")
        assert r.json()["has_password"] is False
        page.refresh_from_db()
        assert page.password_hash == ""

    def test_changes_are_audited_without_the_password(self, auth_api, user):
        page = page_for(user)
        url = f"{PAGES}{page.id}/"
        auth_api.patch(url, {"password": "correct horse"}, format="json")
        auth_api.patch(url, {"password": None}, format="json")
        actions = list(
            AuditLog.objects.filter(action__startswith="status_page")
            .order_by("created_at")
            .values_list("action", flat=True)
        )
        assert actions == ["status_page_password_set", "status_page_password_cleared"]
        assert "correct horse" not in str(list(AuditLog.objects.values("metadata")))

    def test_editing_something_else_is_not_a_password_change(self, auth_api, user):
        page = page_for(user, password="correct horse")
        auth_api.patch(f"{PAGES}{page.id}/", {"name": "Renamed"}, format="json")
        assert not AuditLog.objects.filter(action__startswith="status_page_password").exists()
        page.refresh_from_db()
        assert page.has_password

    @pytest.mark.parametrize("password", ["", "short", "x" * 129])
    def test_weak_or_odd_passwords_are_refused(self, auth_api, user, password):
        page = page_for(user)
        r = auth_api.patch(f"{PAGES}{page.id}/", {"password": password}, format="json")
        assert r.status_code == 400


class TestOwnership:
    """User B can reach nothing of user A's, by any route."""

    def test_other_users_pages_do_not_exist_for_you(self, bob_api, user):
        page = page_for(user)
        monitor = monitor_for(user)
        component = show(page, monitor)
        announcement = StatusPageAnnouncement.objects.create(
            page=page, kind="incident", title="x", body=""
        )
        subscriber = subscriber_for(page)
        base = f"{PAGES}{page.id}/"
        assert bob_api.get(PAGES).json() == []
        for method, url in [
            ("get", base),
            ("patch", base),
            ("delete", base),
            ("get", f"{base}preview/"),
            ("get", f"{base}components/"),
            ("post", f"{base}components/"),
            ("patch", f"{base}components/{component.id}/"),
            ("delete", f"{base}components/{component.id}/"),
            ("post", f"{base}components/reorder/"),
            ("get", f"{base}announcements/"),
            ("post", f"{base}announcements/"),
            ("patch", f"{base}announcements/{announcement.id}/"),
            ("delete", f"{base}announcements/{announcement.id}/"),
            ("post", f"{base}announcements/{announcement.id}/resolve/"),
            ("get", f"{base}subscribers/"),
            ("delete", f"{base}subscribers/{subscriber.id}/"),
        ]:
            r = getattr(bob_api, method)(url, {}, format="json")
            assert r.status_code == 404, (method, url)
        assert StatusPage.objects.filter(pk=page.pk).exists()
        assert StatusPageSubscriber.objects.filter(pk=subscriber.pk).exists()

    def test_you_cannot_show_someone_elses_monitor(self, bob_api, user):
        theirs = monitor_for(user)
        bobs = page_for(bob_api.user, "bobs")
        r = bob_api.post(
            f"{PAGES}{bobs.id}/components/", {"monitor_id": str(theirs.id)}, format="json"
        )
        assert r.status_code == 400 and "monitor_id" in r.json()["field_errors"]
        assert not bobs.components.exists()


# --- owner: components -------------------------------------------------------------------


class TestComponents:
    def test_add_uses_the_monitors_name_unless_you_choose_one(self, auth_api, user):
        page = page_for(user)
        a, b = monitor_for(user, "Website"), monitor_for(user, "Billing API")
        url = f"{PAGES}{page.id}/components/"
        first = auth_api.post(url, {"monitor_id": str(a.id)}, format="json")
        assert first.status_code == 201
        assert first.json()["display_name"] == "Website" and first.json()["order"] == 0
        second = auth_api.post(
            url,
            {"monitor_id": str(b.id), "display_name": "  Payments ", "group": " Core  services"},
            format="json",
        )
        body = second.json()
        assert body["display_name"] == "Payments" and body["group"] == "Core services"
        assert body["order"] == 1
        assert body["monitor"] == {
            "id": str(b.id),
            "name": "Billing API",
            "type": "http",
            "status": "up",
        }
        assert [c["display_name"] for c in auth_api.get(url).json()] == ["Website", "Payments"]

    def test_a_monitor_can_only_be_on_a_page_once(self, auth_api, user):
        page = page_for(user)
        m = monitor_for(user)
        url = f"{PAGES}{page.id}/components/"
        assert auth_api.post(url, {"monitor_id": str(m.id)}, format="json").status_code == 201
        r = auth_api.post(url, {"monitor_id": str(m.id)}, format="json")
        assert (
            r.status_code == 400
            and "already on this page" in r.json()["field_errors"]["monitor_id"][0]
        )

    def test_a_page_has_a_ceiling(self, auth_api, user, monkeypatch):
        monkeypatch.setattr(services, "MAX_COMPONENTS", 2)
        page = page_for(user)
        url = f"{PAGES}{page.id}/components/"
        for _ in range(2):
            m = monitor_for(user)
            assert auth_api.post(url, {"monitor_id": str(m.id)}, format="json").status_code == 201
        r = auth_api.post(url, {"monitor_id": str(monitor_for(user).id)}, format="json")
        assert r.status_code == 400

    def test_rename_and_regroup_but_not_re_point(self, auth_api, user):
        page = page_for(user)
        a, b = monitor_for(user, "A"), monitor_for(user, "B")
        component = show(page, a)
        url = f"{PAGES}{page.id}/components/{component.id}/"
        r = auth_api.patch(
            url,
            {"display_name": "Website", "group": "Public", "monitor_id": str(b.id)},
            format="json",
        )
        assert r.status_code == 200
        component.refresh_from_db()
        assert (component.display_name, component.group, component.monitor_id) == (
            "Website",
            "Public",
            a.id,
        )
        assert auth_api.patch(url, {"display_name": " "}, format="json").status_code == 400

    def test_reorder(self, auth_api, user):
        page = page_for(user)
        parts = [show(page, monitor_for(user, n), order=i) for i, n in enumerate("ABC")]
        url = f"{PAGES}{page.id}/components/reorder/"
        ids = [str(parts[2].id), str(parts[0].id), str(parts[1].id)]
        r = auth_api.post(url, {"ids": ids}, format="json")
        assert r.status_code == 200 and [c["id"] for c in r.json()] == ids
        assert [c.display_name for c in page.components.all()] == ["C", "A", "B"]

    @pytest.mark.parametrize("which", ["missing", "duplicate", "stranger"])
    def test_reorder_needs_every_component_once(self, auth_api, user, which):
        page = page_for(user)
        a, b = (show(page, monitor_for(user, n), order=i) for i, n in enumerate("AB"))
        ids = {
            "missing": [str(a.id)],
            "duplicate": [str(a.id), str(a.id), str(b.id)],
            "stranger": [str(a.id), str(b.id), "00000000-0000-4000-8000-000000000000"],
        }[which]
        r = auth_api.post(f"{PAGES}{page.id}/components/reorder/", {"ids": ids}, format="json")
        assert r.status_code == 400

    def test_remove_and_cascade_from_the_monitor(self, auth_api, user):
        page = page_for(user)
        a, b = monitor_for(user, "A"), monitor_for(user, "B")
        ca, _ = show(page, a), show(page, b, order=1)
        assert auth_api.delete(f"{PAGES}{page.id}/components/{ca.id}/").status_code == 204
        assert page.components.count() == 1
        b.delete()  # deleting the monitor takes it off the page too
        assert page.components.count() == 0


# --- owner: announcements ----------------------------------------------------------------


class TestAnnouncements:
    def url(self, page, tail=""):
        return f"{PAGES}{page.id}/announcements/{tail}"

    def test_post_an_incident_and_resolve_it(self, auth_api, user):
        page = page_for(user)
        r = auth_api.post(
            self.url(page),
            {
                "kind": "incident",
                "title": " Slow checkout ",
                "body": "Looking into it.",
                "impact": "minor",
            },
            format="json",
        )
        assert r.status_code == 201
        body = r.json()
        assert body["title"] == "Slow checkout" and body["state"] == "active"
        assert body["starts_at"] is None and body["resolved_at"] is None
        resolved = auth_api.post(self.url(page, f"{body['id']}/resolve/"))
        assert resolved.json()["state"] == "resolved" and resolved.json()["resolved_at"]
        again = auth_api.post(self.url(page, f"{body['id']}/resolve/"))
        assert again.json()["resolved_at"] == resolved.json()["resolved_at"]  # idempotent

    def test_maintenance_needs_a_window(self, auth_api, user):
        page = page_for(user)
        start = timezone.now() + timedelta(days=1)
        ok = auth_api.post(
            self.url(page),
            {
                "kind": "maintenance",
                "title": "Database upgrade",
                "starts_at": start.isoformat(),
                "ends_at": (start + timedelta(hours=2)).isoformat(),
            },
            format="json",
        )
        assert ok.status_code == 201 and ok.json()["state"] == "upcoming"
        for payload in [
            {"kind": "maintenance", "title": "x"},
            {
                "kind": "maintenance",
                "title": "x",
                "starts_at": start.isoformat(),
                "ends_at": (start - timedelta(hours=1)).isoformat(),
            },
            {
                "kind": "maintenance",
                "title": "x",
                "starts_at": start.isoformat(),
                "ends_at": (start + timedelta(days=31)).isoformat(),
            },
        ]:
            assert auth_api.post(self.url(page), payload, format="json").status_code == 400

    def test_an_incident_ignores_a_window_it_was_given(self, auth_api, user):
        page = page_for(user)
        now = timezone.now()
        r = auth_api.post(
            self.url(page),
            {
                "kind": "incident",
                "title": "x",
                "starts_at": now.isoformat(),
                "ends_at": now.isoformat(),
            },
            format="json",
        )
        assert r.json()["starts_at"] is None and r.json()["ends_at"] is None

    def test_edit_but_not_change_the_kind(self, auth_api, user):
        page = page_for(user)
        a = StatusPageAnnouncement.objects.create(page=page, kind="incident", title="Old", body="")
        ok = auth_api.patch(
            self.url(page, f"{a.id}/"), {"title": "New", "body": "More"}, format="json"
        )
        assert ok.status_code == 200 and ok.json()["title"] == "New"
        bad = auth_api.patch(self.url(page, f"{a.id}/"), {"kind": "maintenance"}, format="json")
        assert bad.status_code == 400

    def test_blank_title_and_unknown_kind_are_refused(self, auth_api, user):
        page = page_for(user)
        assert (
            auth_api.post(
                self.url(page), {"kind": "incident", "title": " "}, format="json"
            ).status_code
            == 400
        )
        assert (
            auth_api.post(
                self.url(page), {"kind": "party", "title": "x"}, format="json"
            ).status_code
            == 400
        )

    def test_delete(self, auth_api, user):
        page = page_for(user)
        a = StatusPageAnnouncement.objects.create(page=page, kind="incident", title="x", body="")
        assert auth_api.delete(self.url(page, f"{a.id}/")).status_code == 204
        assert auth_api.get(self.url(page)).json() == []


# --- owner: preview and subscribers ------------------------------------------------------


class TestPreviewAndSubscribers:
    def test_preview_shows_a_draft_and_a_protected_page(self, auth_api, user):
        page = page_for(user, public=False, password="correct horse")
        show(page, monitor_for(user, "Website"))
        r = auth_api.get(f"{PAGES}{page.id}/preview/")
        assert r.status_code == 200
        assert (
            r.json()["components"][0]["name"] == "Website"
            and r.json()["password_required"] is False
        )

    def test_the_subscriber_list_is_paged_and_can_be_pruned(self, auth_api, user):
        page = page_for(user)
        a = subscriber_for(page, "a@example.com")
        subscriber_for(page, "b@example.com", confirmed=False)
        body = auth_api.get(f"{PAGES}{page.id}/subscribers/").json()
        assert body["count"] == 2
        assert {s["email"]: s["confirmed"] for s in body["results"]} == {
            "a@example.com": True,
            "b@example.com": False,
        }
        assert "token" not in str(body)
        assert auth_api.delete(f"{PAGES}{page.id}/subscribers/{a.id}/").status_code == 204
        assert auth_api.get(f"{PAGES}{page.id}/subscribers/").json()["count"] == 1


# --- public: who can see what ------------------------------------------------------------


class TestPublicAccess:
    def test_missing_and_unpublished_pages_look_identical(self, visitor, user):
        page_for(user, "draft", public=False)
        a, b = visitor.get(public("draft")), visitor.get(public("never-existed"))
        assert a.status_code == b.status_code == 404
        assert a.json() == b.json()

    def test_a_published_page_needs_no_sign_in(self, visitor, user):
        page = page_for(user)
        show(page, monitor_for(user, "Website"))
        r = visitor.get(public("acme"))
        assert r.status_code == 200
        assert r["Cache-Control"] == "no-store"
        assert r.json()["name"] == "Acme status"

    def test_unpublishing_takes_effect_immediately(self, auth_api, visitor, user):
        page = page_for(user)
        assert visitor.get(public("acme")).status_code == 200
        auth_api.patch(f"{PAGES}{page.id}/", {"is_public": False}, format="json")
        assert visitor.get(public("acme")).status_code == 404

    def test_nothing_technical_about_a_monitor_reaches_the_page(self, visitor, user):
        page = page_for(user)
        m = monitor_for(user, "Internal billing", deploy_service="billing-svc")
        show(page, m, "Payments")
        incident = make_incident(m, duration_minutes=None)
        Check.objects.create(
            monitor=m,
            region="primary",
            started_at=timezone.now(),
            success=False,
            status_detail="status_mismatch",
            status_code=503,
        )
        text = visitor.get(public("acme")).content.decode()
        for secret in [
            "secret-internal.example.com",
            "Internal billing",
            "billing-svc",
            "status_mismatch",
            str(m.id),
            str(user.id),
            user.email,
        ]:
            assert secret not in text, secret
        assert str(incident.id) in text  # incidents are listed, by id and component name only

    def test_other_peoples_monitors_never_appear(self, visitor, user, make_user):
        page = page_for(user)
        show(page, monitor_for(user, "Mine"))
        monitor_for(make_user("carol@example.com"), "Carols")
        text = visitor.get(public("acme")).content.decode()
        assert "Mine" in text and "Carols" not in text

    def test_the_public_page_reads_a_fixed_number_of_queries(self, visitor, user):
        page = page_for(user)
        first = monitor_for(user, "A")
        show(page, first)
        make_incident(first)
        visitor.get(public("acme"))  # warm up
        with CaptureQueriesContext(connection) as small:
            visitor.get(public("acme"))
        for i in range(12):
            m = monitor_for(user, f"M{i}")
            show(page, m, order=i + 1)
            make_incident(m, started_minutes_ago=30 + i)
        with CaptureQueriesContext(connection) as big:
            visitor.get(public("acme"))
        assert len(big) == len(small)


class TestPasswordGate:
    def test_a_locked_page_says_only_that_it_is_locked(self, visitor, user):
        page = page_for(user, password="correct horse", branding={"accent": "#ff0000"})
        show(page, monitor_for(user, "Website"))
        r = visitor.get(public("acme"))
        assert r.status_code == 200
        assert r.json() == {
            "name": "Acme status",
            "slug": "acme",
            "branding": {
                "accent": "#ff0000",
                "logo_url": "",
                "description": "",
                "support_url": "",
            },
            "password_required": True,
        }
        assert "Website" not in r.content.decode()

    def test_the_right_password_gives_a_token_that_opens_the_page(self, visitor, user):
        page = page_for(user, password="correct horse")
        show(page, monitor_for(user, "Website"))
        r = visitor.post(public("acme", "unlock/"), {"password": "correct horse"}, format="json")
        assert r.status_code == 200 and r.json()["expires_in"] > 0
        opened = visitor.get(public("acme"), HTTP_X_STATUS_PAGE_TOKEN=r.json()["token"])
        assert opened.json()["password_required"] is False
        assert opened.json()["components"][0]["name"] == "Website"

    def test_the_wrong_password_gets_nothing(self, visitor, user):
        page_for(user, password="correct horse")
        r = visitor.post(public("acme", "unlock/"), {"password": "wrong"}, format="json")
        assert r.status_code == 403 and "token" not in r.json()

    @pytest.mark.parametrize("token", ["", "garbage", "a:b:c"])
    def test_a_made_up_token_gets_nothing(self, visitor, user, token):
        page_for(user, password="correct horse")
        r = visitor.get(public("acme"), HTTP_X_STATUS_PAGE_TOKEN=token)
        assert r.json()["password_required"] is True and "components" not in r.json()

    def test_a_token_only_opens_its_own_page(self, visitor, user, make_user):
        page_for(user, "one", password="correct horse")
        other = page_for(make_user("carol@example.com"), "two", password="correct horse")
        token = visitor.post(
            public("one", "unlock/"), {"password": "correct horse"}, format="json"
        ).json()["token"]
        assert visitor.get(public("two"), HTTP_X_STATUS_PAGE_TOKEN=token).json()[
            "password_required"
        ]
        assert other.has_password

    def test_changing_the_password_retires_every_token(self, auth_api, visitor, user):
        page = page_for(user, password="correct horse")
        token = visitor.post(
            public("acme", "unlock/"), {"password": "correct horse"}, format="json"
        ).json()["token"]
        auth_api.patch(f"{PAGES}{page.id}/", {"password": "battery staple"}, format="json")
        assert visitor.get(public("acme"), HTTP_X_STATUS_PAGE_TOKEN=token).json()[
            "password_required"
        ]

    def test_removing_the_password_opens_the_page_to_everyone(self, auth_api, visitor, user):
        page = page_for(user, password="correct horse")
        auth_api.patch(f"{PAGES}{page.id}/", {"password": None}, format="json")
        assert visitor.get(public("acme")).json()["password_required"] is False

    def test_tokens_expire(self, visitor, user, monkeypatch):
        page_for(user, password="correct horse")
        token = visitor.post(
            public("acme", "unlock/"), {"password": "correct horse"}, format="json"
        ).json()["token"]
        monkeypatch.setattr(services, "UNLOCK_MAX_AGE", -1)
        assert visitor.get(public("acme"), HTTP_X_STATUS_PAGE_TOKEN=token).json()[
            "password_required"
        ]

    def test_guessing_is_throttled(self, visitor, user):
        page_for(user, password="correct horse")
        codes = [
            visitor.post(
                public("acme", "unlock/"), {"password": f"guess{i}"}, format="json"
            ).status_code
            for i in range(12)
        ]
        assert codes[:10] == [403] * 10 and 429 in codes[10:]

    def test_subscribing_to_a_locked_page_needs_the_password_first(self, visitor, user):
        page = page_for(user, password="correct horse")
        r = visitor.post(public("acme", "subscribe/"), {"email": "a@example.com"}, format="json")
        assert r.status_code == 403 and not page.subscribers.exists()


# --- public: what the page says ----------------------------------------------------------


class TestPageContent:
    def test_component_statuses_and_the_overall_banner(self, visitor, user):
        page = page_for(user)
        show(page, monitor_for(user, "Up", status="up"), order=0)
        show(page, monitor_for(user, "Down", status="down"), order=1)
        show(page, monitor_for(user, "Paused", status="paused", next_check_at=None), order=2)
        show(page, monitor_for(user, "New", status="pending"), order=3)
        body = visitor.get(public("acme")).json()
        assert {c["name"]: c["status"] for c in body["components"]} == {
            "Up": "operational",
            "Down": "major_outage",
            "Paused": "paused",
            "New": "unknown",
        }
        assert body["overall"]["status"] == "partial_outage"

    @pytest.mark.parametrize(
        ("statuses", "expected"),
        [
            ([], "unknown"),
            (["operational", "operational"], "operational"),
            (["operational", "major_outage"], "partial_outage"),
            (["major_outage", "major_outage"], "major_outage"),
            (["operational", "maintenance"], "maintenance"),
            (["major_outage", "maintenance"], "partial_outage"),
            (["paused", "unknown"], "unknown"),
            (["paused", "operational"], "operational"),
        ],
    )
    def test_overall_status(self, statuses, expected):
        assert services.overall_status(statuses) == expected

    def test_a_maintenance_window_shows_as_maintenance(self, visitor, user):
        page = page_for(user)
        m = monitor_for(user, "Website")
        show(page, m)
        window = MaintenanceWindow.objects.create(
            user=user,
            name="Upgrade",
            starts_at=timezone.now() - timedelta(minutes=5),
            ends_at=timezone.now() + timedelta(hours=1),
        )
        window.monitors.add(m)
        body = visitor.get(public("acme")).json()
        assert body["components"][0]["status"] == "maintenance"
        assert body["overall"]["status"] == "maintenance"

    def test_uptime_bars_come_from_the_daily_rollups(self, visitor, user):
        page = page_for(user)
        m = monitor_for(user, "Website")
        show(page, m)
        today = datetime.now(UTC).date()
        CheckRollupDaily.objects.create(monitor=m, day=today, up=99, down=1)
        CheckRollupDaily.objects.create(monitor=m, day=today - timedelta(days=1), up=100, down=0)
        CheckRollupDaily.objects.create(  # a bad day, but all of it inside an excluded incident
            monitor=m, day=today - timedelta(days=2), up=50, down=50, down_excluded=50
        )
        CheckRollupDaily.objects.create(  # too old to show
            monitor=m, day=today - timedelta(days=95), up=0, down=10
        )
        body = visitor.get(public("acme")).json()
        component = body["components"][0]
        days = component["days"]
        assert len(days) == body["uptime_days"] == 90
        assert days[-1] == {"date": today.isoformat(), "uptime_percent": 99.0}
        assert days[-2]["uptime_percent"] == 100.0
        assert days[-3]["uptime_percent"] == 100.0  # the excluded failures don't count
        assert days[0]["uptime_percent"] is None  # no data is not a bad day
        assert [d["date"] for d in days] == sorted(d["date"] for d in days)
        assert component["uptime_percent"] == round(100 * 249 / 250, 3)

    def test_no_data_at_all_is_not_100_percent(self, visitor, user):
        page = page_for(user)
        show(page, monitor_for(user, "Website"))
        assert visitor.get(public("acme")).json()["components"][0]["uptime_percent"] is None

    def test_ordering_and_groups_are_kept(self, visitor, user):
        page = page_for(user)
        show(page, monitor_for(user, "B"), "Beta", group="Core", order=1)
        show(page, monitor_for(user, "A"), "Alpha", group="Edge", order=0)
        names = [(c["name"], c["group"]) for c in visitor.get(public("acme")).json()["components"]]
        assert names == [("Alpha", "Edge"), ("Beta", "Core")]


class TestIncidentsOnThePage:
    def test_ongoing_and_recent_incidents_are_listed_by_component_name(self, visitor, user):
        page = page_for(user)
        m = monitor_for(user, "Internal name", status="down")
        show(page, m, "Payments")
        ongoing = make_incident(m, started_minutes_ago=20, duration_minutes=None)
        older = make_incident(m, started_minutes_ago=60 * 24 * 3, duration_minutes=30)
        make_incident(m, started_minutes_ago=60 * 24 * 40, duration_minutes=30)  # long ago
        body = visitor.get(public("acme")).json()
        listed = body["incidents"]
        assert [i["id"] for i in listed] == [str(ongoing.id), str(older.id)]
        assert listed[0]["component"] == "Payments" and listed[0]["ongoing"] is True
        assert listed[1]["ongoing"] is False and listed[1]["ended_at"]

    def test_incidents_excluded_from_reports_are_hidden(self, visitor, user):
        page = page_for(user)
        m = monitor_for(user)
        show(page, m)
        make_incident(m, excluded_from_reports=True)
        assert visitor.get(public("acme")).json()["incidents"] == []

    def test_incidents_of_monitors_not_on_the_page_are_not_listed(self, visitor, user):
        page = page_for(user)
        show(page, monitor_for(user, "On"))
        make_incident(monitor_for(user, "Off"))
        assert visitor.get(public("acme")).json()["incidents"] == []

    def test_a_note_appears_when_toggled_visible_and_vanishes_when_toggled_off(
        self, auth_api, visitor, user
    ):
        """The definition of done: the toggle in the incident view controls the public page."""
        page = page_for(user)
        m = monitor_for(user)
        show(page, m, "Website")
        incident = make_incident(m, duration_minutes=None)
        base = f"/api/v1/incidents/{incident.id}/comments/"

        private = auth_api.post(base, {"body": "Internal: it was the cache"}, format="json")
        public_note = auth_api.post(
            base,
            {"body": "We found the cause and are fixing it.", "visible_on_status_page": True},
            format="json",
        )
        text = visitor.get(public("acme")).content.decode()
        assert "We found the cause and are fixing it." in text
        assert "Internal: it was the cache" not in text
        assert private.status_code == public_note.status_code == 201

        auth_api.patch(
            f"{base}{private.json()['id']}/", {"visible_on_status_page": True}, format="json"
        )
        assert "Internal: it was the cache" in visitor.get(public("acme")).content.decode()

        auth_api.patch(
            f"{base}{public_note.json()['id']}/", {"visible_on_status_page": False}, format="json"
        )
        after = visitor.get(public("acme")).content.decode()
        assert "We found the cause" not in after and "Internal: it was the cache" in after

        auth_api.delete(f"{base}{private.json()['id']}/")
        assert visitor.get(public("acme")).json()["incidents"][0]["updates"] == []

    def test_notes_never_say_who_wrote_them(self, visitor, user):
        page = page_for(user)
        m = monitor_for(user)
        show(page, m)
        incident = make_incident(m)
        IncidentComment.objects.create(
            incident=incident, author=user, body="Fixed.", visible_on_status_page=True
        )
        update = visitor.get(public("acme")).json()["incidents"][0]["updates"][0]
        assert set(update) == {"id", "body", "at"}
        assert user.email not in str(update)


class TestAnnouncementsOnThePage:
    def test_active_upcoming_and_recent_past(self, visitor, user):
        page = page_for(user)
        now = timezone.now()

        def post(**kw):
            kw.setdefault("body", "")
            return StatusPageAnnouncement.objects.create(page=page, **kw)

        post(kind="incident", title="Live incident")
        post(
            kind="maintenance",
            title="Next week",
            starts_at=now + timedelta(days=7),
            ends_at=now + timedelta(days=7, hours=2),
        )
        post(
            kind="maintenance",
            title="Right now",
            starts_at=now - timedelta(minutes=10),
            ends_at=now + timedelta(hours=1),
        )
        done = post(kind="incident", title="Yesterday", resolved_at=now - timedelta(days=1))
        post(kind="incident", title="Ancient", resolved_at=now - timedelta(days=30))
        StatusPageAnnouncement.objects.filter(title="Ancient").update(
            created_at=now - timedelta(days=31)
        )
        body = visitor.get(public("acme")).json()["announcements"]
        assert {a["title"]: a["state"] for a in body["active"]} == {
            "Live incident": "active",
            "Next week": "upcoming",
            "Right now": "in_progress",
        }
        assert [a["title"] for a in body["past"]] == ["Yesterday"]
        assert done.resolved_at

    def test_a_maintenance_that_has_ended_moves_to_the_past(self, visitor, user):
        page = page_for(user)
        now = timezone.now()
        StatusPageAnnouncement.objects.create(
            page=page,
            kind="maintenance",
            title="Done",
            body="",
            starts_at=now - timedelta(hours=3),
            ends_at=now - timedelta(hours=1),
        )
        body = visitor.get(public("acme")).json()["announcements"]
        assert body["active"] == [] and body["past"][0]["state"] == "completed"

    def test_deleted_announcements_disappear(self, auth_api, visitor, user):
        page = page_for(user)
        a = StatusPageAnnouncement.objects.create(page=page, kind="incident", title="Oops", body="")
        auth_api.delete(f"{PAGES}{page.id}/announcements/{a.id}/")
        assert visitor.get(public("acme")).json()["announcements"]["active"] == []

    def test_announcement_text_is_sent_as_plain_text(self, visitor, user):
        page = page_for(user)
        StatusPageAnnouncement.objects.create(
            page=page, kind="incident", title="<script>alert(1)</script>", body="<b>hi</b>"
        )
        a = visitor.get(public("acme")).json()["announcements"]["active"][0]
        assert a["title"] == "<script>alert(1)</script>" and a["body"] == "<b>hi</b>"
        # (The frontend renders it as text; the API never turns it into markup.)


# --- subscribers: double opt-in ----------------------------------------------------------


def sent_mail():
    return page_mail.run_status_page_mail_once()


class TestDoubleOptIn:
    def subscribe(self, visitor, email="reader@example.com", slug="acme"):
        return visitor.post(public(slug, "subscribe/"), {"email": email}, format="json")

    def test_subscribing_sends_only_a_confirmation_and_nothing_else_until_confirmed(
        self, visitor, user
    ):
        page = page_for(user)
        r = self.subscribe(visitor)
        assert r.status_code == 202
        subscriber = page.subscribers.get()
        assert subscriber.confirmed_at is None
        assert sent_mail().sent == 1
        message = mail.outbox[0]
        assert message.to == ["reader@example.com"]
        assert f"/s/acme/confirm/{subscriber.confirm_token}" in message.body
        assert "List-Unsubscribe" not in message.extra_headers

        # An announcement posted before confirming reaches nobody.
        announcement = StatusPageAnnouncement.objects.create(
            page=page, kind="incident", title="Down", body=""
        )
        services.announce(announcement)
        assert sent_mail().claimed == 0 and len(mail.outbox) == 1

    def test_the_link_confirms_once(self, visitor, user):
        page = page_for(user)
        self.subscribe(visitor)
        subscriber = page.subscribers.get()
        token = subscriber.confirm_token
        r = visitor.post(public("acme", f"confirm/{token}/"))
        assert r.status_code == 200
        assert r.json() == {
            "page_name": "Acme status",
            "page_slug": "acme",
            "email": "r*****@example.com",
        }
        subscriber.refresh_from_db()
        assert subscriber.confirmed and subscriber.confirm_token == ""
        assert visitor.post(public("acme", f"confirm/{token}/")).status_code == 404

    @pytest.mark.parametrize("token", ["nope", "x" * 40])
    def test_a_made_up_confirmation_link_does_nothing(self, visitor, user, token):
        page = page_for(user)
        self.subscribe(visitor)
        assert visitor.post(public("acme", f"confirm/{token}/")).status_code == 404
        assert not page.subscribers.get().confirmed

    def test_a_link_only_works_on_its_own_page(self, visitor, user, make_user):
        page = page_for(user)
        page_for(make_user("carol@example.com"), "other")
        self.subscribe(visitor)
        token = page.subscribers.get().confirm_token
        assert visitor.post(public("other", f"confirm/{token}/")).status_code == 404

    def test_an_old_unconfirmed_request_expires(self, visitor, user):
        page = page_for(user)
        self.subscribe(visitor)
        subscriber = page.subscribers.get()
        StatusPageSubscriber.objects.filter(pk=subscriber.pk).update(
            created_at=timezone.now() - services.UNCONFIRMED_TTL - timedelta(hours=1)
        )
        assert (
            visitor.post(public("acme", f"confirm/{subscriber.confirm_token}/")).status_code == 404
        )
        sent_mail()  # the housekeeping step tidies the stale request away
        assert not page.subscribers.exists()

    def test_the_answer_is_the_same_whether_or_not_the_address_is_known(self, visitor, user):
        page = page_for(user)
        subscriber_for(page, "known@example.com")
        a = self.subscribe(visitor, "known@example.com")
        b = self.subscribe(visitor, "new@example.com")
        assert (a.status_code, a.json()) == (b.status_code, b.json())
        assert sent_mail().sent == 1  # only the new address was emailed
        assert mail.outbox[0].to == ["new@example.com"]

    def test_addresses_are_not_case_sensitive(self, visitor, user):
        page = page_for(user)
        self.subscribe(visitor, "Reader@Example.com")
        self.subscribe(visitor, "reader@example.COM")
        assert (
            page.subscribers.count() == 1 and page.subscribers.get().email == "reader@example.com"
        )

    def test_one_confirmation_email_per_address_per_cooldown(self, visitor, user):
        page = page_for(user)
        self.subscribe(visitor)
        self.subscribe(visitor)
        self.subscribe(visitor)
        assert sent_mail().sent == 1
        StatusPageSubscriber.objects.update(
            last_confirmation_at=timezone.now() - services.CONFIRM_COOLDOWN - timedelta(minutes=1)
        )
        self.subscribe(visitor)
        assert sent_mail().sent == 1 and len(mail.outbox) == 2
        assert page.subscribers.count() == 1

    def test_the_form_checks_the_address(self, visitor, user):
        page_for(user)
        for payload in [{}, {"email": ""}, {"email": "nope"}, {"email": "a@b"}]:
            assert (
                visitor.post(public("acme", "subscribe/"), payload, format="json").status_code
                == 400
            )
        assert not StatusPageSubscriber.objects.exists()

    def test_pages_have_a_ceiling_on_subscribers_and_on_unconfirmed_ones(
        self, visitor, user, monkeypatch
    ):
        page = page_for(user)
        monkeypatch.setattr(services, "MAX_UNCONFIRMED", 2)
        for i in range(4):
            self.subscribe(visitor, f"p{i}@example.com")
        assert page.subscribers.count() == 2
        monkeypatch.setattr(services, "MAX_SUBSCRIBERS", 2)
        monkeypatch.setattr(services, "MAX_UNCONFIRMED", 200)
        self.subscribe(visitor, "more@example.com")
        assert page.subscribers.count() == 2

    def test_you_cannot_subscribe_to_a_draft_or_a_missing_page(self, visitor, user):
        page_for(user, "draft", public=False)
        assert self.subscribe(visitor, slug="draft").status_code == 404
        assert self.subscribe(visitor, slug="missing").status_code == 404
        assert not StatusPageSubscriber.objects.exists()

    def test_subscribing_is_throttled_per_address_of_the_caller(self, visitor, user):
        page_for(user)
        codes = [self.subscribe(visitor, f"p{i}@example.com").status_code for i in range(12)]
        assert codes[:10] == [202] * 10 and 429 in codes[10:]

    def test_a_protected_page_accepts_subscribers_who_have_unlocked_it(self, visitor, user):
        page = page_for(user, password="correct horse")
        token = visitor.post(
            public("acme", "unlock/"), {"password": "correct horse"}, format="json"
        ).json()["token"]
        r = visitor.post(
            public("acme", "subscribe/"),
            {"email": "a@example.com"},
            format="json",
            HTTP_X_STATUS_PAGE_TOKEN=token,
        )
        assert r.status_code == 202 and page.subscribers.count() == 1


class TestUnsubscribe:
    def test_looking_at_the_link_changes_nothing_and_posting_removes_you(self, visitor, user):
        page = page_for(user)
        subscriber = subscriber_for(page)
        url = public("acme", f"unsubscribe/{subscriber.unsubscribe_token}/")
        info = visitor.get(url)
        assert info.status_code == 200 and info.json()["email"] == "r*****@example.com"
        assert page.subscribers.count() == 1  # a mail scanner opening the link did nothing
        assert visitor.post(url).status_code == 200
        assert not page.subscribers.exists()
        assert visitor.post(url).status_code == 404

    def test_a_made_up_or_foreign_token_does_nothing(self, visitor, user, make_user):
        page = page_for(user)
        subscriber = subscriber_for(page)
        page_for(make_user("carol@example.com"), "other")
        assert visitor.post(public("acme", "unsubscribe/nope/")).status_code == 404
        wrong = public("other", f"unsubscribe/{subscriber.unsubscribe_token}/")
        assert visitor.post(wrong).status_code == 404
        assert page.subscribers.count() == 1

    def test_it_works_even_after_the_page_is_unpublished(self, visitor, user):
        page = page_for(user, public=False)
        subscriber = subscriber_for(page)
        url = public("acme", f"unsubscribe/{subscriber.unsubscribe_token}/")
        assert visitor.post(url).status_code == 200


# --- subscribers: the emails -------------------------------------------------------------


@pytest.fixture
def watched(user):
    """A published page showing one monitor, with two confirmed subscribers."""
    page = page_for(user)
    monitor = monitor_for(user, "Internal name", status="up")
    show(page, monitor, "Payments")
    one, two = subscriber_for(page, "one@example.com"), subscriber_for(page, "two@example.com")
    return page, monitor, one, two


def fail_monitor(monitor):
    monitor.status = "down"
    monitor.save()
    check = failing_check(monitor)
    return incident_services.open_incident(monitor, check)


def recover_monitor(monitor):
    monitor.status = "up"
    monitor.save()
    check = Check.objects.create(
        monitor=monitor,
        region="primary",
        started_at=timezone.now(),
        success=True,
        status_detail="ok",
        status_code=200,
        duration_ms=50,
    )
    incident_services.reconcile(monitor, "up", check)


class TestComponentEmails:
    def test_an_outage_emails_confirmed_subscribers_and_says_nothing_technical(self, watched):
        page, monitor, *_ = watched
        subscriber_for(page, "pending@example.com", confirmed=False)
        incident = fail_monitor(monitor)
        assert StatusPageEmail.objects.filter(incident=incident).count() == 2
        assert sent_mail().sent == 2
        assert {m.to[0] for m in mail.outbox} == {"one@example.com", "two@example.com"}
        message = mail.outbox[0]
        assert message.subject == "[Acme status] Payments is down"
        assert "Internal name" not in message.body + message.subject
        assert "secret-internal" not in message.body and "status_mismatch" not in message.body
        assert "/s/acme/unsubscribe/" in message.body
        assert message.extra_headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
        assert (
            "/api/v1/public/status-pages/acme/unsubscribe/"
            in message.extra_headers["List-Unsubscribe"]
        )

    def test_recovery_goes_to_those_who_heard_about_the_outage(self, watched):
        page, monitor, one, two = watched
        fail_monitor(monitor)
        sent_mail()
        late = subscriber_for(page, "late@example.com")  # joined during the outage
        mail.outbox.clear()
        recover_monitor(monitor)
        assert sent_mail().sent == 2
        assert {m.to[0] for m in mail.outbox} == {"one@example.com", "two@example.com"}
        assert all(m.subject == "[Acme status] Payments is back up" for m in mail.outbox)
        assert late.emails.count() == 0

    def test_no_all_clear_for_an_alarm_that_never_went_out(self, watched):
        _, monitor, *_ = watched
        fail_monitor(monitor)
        recover_monitor(monitor)  # before the mail pass ran
        assert sent_mail().sent == 0 and mail.outbox == []
        assert set(StatusPageEmail.objects.values_list("status", flat=True)) == {"skipped"}

    def test_a_draft_page_emails_nobody(self, user):
        page = page_for(user, public=False)
        monitor = monitor_for(user)
        show(page, monitor)
        subscriber_for(page)
        fail_monitor(monitor)
        assert not StatusPageEmail.objects.exists()

    def test_unpublishing_before_the_mail_goes_out_stops_it(self, watched):
        page, monitor, *_ = watched
        fail_monitor(monitor)
        page.is_public = False
        page.save()
        assert sent_mail().skipped == 2 and mail.outbox == []

    def test_a_monitor_on_two_pages_tells_each_pages_subscribers_separately(self, user, watched):
        page, monitor, *_ = watched
        other = StatusPage.objects.create(user=user, name="Other", slug="other", is_public=True)
        show(other, monitor, "Pay")
        subscriber_for(other, "three@example.com")
        fail_monitor(monitor)
        sent_mail()
        subjects = {m.to[0]: m.subject for m in mail.outbox}
        assert subjects["three@example.com"] == "[Other] Pay is down"
        assert subjects["one@example.com"] == "[Acme status] Payments is down"

    def test_queuing_twice_never_emails_twice(self, watched):
        _, monitor, *_ = watched
        incident = fail_monitor(monitor)
        services.on_incident_opened(incident)
        services.on_incident_opened(incident)
        assert StatusPageEmail.objects.filter(incident=incident).count() == 2

    def test_someone_who_unsubscribes_mid_outage_hears_no_more(self, watched):
        page, monitor, one, _ = watched
        fail_monitor(monitor)
        sent_mail()
        mail.outbox.clear()
        one.delete()
        recover_monitor(monitor)
        sent_mail()
        assert [m.to[0] for m in mail.outbox] == ["two@example.com"]

    def test_a_failed_page_email_never_costs_the_incident(self, watched, monkeypatch):
        _, monitor, *_ = watched

        def boom(*a, **k):
            raise RuntimeError("queue is broken")

        monkeypatch.setattr(services, "on_incident_opened", boom)
        incident = fail_monitor(monitor)
        assert incident is not None and Incident.objects.filter(pk=incident.pk).exists()

    def test_a_maintenance_window_silences_the_email(self, watched):
        page, monitor, *_ = watched
        incident = fail_monitor(monitor)
        window = MaintenanceWindow.objects.create(
            user=page.user,
            name="Upgrade",
            starts_at=timezone.now() - timedelta(minutes=1),
            ends_at=timezone.now() + timedelta(hours=1),
        )
        window.monitors.add(monitor)
        assert sent_mail().skipped == 2 and mail.outbox == []
        assert incident.ended_at is None


class TestAnnouncementEmails:
    def test_posting_emails_subscribers_and_resolving_follows_up(self, auth_api, watched):
        page, *_ = watched
        mail.outbox.clear()  # (signing in sent a login email)
        r = auth_api.post(
            f"{PAGES}{page.id}/announcements/",
            {"kind": "incident", "title": "Slow checkout", "body": "We are on it."},
            format="json",
        )
        assert sent_mail().sent == 2
        message = mail.outbox[0]
        assert message.subject == "[Acme status] Incident: Slow checkout"
        assert "We are on it." in message.body
        mail.outbox.clear()
        auth_api.post(f"{PAGES}{page.id}/announcements/{r.json()['id']}/resolve/")
        assert sent_mail().sent == 2
        assert mail.outbox[0].subject == "[Acme status] Resolved: Slow checkout"

    def test_maintenance_says_when(self, auth_api, watched):
        page, *_ = watched
        mail.outbox.clear()  # (signing in sent a login email)
        start = datetime(2026, 11, 1, 2, 0, tzinfo=UTC)
        auth_api.post(
            f"{PAGES}{page.id}/announcements/",
            {
                "kind": "maintenance",
                "title": "Upgrade",
                "starts_at": start.isoformat(),
                "ends_at": (start + timedelta(hours=2)).isoformat(),
            },
            format="json",
        )
        sent_mail()
        assert "Planned maintenance: Upgrade" in mail.outbox[0].subject
        assert "2026-11-01 02:00 UTC to 2026-11-01 04:00 UTC" in mail.outbox[0].body

    def test_you_can_post_without_emailing(self, auth_api, watched):
        page, *_ = watched
        mail.outbox.clear()  # (signing in sent a login email)
        auth_api.post(
            f"{PAGES}{page.id}/announcements/",
            {"kind": "incident", "title": "Quiet", "notify": False},
            format="json",
        )
        assert sent_mail().claimed == 0

    def test_resolving_before_the_email_went_out_cancels_it(self, auth_api, watched):
        page, *_ = watched
        mail.outbox.clear()  # (signing in sent a login email)
        r = auth_api.post(
            f"{PAGES}{page.id}/announcements/", {"kind": "incident", "title": "Blip"}, format="json"
        )
        auth_api.post(f"{PAGES}{page.id}/announcements/{r.json()['id']}/resolve/")
        assert sent_mail().sent == 0 and mail.outbox == []

    def test_a_draft_page_does_not_email_announcements(self, auth_api, user):
        page = page_for(user, public=False)
        subscriber_for(page)
        auth_api.post(
            f"{PAGES}{page.id}/announcements/", {"kind": "incident", "title": "x"}, format="json"
        )
        assert not StatusPageEmail.objects.exists()

    def test_a_title_cannot_add_mail_headers(self, auth_api, watched):
        page, *_ = watched
        mail.outbox.clear()  # (signing in sent a login email)
        auth_api.post(
            f"{PAGES}{page.id}/announcements/",
            {"kind": "incident", "title": "Hello\r\nBcc: evil@example.com"},
            format="json",
        )
        sent_mail()
        assert "\n" not in mail.outbox[0].subject and "\r" not in mail.outbox[0].subject
        assert mail.outbox[0].bcc == []


class TestMailQueue:
    def test_a_temporary_failure_is_retried_later(self, watched, monkeypatch):
        page, monitor, *_ = watched
        fail_monitor(monitor)
        real = page_mail.EmailMessage.send
        calls = {"n": 0}

        def flaky(self, *a, **k):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise smtplib.SMTPServerDisconnected("gone")
            return real(self, *a, **k)

        monkeypatch.setattr(page_mail.EmailMessage, "send", flaky)
        first = sent_mail()
        assert first.retrying == 2 and first.sent == 0
        assert set(StatusPageEmail.objects.values_list("status", flat=True)) == {"pending"}
        later = timezone.now() + timedelta(hours=1)
        assert page_mail.run_status_page_mail_once(now=later).sent == 2

    def test_a_refused_address_fails_for_good(self, watched, monkeypatch):
        _, monitor, *_ = watched
        fail_monitor(monitor)

        def refused(self, *a, **k):
            raise smtplib.SMTPRecipientsRefused({"x": (550, b"no")})

        monkeypatch.setattr(page_mail.EmailMessage, "send", refused)
        result = sent_mail()
        assert result.failed == 2
        assert set(StatusPageEmail.objects.values_list("status", flat=True)) == {"failed"}

    def test_it_gives_up_after_the_configured_attempts(self, watched, monkeypatch, settings):
        _, monitor, *_ = watched
        fail_monitor(monitor)

        def down(self, *a, **k):
            raise smtplib.SMTPServerDisconnected("gone")

        monkeypatch.setattr(page_mail.EmailMessage, "send", down)
        moment = timezone.now()
        for _ in range(settings.NOTIFY_MAX_ATTEMPTS):
            page_mail.run_status_page_mail_once(now=moment)
            moment += timedelta(hours=1)
        assert set(StatusPageEmail.objects.values_list("status", flat=True)) == {"failed"}

    def test_two_passes_never_send_the_same_email_twice(self, watched):
        _, monitor, *_ = watched
        fail_monitor(monitor)
        sent_mail()
        sent_mail()
        assert len(mail.outbox) == 2

    def test_old_finished_rows_are_pruned(self, watched):
        _, monitor, *_ = watched
        fail_monitor(monitor)
        sent_mail()
        StatusPageEmail.objects.update(created_at=timezone.now() - timedelta(days=40))
        sent_mail()
        assert not StatusPageEmail.objects.exists()


class TestEngineIntegration:
    def test_the_signed_trigger_also_sends_status_page_mail(self, visitor, user, settings):
        from monitoring import signing

        page = page_for(user)
        StatusPageSubscriber.objects.create(page=page, email="a@example.com")
        services.queue_confirmation(page.subscribers.get(), timezone.now())
        body = b""
        header = signing.sign(settings.SCHEDULER_SHARED_SECRET, body)
        r = visitor.post(
            "/internal/run-due-checks/",
            data=body,
            content_type="application/json",
            headers={signing.HEADER: header},
        )
        assert r.status_code == 200
        assert r.json()["status_page_mail"]["sent"] == 1
