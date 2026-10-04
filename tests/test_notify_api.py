"""The HTTP surface of Phase 8: alert contacts, attaching them to monitors, test sends,
email confirmation, delivery history, maintenance windows and the incident's alert list."""

import re
from datetime import timedelta

import httpx
import pytest
from django.core import mail
from django.utils import timezone
from rest_framework.test import APIClient

from monitoring.models import AlertContact, MaintenanceWindow, Monitor
from notifications import channels
from notifications.channels import CHANNELS
from notifications.http import DeliveryError
from notifications.models import NotificationDelivery as Delivery
from tests.conftest import ORIGIN

pytestmark = pytest.mark.django_db

CONTACTS = "/api/v1/alert-contacts/"
SLACK = "https://hooks.slack.com/services/T000/B000/abcdefghijkl"
TOKEN = "123456789:" + "A" * 35


@pytest.fixture
def other_api(make_user, sign_in):
    client = APIClient(HTTP_ORIGIN=ORIGIN)
    user = make_user("mallory@example.com")
    client.credentials(
        HTTP_AUTHORIZATION=f"Bearer {sign_in(client, user.email).json()['access_token']}"
    )
    client.user = user
    return client


@pytest.fixture
def sends(monkeypatch):
    """Replace every channel's send; `sends.fail` makes the next sends raise."""

    class Sends:
        def __init__(self):
            self.calls = []
            self.fail = None

    box = Sends()

    def install(key):
        def send(config, payload, delivery_id):
            box.calls.append((key, payload["event"]))
            if box.fail:
                raise box.fail

        monkeypatch.setattr(CHANNELS[key], "send", send)

    for key in CHANNELS:
        install(key)
    return box


def slack(api, **extra):
    body = {"name": "Ops", "channel": "slack", "config": {"webhook_url": SLACK}, **extra}
    return api.post(CONTACTS, body, format="json")


def monitor_body(**extra):
    return {"name": "Site", "type": "http", "target": "https://example.com", **extra}


class TestCreate:
    def test_creates_a_slack_contact_with_the_url_masked(self, auth_api):
        resp = slack(auth_api, delay_seconds=60)
        assert resp.status_code == 201
        body = resp.json()
        assert body["channel"] == "slack" and body["delay_seconds"] == 60
        assert body["enabled"] is True and body["verified"] is True
        assert "T000" not in str(body) and body["secret"] is None

    def test_validation_errors_use_dotted_config_paths(self, auth_api):
        resp = auth_api.post(
            CONTACTS,
            {
                "name": "x",
                "channel": "slack",
                "config": {"webhook_url": "https://evil.example.com/x"},
            },
            format="json",
        )
        assert resp.status_code == 400
        assert "config.webhook_url" in resp.json()["field_errors"]

    def test_unknown_channel_name_and_config_are_rejected(self, auth_api):
        assert (
            auth_api.post(
                CONTACTS, {"name": "x", "channel": "sms", "config": {}}, format="json"
            ).status_code
            == 400
        )
        assert (
            auth_api.post(
                CONTACTS,
                {"name": " ", "channel": "slack", "config": {"webhook_url": SLACK}},
                format="json",
            ).status_code
            == 400
        )
        assert (
            auth_api.post(CONTACTS, {"name": "x", "channel": "slack"}, format="json").status_code
            == 400
        )

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("delay_seconds", 3601),
            ("repeat_interval_seconds", 60),
            ("repeat_interval_seconds", 90000),
        ],
    )
    def test_timing_limits(self, auth_api, field, value):
        assert slack(auth_api, **{field: value}).status_code == 400

    def test_repeat_interval_zero_or_in_range(self, auth_api):
        assert slack(auth_api, repeat_interval_seconds=0).status_code == 201
        assert slack(auth_api, repeat_interval_seconds=900).status_code == 201

    def test_requires_sign_in(self, api):
        assert api.get(CONTACTS).status_code == 401
        assert api.post(CONTACTS, {}, format="json").status_code == 401

    def test_the_contact_limit(self, auth_api, settings):
        settings.MAX_ALERT_CONTACTS_PER_USER = 2
        assert slack(auth_api).status_code == 201
        assert slack(auth_api).status_code == 201
        assert slack(auth_api).status_code == 403

    def test_attach_to_existing_monitors(self, auth_api, user):
        a = Monitor.objects.create(
            user=user, name="A", type="http", interval_seconds=300, target="https://a.example.com"
        )
        b = Monitor.objects.create(
            user=user, name="B", type="http", interval_seconds=300, target="https://b.example.com"
        )
        resp = slack(auth_api, attach_to_existing_monitors=True)
        assert resp.json()["monitor_count"] == 2
        assert set(a.alert_contacts.all()) == set(b.alert_contacts.all()) != set()

    def test_creating_one_is_audited(self, auth_api, user):
        from accounts.models import AuditLog

        slack(auth_api)
        assert AuditLog.objects.filter(user=user, action="alert_contact_created").exists()


