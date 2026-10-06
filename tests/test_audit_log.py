"""The account's own history: what the Security page shows so "was that me?" has an answer."""

from datetime import timedelta

import pytest
from django.utils import timezone

from accounts.models import AuditLog
from apikeys.models import ApiKey

pytestmark = pytest.mark.django_db

URL = "/api/v1/auth/audit-log/"


def entry(user, action, ip="203.0.113.7", minutes_ago=0, **metadata):
    row = AuditLog.objects.create(user=user, action=action, ip_address=ip, metadata=metadata)
    AuditLog.objects.filter(pk=row.pk).update(
        created_at=timezone.now() - timedelta(minutes=minutes_ago)
    )
    return row


def test_it_lists_your_events_newest_first(auth_api, user):
    # (signing in for the test made its own "login" a moment ago; these are later still)
    entry(user, "login", minutes_ago=-1)
    entry(user, "api_key_created", ip="198.51.100.9", minutes_ago=-2)
    body = auth_api.get(URL).json()
    actions = [e["action"] for e in body["results"]]
    assert actions[0] == "api_key_created"
    assert {"login", "api_key_created"} <= set(actions)
    first = body["results"][0]
    assert set(first) == {"id", "action", "ip_address", "created_at"}
    assert first["ip_address"] == "198.51.100.9"


def test_the_stored_details_are_never_shown(auth_api, user):
    entry(user, "api_key_created", key_id="abc", scope="full", secret_looking="hunter2")
    text = auth_api.get(URL).content.decode()
    assert "hunter2" not in text and "key_id" not in text and "metadata" not in text


def test_only_your_own_entries_appear(auth_api, user, make_user):
    carol = make_user("carol@example.com")
    entry(carol, "register", ip="192.0.2.99")
    entry(user, "login", ip="203.0.113.1")
    text = auth_api.get(URL).content.decode()
    assert "192.0.2.99" not in text and "carol" not in text
    # events that belong to nobody (a failed sign-in for an unknown email) aren't anyone's to see
    AuditLog.objects.create(user=None, action="email_verify_failed", ip_address="192.0.2.50")
    assert "192.0.2.50" not in auth_api.get(URL).content.decode()


def test_it_is_paged(auth_api, user):
    AuditLog.objects.bulk_create(AuditLog(user=user, action="login") for _ in range(30))
    first = auth_api.get(URL).json()
    assert first["next"] and len(first["results"]) == 25
    second = auth_api.get(first["next"]).json()
    assert second["previous"] and len(second["results"]) >= 5


def test_real_activity_shows_up(auth_api, user):
    auth_api.post("/api/v1/api-keys/", {"name": "ci"}, format="json")
    actions = [e["action"] for e in auth_api.get(URL).json()["results"]]
    assert "api_key_created" in actions and "login" in actions


def test_signed_out_callers_get_nothing(api):
    assert api.get(URL).status_code == 401


def test_a_read_only_key_can_look_at_it_but_not_change_anything(user):
    from rest_framework.test import APIClient

    _, raw = ApiKey.issue(user, "ci", "read")
    client = APIClient(HTTP_ORIGIN="http://localhost:3000")
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert client.get(URL).status_code == 200
    assert client.post(URL, {}, format="json").status_code == 403
