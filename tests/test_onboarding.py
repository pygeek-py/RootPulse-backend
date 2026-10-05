"""The setup checklist reads the person's real data, so these tests do things (create a monitor,
connect a source) and watch the steps follow, in both directions."""

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from accounts import onboarding
from deploys.models import DeploySource
from monitoring.models import AlertContact, Monitor
from providers.models import Provider, UserProviderSubscription
from statuspages.models import StatusPage

pytestmark = pytest.mark.django_db

URL = "/api/v1/auth/onboarding/"
ORDER = ["monitor", "alerts", "deploys", "dependencies", "status-page"]


def state(client):
    body = client.get(URL).json()
    return {s["id"]: s["done"] for s in body["steps"]}, body


def monitor_for(user):
    return Monitor.objects.create(
        user=user,
        name="Site",
        type="http",
        target="https://example.com",
        interval_seconds=300,
        config={},
        next_check_at=timezone.now(),
    )


def contact_for(user, channel="slack", verified=True, enabled=True):
    return AlertContact.objects.create(
        user=user,
        name="Team",
        channel=channel,
        config={},
        enabled=enabled,
        verified_at=timezone.now() if verified else None,
    )


def source_for(user):
    return DeploySource.objects.create(user=user, name="Repo", type="github", secret="s" * 20)


def subscription_for(user):
    return UserProviderSubscription.objects.create(
        user=user, provider=Provider.objects.get(slug="github")
    )


def page_for(user, slug="acme"):
    return StatusPage.objects.create(user=user, name="Acme", slug=slug)


class TestSteps:
    def test_a_new_account_has_nothing_done(self, auth_api):
        done, body = state(auth_api)
        assert list(done) == ORDER and not any(done.values())
        assert body["done_count"] == 0 and body["total"] == 5
        assert body["completed_at"] is None and body["dismissed"] is False

    def test_each_step_ticks_when_its_thing_exists(self, auth_api, user):
        monitor_for(user)
        assert state(auth_api)[0]["monitor"] is True
        contact_for(user)
        assert state(auth_api)[0]["alerts"] is True
        source_for(user)
        assert state(auth_api)[0]["deploys"] is True
        subscription_for(user)
        assert state(auth_api)[0]["dependencies"] is True
        page_for(user)
        done, body = state(auth_api)
        assert all(done.values()) and body["done_count"] == 5

    def test_progress_follows_the_data_both_ways(self, auth_api, user):
        m = monitor_for(user)
        assert state(auth_api)[0]["monitor"] is True
        m.delete()
        assert state(auth_api)[0]["monitor"] is False

    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            ({"channel": "slack"}, True),
            ({"channel": "webhook"}, True),
            ({"channel": "email", "verified": True}, True),
            ({"channel": "email", "verified": False}, False),
            ({"channel": "slack", "enabled": False}, False),
        ],
    )
    def test_an_alert_channel_has_to_be_able_to_reach_you(self, auth_api, user, kwargs, expected):
        contact_for(user, **kwargs)
        assert state(auth_api)[0]["alerts"] is expected

    def test_other_peoples_things_do_not_count(self, auth_api, make_user):
        carol = make_user("carol@example.com")
        monitor_for(carol)
        contact_for(carol)
        source_for(carol)
        subscription_for(carol)
        page_for(carol)
        assert not any(state(auth_api)[0].values())


class TestCompletion:
    def finish(self, user):
        monitor_for(user)
        contact_for(user)
        source_for(user)
        subscription_for(user)
        page_for(user)

    def test_finishing_is_stamped_once(self, auth_api, user):
        self.finish(user)
        first = auth_api.get(URL).json()["completed_at"]
        assert first is not None
        assert auth_api.get(URL).json()["completed_at"] == first

    def test_the_stamp_survives_later_changes(self, auth_api, user):
        """Finished is a moment, not a state: removing a step later reopens that step but doesn't
        un-celebrate."""
        self.finish(user)
        stamped = auth_api.get(URL).json()["completed_at"]
        Monitor.objects.all().delete()
        done, body = state(auth_api)
        assert done["monitor"] is False and body["completed_at"] == stamped

    def test_four_of_five_is_not_finished(self, auth_api, user):
        monitor_for(user)
        contact_for(user)
        source_for(user)
        subscription_for(user)
        assert auth_api.get(URL).json()["completed_at"] is None


class TestDismiss:
    def test_dismiss_and_bring_it_back(self, auth_api, user):
        r = auth_api.patch(URL, {"dismissed": True}, format="json")
        assert r.status_code == 200 and r.json()["dismissed"] is True
        user.refresh_from_db()
        assert user.onboarding_dismissed_at is not None
        assert auth_api.get(URL).json()["dismissed"] is True
        assert auth_api.patch(URL, {"dismissed": False}, format="json").json()["dismissed"] is False

    def test_dismissing_does_not_hide_the_steps_or_change_them(self, auth_api, user):
        monitor_for(user)
        body = auth_api.patch(URL, {"dismissed": True}, format="json").json()
        assert body["done_count"] == 1 and len(body["steps"]) == 5

    @pytest.mark.parametrize("payload", [{}, {"dismissed": "maybe"}, {"dismissed": None}])
    def test_bad_requests_are_refused(self, auth_api, payload):
        assert auth_api.patch(URL, payload, format="json").status_code == 400

    def test_it_is_per_person(self, auth_api, make_user):
        carol = make_user("carol@example.com")
        auth_api.patch(URL, {"dismissed": True}, format="json")
        carol.refresh_from_db()
        assert carol.onboarding_dismissed_at is None


class TestAccess:
    def test_signed_out_callers_are_refused(self):
        anon = APIClient(HTTP_ORIGIN="http://localhost:3000")
        assert anon.get(URL).status_code == 401
        assert anon.patch(URL, {"dismissed": True}, format="json").status_code == 401

    def test_the_user_endpoint_is_unchanged(self, auth_api):
        body = auth_api.get("/api/v1/auth/me/").json()
        assert "onboarding_completed_at" not in body and "onboarding_dismissed_at" not in body

    def test_the_step_list_is_the_documented_one(self):
        assert list(onboarding.STEP_IDS) == ORDER