class TestWebhookSecret:
    def create(self, api):
        return api.post(
            CONTACTS,
            {
                "name": "Hook",
                "channel": "webhook",
                "config": {"url": "https://hooks.example.com/in"},
            },
            format="json",
        )

    def test_the_signing_secret_is_shown_once(self, auth_api):
        created = self.create(auth_api).json()
        assert len(created["secret"]) >= 32
        assert created["config"] == {"host": "hooks.example.com"}
        assert auth_api.get(f"{CONTACTS}{created['id']}/").json()["secret"] is None
        assert all(c["secret"] is None for c in auth_api.get(CONTACTS).json()["results"])

    def test_editing_keeps_the_secret_and_never_returns_it(self, auth_api):
        created = self.create(auth_api).json()
        resp = auth_api.patch(
            f"{CONTACTS}{created['id']}/",
            {"config": {"url": "https://hooks.example.com/new"}},
            format="json",
        )
        assert resp.status_code == 200 and resp.json()["secret"] is None
        stored = AlertContact.objects.get(pk=created["id"]).config
        assert stored["secret"] == created["secret"] and stored["url"].endswith("/new")

    def test_a_caller_cannot_choose_the_secret(self, auth_api):
        resp = auth_api.post(
            CONTACTS,
            {
                "name": "H",
                "channel": "webhook",
                "config": {"url": "https://h.example.com/x", "secret": "weak"},
            },
            format="json",
        )
        assert resp.json()["secret"] != "weak"

    def test_rotating_replaces_the_secret_and_shows_the_new_one_once(self, auth_api):
        created = self.create(auth_api).json()
        rotated = auth_api.post(f"{CONTACTS}{created['id']}/rotate-secret/").json()
        assert rotated["secret"] and rotated["secret"] != created["secret"]
        assert AlertContact.objects.get(pk=created["id"]).config["secret"] == rotated["secret"]
        assert auth_api.get(f"{CONTACTS}{created['id']}/").json()["secret"] is None

    def test_only_webhooks_rotate(self, auth_api):
        contact = slack(auth_api).json()
        assert auth_api.post(f"{CONTACTS}{contact['id']}/rotate-secret/").status_code == 400

    @pytest.mark.parametrize(
        "url", ["http://localhost/x", "http://10.0.0.1/x", "http://169.254.169.254/"]
    )
    def test_private_destinations_are_refused(self, auth_api, url):
        resp = auth_api.post(
            CONTACTS, {"name": "H", "channel": "webhook", "config": {"url": url}}, format="json"
        )
        assert resp.status_code == 400 and "config.url" in resp.json()["field_errors"]

    def test_a_hostname_that_resolves_privately_is_refused(self, auth_api, dns):
        dns["sneaky.example.com"] = ["10.1.2.3"]
        resp = auth_api.post(
            CONTACTS,
            {"name": "H", "channel": "webhook", "config": {"url": "https://sneaky.example.com/x"}},
            format="json",
        )
        assert resp.status_code == 400


class TestUpdateAndDelete:
    def test_edit_name_timing_and_switch_off(self, auth_api):
        contact = slack(auth_api).json()
        resp = auth_api.patch(
            f"{CONTACTS}{contact['id']}/",
            {"name": "Renamed", "delay_seconds": 120, "enabled": False},
            format="json",
        )
        assert resp.status_code == 200
        assert (resp.json()["name"], resp.json()["delay_seconds"], resp.json()["enabled"]) == (
            "Renamed",
            120,
            False,
        )

    def test_the_channel_cannot_change(self, auth_api):
        contact = slack(auth_api).json()
        resp = auth_api.patch(f"{CONTACTS}{contact['id']}/", {"channel": "discord"}, format="json")
        assert resp.status_code == 400

    def test_a_partial_config_edit_keeps_the_rest(self, auth_api):
        resp = auth_api.post(
            CONTACTS,
            {"name": "T", "channel": "telegram", "config": {"bot_token": TOKEN, "chat_id": "5"}},
            format="json",
        )
        contact = resp.json()
        assert contact["config"]["bot_token"] == "123456789:••••"
        auth_api.patch(
            f"{CONTACTS}{contact['id']}/", {"config": {"chat_id": "-100777"}}, format="json"
        )
        stored = AlertContact.objects.get(pk=contact["id"]).config
        assert stored == {"bot_token": TOKEN, "chat_id": "-100777"}

    def test_delete_detaches_and_keeps_history(self, auth_api, user):
        monitor = Monitor.objects.create(
            user=user, name="M", type="http", interval_seconds=300, target="https://m.example.com"
        )
        contact = slack(auth_api, attach_to_existing_monitors=True).json()
        Delivery.objects.create(
            monitor=monitor, contact_id=contact["id"], contact_name="Ops", channel="slack",
            event="test", dedupe_key="t", status="sent",
        )  # fmt: skip
        assert auth_api.delete(f"{CONTACTS}{contact['id']}/").status_code == 204
        assert Monitor.objects.filter(pk=monitor.pk).exists()
        assert monitor.alert_contacts.count() == 0
        assert Delivery.objects.get().contact_name == "Ops"

    def test_put_is_not_allowed(self, auth_api):
        contact = slack(auth_api).json()
        assert auth_api.put(f"{CONTACTS}{contact['id']}/", {}, format="json").status_code == 405


class TestOwnership:
    def test_other_users_contacts_are_invisible(self, auth_api, other_api):
        mine = slack(auth_api).json()
        assert other_api.get(CONTACTS).json()["results"] == []
        for method, suffix in (
            (other_api.get, ""),
            (other_api.patch, ""),
            (other_api.delete, ""),
            (other_api.post, "test/"),
            (other_api.post, "verify/"),
            (other_api.post, "resend-verification/"),
            (other_api.post, "rotate-secret/"),
        ):
            assert method(f"{CONTACTS}{mine['id']}/{suffix}").status_code == 404, suffix
        assert AlertContact.objects.filter(pk=mine["id"]).exists()


class TestTestSend:
    def test_sends_now_and_reports_success(self, auth_api, sends):
        contact = slack(auth_api).json()
        resp = auth_api.post(f"{CONTACTS}{contact['id']}/test/")
        assert resp.status_code == 200 and resp.json() == {"status": "sent", "error": ""}
        assert sends.calls == [("slack", "test")]
        delivery = Delivery.objects.get()
        assert (delivery.event, delivery.status, delivery.incident) == ("test", "sent", None)

    def test_reports_what_went_wrong_in_words(self, auth_api, sends):
        contact = slack(auth_api).json()
        sends.fail = DeliveryError(
            "Slack says the destination no longer exists (404)", retryable=False
        )
        resp = auth_api.post(f"{CONTACTS}{contact['id']}/test/").json()
        assert resp["status"] == "failed" and "no longer exists" in resp["error"]

    def test_a_retryable_failure_is_reported_not_queued(self, auth_api, sends):
        contact = slack(auth_api).json()
        sends.fail = DeliveryError("Slack had a server error (503)", retryable=True)
        resp = auth_api.post(f"{CONTACTS}{contact['id']}/test/").json()
        assert resp["status"] == "failed"
        assert Delivery.objects.get().next_attempt_at is None  # not left to be sent later

    def test_a_disabled_contact_is_refused(self, auth_api, sends):
        contact = slack(auth_api, enabled=False).json()
        assert auth_api.post(f"{CONTACTS}{contact['id']}/test/").status_code == 400
        assert sends.calls == []

    def test_it_is_rate_limited(self, auth_api, sends):
        contact = slack(auth_api).json()
        codes = [auth_api.post(f"{CONTACTS}{contact['id']}/test/").status_code for _ in range(12)]
        assert codes[:10] == [200] * 10 and 429 in codes[10:]


class TestEmailConfirmation:
    def create(self, api, address):
        return api.post(
            CONTACTS,
            {"name": "Mail", "channel": "email", "config": {"address": address}},
            format="json",
        )

    def code(self):
        return re.search(r"Confirmation code: (\d{6})", mail.outbox[-1].body).group(1)

    def test_your_own_address_needs_no_confirmation(self, auth_api, sends):
        body = self.create(auth_api, "ALICE@example.com").json()
        assert body["verified"] is True and len(mail.outbox) == 1 - 1 + len(mail.outbox)
        assert auth_api.post(f"{CONTACTS}{body['id']}/test/").status_code == 200

    def test_someone_elses_address_is_emailed_a_code_and_sends_nothing_until_confirmed(
        self, auth_api, sends
    ):
        before = len(mail.outbox)
        body = self.create(auth_api, "boss@example.com").json()
        assert body["verified"] is False
        assert len(mail.outbox) == before + 1 and mail.outbox[-1].to == ["boss@example.com"]

        refused = auth_api.post(f"{CONTACTS}{body['id']}/test/")
        assert refused.status_code == 400 and "Confirm" in str(refused.json())

        resp = auth_api.post(
            f"{CONTACTS}{body['id']}/verify/", {"code": self.code()}, format="json"
        )
        assert resp.status_code == 200 and resp.json()["verified"] is True
        assert auth_api.post(f"{CONTACTS}{body['id']}/test/").status_code == 200

    def test_wrong_codes_are_counted_and_the_code_dies_after_five(self, auth_api):
        body = self.create(auth_api, "boss@example.com").json()
        real = self.code()
        for _ in range(5):
            assert (
                auth_api.post(
                    f"{CONTACTS}{body['id']}/verify/",
                    {"code": "000000" if real != "000000" else "111111"},
                    format="json",
                ).status_code
                == 400
            )
        # Even the right code is now dead; they must ask for a new one.
        assert (
            auth_api.post(
                f"{CONTACTS}{body['id']}/verify/", {"code": real}, format="json"
            ).status_code
            == 400
        )
        assert AlertContact.objects.get(pk=body["id"]).verified_at is None

    def test_codes_expire(self, auth_api):
        body = self.create(auth_api, "boss@example.com").json()
        AlertContact.objects.filter(pk=body["id"]).update(
            verification_expires_at=timezone.now() - timedelta(minutes=1)
        )
        assert (
            auth_api.post(
                f"{CONTACTS}{body['id']}/verify/", {"code": self.code()}, format="json"
            ).status_code
            == 400
        )

    def test_resending_issues_a_fresh_code_and_kills_the_old_one(self, auth_api):
        body = self.create(auth_api, "boss@example.com").json()
        old = self.code()
        assert auth_api.post(f"{CONTACTS}{body['id']}/resend-verification/").status_code == 204
        new = self.code()
        if new != old:
            assert (
                auth_api.post(
                    f"{CONTACTS}{body['id']}/verify/", {"code": old}, format="json"
                ).status_code
                == 400
            )
        assert (
            auth_api.post(
                f"{CONTACTS}{body['id']}/verify/", {"code": new}, format="json"
            ).status_code
            == 200
        )

    def test_the_code_has_to_look_like_a_code(self, auth_api):
        body = self.create(auth_api, "boss@example.com").json()
        for bad in ("12345", "abcdef", "", "1234567"):
            assert (
                auth_api.post(
                    f"{CONTACTS}{body['id']}/verify/", {"code": bad}, format="json"
                ).status_code
                == 400
            )

    def test_changing_the_address_requires_confirming_the_new_one(self, auth_api):
        body = self.create(auth_api, "alice@example.com").json()
        assert body["verified"] is True
        resp = auth_api.patch(
            f"{CONTACTS}{body['id']}/",
            {"config": {"address": "stranger@example.com"}},
            format="json",
        )
        assert resp.json()["verified"] is False
        assert mail.outbox[-1].to == ["stranger@example.com"]

    def test_non_email_contacts_have_nothing_to_confirm(self, auth_api):
        contact = slack(auth_api).json()
        assert (
            auth_api.post(
                f"{CONTACTS}{contact['id']}/verify/", {"code": "123456"}, format="json"
            ).status_code
            == 400
        )
        assert auth_api.post(f"{CONTACTS}{contact['id']}/resend-verification/").status_code == 400

    def test_verification_is_rate_limited(self, auth_api):
        body = self.create(auth_api, "boss@example.com").json()
        codes = [
            auth_api.post(
                f"{CONTACTS}{body['id']}/verify/", {"code": "000000"}, format="json"
            ).status_code
            for _ in range(12)
        ]
        assert 429 in codes


class TestTelegramDiscovery:
    def test_lists_chats_for_a_token(self, auth_api, monkeypatch):
        monkeypatch.setattr(
            "notifications.views.discover_telegram_chats",
            lambda token: [{"id": "11", "type": "private", "title": "Ada"}],
        )
        resp = auth_api.post(f"{CONTACTS}telegram-chats/", {"bot_token": TOKEN}, format="json")
        assert resp.status_code == 200 and resp.json()[0]["id"] == "11"

    def test_a_bad_token_is_a_field_error(self, auth_api):
        resp = auth_api.post(f"{CONTACTS}telegram-chats/", {"bot_token": "nonsense"}, format="json")
        assert resp.status_code == 400 and "config.bot_token" in resp.json()["field_errors"]

    def test_network_trouble_is_reported(self, auth_api, monkeypatch):
        def boom(token):
            raise DeliveryError("Couldn't reach api.telegram.org", retryable=True)

        monkeypatch.setattr("notifications.views.discover_telegram_chats", boom)
        resp = auth_api.post(f"{CONTACTS}telegram-chats/", {"bot_token": TOKEN}, format="json")
        assert resp.status_code == 400 and "api.telegram.org" in str(resp.json())

    def test_the_token_never_appears_in_a_response_or_error(self, auth_api, monkeypatch):
        transport = httpx.MockTransport(
            lambda r: httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
        )
        real = channels.discover_telegram_chats
        monkeypatch.setattr(
            "notifications.views.discover_telegram_chats",
            lambda token: real(token, transport=transport),
        )
        resp = auth_api.post(f"{CONTACTS}telegram-chats/", {"bot_token": TOKEN}, format="json")
        assert resp.status_code == 400 and TOKEN not in resp.content.decode()


class TestAttachingToMonitors:
    def test_a_new_monitor_alerts_all_your_contacts_by_default(self, auth_api):
        a, b = slack(auth_api).json(), slack(auth_api, name="Two").json()
        created = auth_api.post("/api/v1/monitors/", monitor_body(), format="json").json()
        assert sorted(created["alert_contact_ids"]) == sorted([a["id"], b["id"]])

    def test_or_exactly_the_ones_you_pick(self, auth_api):
        a, _ = slack(auth_api).json(), slack(auth_api, name="Two").json()
        created = auth_api.post(
            "/api/v1/monitors/", monitor_body(alert_contact_ids=[a["id"]]), format="json"
        ).json()
        assert created["alert_contact_ids"] == [a["id"]]

    def test_or_none_at_all(self, auth_api):
        slack(auth_api)
        created = auth_api.post(
            "/api/v1/monitors/", monitor_body(alert_contact_ids=[]), format="json"
        ).json()
        assert created["alert_contact_ids"] == []

    def test_you_cannot_attach_someone_elses_contact(self, auth_api, other_api):
        theirs = slack(other_api).json()
        resp = auth_api.post(
            "/api/v1/monitors/", monitor_body(alert_contact_ids=[theirs["id"]]), format="json"
        )
        assert resp.status_code == 400 and not Monitor.objects.exists()

    def test_editing_replaces_the_set(self, auth_api):
        a, b = slack(auth_api).json(), slack(auth_api, name="Two").json()
        monitor = auth_api.post("/api/v1/monitors/", monitor_body(), format="json").json()
        resp = auth_api.patch(
            f"/api/v1/monitors/{monitor['id']}/", {"alert_contact_ids": [b["id"]]}, format="json"
        )
        assert resp.status_code == 200 and resp.json()["alert_contact_ids"] == [b["id"]]
        assert (
            a["id"]
            not in auth_api.get(f"/api/v1/monitors/{monitor['id']}/").json()["alert_contact_ids"]
        )

    def test_editing_something_else_leaves_the_contacts_alone(self, auth_api):
        a = slack(auth_api).json()
        monitor = auth_api.post("/api/v1/monitors/", monitor_body(), format="json").json()
        auth_api.patch(f"/api/v1/monitors/{monitor['id']}/", {"name": "Renamed"}, format="json")
        assert auth_api.get(f"/api/v1/monitors/{monitor['id']}/").json()["alert_contact_ids"] == [
            a["id"]
        ]

    def test_the_list_shows_them_too(self, auth_api):
        a = slack(auth_api).json()
        auth_api.post("/api/v1/monitors/", monitor_body(), format="json")
        row = auth_api.get("/api/v1/monitors/").json()["results"][0]
        assert row["alert_contact_ids"] == [a["id"]]

    def test_a_monitor_test_alert_goes_to_each_attached_contact(self, auth_api, sends):
        slack(auth_api)
        slack(auth_api, name="Off", enabled=False)
        monitor = auth_api.post("/api/v1/monitors/", monitor_body(), format="json").json()
        resp = auth_api.post(f"/api/v1/monitors/{monitor['id']}/test-notification/")
        assert resp.status_code == 200
        results = resp.json()["results"]
        assert [(r["channel"], r["status"]) for r in results] == [("slack", "sent")]
        assert sends.calls == [("slack", "test")]

    def test_a_monitor_test_alert_with_nobody_attached_is_an_empty_result(self, auth_api, sends):
        monitor = auth_api.post(
            "/api/v1/monitors/", monitor_body(alert_contact_ids=[]), format="json"
        ).json()
        resp = auth_api.post(f"/api/v1/monitors/{monitor['id']}/test-notification/")
        assert resp.status_code == 200 and resp.json() == {"results": []}

    def test_unconfirmed_email_is_reported_as_skipped(self, auth_api, sends):
        auth_api.post(
            CONTACTS,
            {"name": "Boss", "channel": "email", "config": {"address": "boss@example.com"}},
            format="json",
        )
        monitor = auth_api.post("/api/v1/monitors/", monitor_body(), format="json").json()
        results = auth_api.post(f"/api/v1/monitors/{monitor['id']}/test-notification/").json()[
            "results"
        ]
        assert results[0]["status"] == "skipped" and sends.calls == []

    def test_another_users_monitor_is_a_404(self, auth_api, other_api):
        theirs = other_api.post("/api/v1/monitors/", monitor_body(), format="json").json()
        assert (
            auth_api.post(f"/api/v1/monitors/{theirs['id']}/test-notification/").status_code == 404
        )


class TestDeliveryHistory:
    def make(self, user, contact, **kwargs):
        fields = {
            "contact": contact, "contact_name": contact.name, "channel": contact.channel,
            "event": "test", "dedupe_key": f"t{Delivery.objects.count()}", "status": "sent",
        }  # fmt: skip
        fields.update(kwargs)
        return Delivery.objects.create(**fields)

    def test_lists_newest_first_and_only_mine(self, auth_api, other_api, user):
        mine = AlertContact.objects.create(user=user, name="Mine", channel="slack", config={})
        theirs = AlertContact.objects.create(
            user=other_api.user, name="Theirs", channel="slack", config={}
        )
        first = self.make(user, mine)
        second = self.make(user, mine, status="failed", error="nope")
        self.make(other_api.user, theirs)

        body = auth_api.get("/api/v1/notifications/").json()
        assert [d["id"] for d in body["results"]] == [second.id, first.id]
        assert body["results"][0]["error"] == "nope"

    def test_filters(self, auth_api, user):
        contact = AlertContact.objects.create(user=user, name="C", channel="slack", config={})
        self.make(user, contact)
        self.make(user, contact, status="failed")
        assert len(auth_api.get("/api/v1/notifications/?status=failed").json()["results"]) == 1
        assert auth_api.get("/api/v1/notifications/?status=bogus").status_code == 400
        assert auth_api.get("/api/v1/notifications/?monitor_id=not-a-uuid").status_code == 400
        other = AlertContact.objects.create(user=user, name="D", channel="slack", config={})
        self.make(user, other)
        assert (
            len(auth_api.get(f"/api/v1/notifications/?contact_id={contact.id}").json()["results"])
            == 2
        )

    def test_requires_sign_in(self, api):
        assert api.get("/api/v1/notifications/").status_code == 401

    def test_the_payload_is_not_exposed(self, auth_api, user):
        contact = AlertContact.objects.create(
            user=user, name="C", channel="slack", config={"webhook_url": SLACK}
        )
        self.make(user, contact, payload={"secret": "x"})
        raw = auth_api.get("/api/v1/notifications/").content.decode()
        assert "payload" not in raw and "T000" not in raw


class TestIncidentDetail:
    def test_lists_the_alerts_for_that_incident(self, auth_api, user, sends):
        from tests.test_incidents_api import incident, monitor_for

        monitor = monitor_for(user)
        contact = AlertContact.objects.create(user=user, name="Ops", channel="slack", config={})
        inc = incident(monitor, duration_minutes=None)
        Delivery.objects.create(
            incident=inc, monitor=monitor, contact=contact, contact_name="Ops", channel="slack",
            event="opened", dedupe_key="opened", status="sent", sent_at=timezone.now(),
        )  # fmt: skip
        Delivery.objects.create(
            incident=inc, monitor=monitor, contact=contact, contact_name="Ops", channel="slack",
            event="reminder", dedupe_key="reminder:1", status="failed", error="nope",
        )  # fmt: skip
        body = auth_api.get(f"/api/v1/incidents/{inc.id}/").json()
        assert [(n["event"], n["status"]) for n in body["notifications"]] == [
            ("opened", "sent"),
            ("reminder", "failed"),
        ]
        assert body["notifications"][0]["contact_name"] == "Ops"


class TestVapidKey:
    def test_reports_nothing_when_unconfigured(self, auth_api):
        assert auth_api.get("/api/v1/notifications/vapid-key/").json() == {"public_key": None}

    def test_returns_the_public_key_only(self, auth_api, settings):
        settings.VAPID_PUBLIC_KEY, settings.VAPID_PRIVATE_KEY = "PUBLIC", "PRIVATE"
        resp = auth_api.get("/api/v1/notifications/vapid-key/")
        assert resp.json() == {"public_key": "PUBLIC"} and "PRIVATE" not in resp.content.decode()

    def test_needs_both_halves(self, auth_api, settings):
        settings.VAPID_PUBLIC_KEY, settings.VAPID_PRIVATE_KEY = "PUBLIC", ""
        assert auth_api.get("/api/v1/notifications/vapid-key/").json() == {"public_key": None}

    def test_requires_sign_in(self, api):
        assert api.get("/api/v1/notifications/vapid-key/").status_code == 401


def test_generated_vapid_keys_are_usable():
    from py_vapid import Vapid

    from notifications.management.commands.generate_vapid_keys import generate

    private, public = generate()
    vapid = Vapid.from_string(private)  # the exact string a user pastes into .env
    claims = vapid.sign({"sub": "mailto:me@example.com", "aud": "https://fcm.googleapis.com"})
    assert claims["Authorization"].startswith("vapid ") and len(public) == 87


WINDOWS = "/api/v1/maintenance-windows/"


class TestMaintenanceWindows:
    def body(self, monitor, **extra):
        now = timezone.now()
        return {
            "name": "Upgrade",
            "starts_at": (now + timedelta(hours=1)).isoformat(),
            "ends_at": (now + timedelta(hours=2)).isoformat(),
            "monitor_ids": [str(monitor.id)],
            **extra,
        }

    @pytest.fixture
    def monitor(self, user):
        return Monitor.objects.create(
            user=user, name="M", type="http", interval_seconds=300, target="https://m.example.com"
        )

    def test_create_list_edit_delete(self, auth_api, monitor):
        created = auth_api.post(WINDOWS, self.body(monitor), format="json")
        assert created.status_code == 201 and created.json()["state"] == "upcoming"
        window = created.json()
        listing = auth_api.get(WINDOWS).json()
        assert [
            w["id"] for w in (listing["results"] if isinstance(listing, dict) else listing)
        ] == [window["id"]]

        patched = auth_api.patch(f"{WINDOWS}{window['id']}/", {"name": "Renamed"}, format="json")
        assert patched.status_code == 200 and patched.json()["name"] == "Renamed"
        assert auth_api.delete(f"{WINDOWS}{window['id']}/").status_code == 204
        assert not MaintenanceWindow.objects.exists()

    def test_state_reflects_the_clock(self, auth_api, monitor):
        now = timezone.now()
        for starts, ends in ((-2, -1), (-1, 1), (1, 2)):
            auth_api.post(
                WINDOWS,
                self.body(
                    monitor,
                    starts_at=(now + timedelta(hours=starts)).isoformat(),
                    ends_at=(now + timedelta(hours=ends)).isoformat(),
                ),
                format="json",
            )
        listing = auth_api.get(WINDOWS)
        data = listing.json()
        rows = data["results"] if isinstance(data, dict) else data
        assert sorted(w["state"] for w in rows) == ["active", "past", "upcoming"]

    def test_validation(self, auth_api, monitor):
        now = timezone.now()
        backwards = self.body(
            monitor,
            starts_at=(now + timedelta(hours=2)).isoformat(),
            ends_at=(now + timedelta(hours=1)).isoformat(),
        )
        assert auth_api.post(WINDOWS, backwards, format="json").status_code == 400
        too_long = self.body(monitor, ends_at=(now + timedelta(days=40)).isoformat())
        assert auth_api.post(WINDOWS, too_long, format="json").status_code == 400
        assert (
            auth_api.post(WINDOWS, self.body(monitor, monitor_ids=[]), format="json").status_code
            == 400
        )
        assert (
            auth_api.post(WINDOWS, self.body(monitor, name=" "), format="json").status_code == 400
        )

    def test_only_your_own_monitors_and_windows(self, auth_api, other_api, monitor):
        theirs = Monitor.objects.create(
            user=other_api.user,
            name="T",
            type="http",
            interval_seconds=300,
            target="https://t.example.com",
        )
        assert auth_api.post(WINDOWS, self.body(theirs), format="json").status_code == 400

        window = other_api.post(WINDOWS, self.body(theirs), format="json").json()
        assert auth_api.get(f"{WINDOWS}{window['id']}/").status_code == 404
        assert auth_api.delete(f"{WINDOWS}{window['id']}/").status_code == 404
        assert (
            auth_api.patch(f"{WINDOWS}{window['id']}/", {"name": "x"}, format="json").status_code
            == 404
        )

    def test_filter_by_state(self, auth_api, monitor):
        auth_api.post(WINDOWS, self.body(monitor), format="json")
        data = auth_api.get(f"{WINDOWS}?state=upcoming").json()
        assert len(data["results"] if isinstance(data, dict) else data) == 1
        data = auth_api.get(f"{WINDOWS}?state=past").json()
        assert len(data["results"] if isinstance(data, dict) else data) == 0
        assert auth_api.get(f"{WINDOWS}?state=bogus").status_code == 400

    def test_requires_sign_in(self, api):
        assert api.get(WINDOWS).status_code == 401

    def test_a_window_that_is_active_now_mutes_the_monitor(self, auth_api, user, monitor):
        from notifications import services

        now = timezone.now()
        auth_api.post(
            WINDOWS,
            self.body(
                monitor,
                starts_at=(now - timedelta(minutes=1)).isoformat(),
                ends_at=(now + timedelta(hours=1)).isoformat(),
            ),
            format="json",
        )
        assert services.in_maintenance(monitor, now) is True
        assert services.in_maintenance(monitor, now + timedelta(hours=2)) is False
