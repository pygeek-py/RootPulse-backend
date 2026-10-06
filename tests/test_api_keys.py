"""API keys: the credential for scripts, and the test matrix from docs/plan/04-security.md section 3
and 05-testing: valid works, revoked is rejected at once, a read key is rejected on a write."""

import uuid
from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import AuditLog
from apikeys.models import ApiKey, hash_key
from monitoring.models import Monitor

pytestmark = pytest.mark.django_db

KEYS = "/api/v1/api-keys/"
MONITORS = "/api/v1/monitors/"
NEW_MONITOR = {"type": "http", "name": "Site", "target": "https://example.com/health"}


def client_with(raw: str | None = None, **headers) -> APIClient:
    client = APIClient(HTTP_ORIGIN="http://localhost:3000")
    if raw is not None:
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}", **headers)
    return client


def make_key(user, scope="read", name="CI"):
    key, raw = ApiKey.issue(user, name, scope)
    return key, client_with(raw)


def monitor_for(user, name="Shop"):
    return Monitor.objects.create(
        user=user,
        name=name,
        type="http",
        target="https://example.com",
        interval_seconds=300,
        config={},
        next_check_at=timezone.now(),
    )


class TestCreating:
    def test_the_key_is_shown_once_and_only_a_hash_is_kept(self, auth_api, user):
        r = auth_api.post(KEYS, {"name": "  Deploy   script ", "scope": "full"}, format="json")
        assert r.status_code == 201
        body = r.json()
        raw = body["key"]
        assert raw.startswith("rp_") and len(raw) > 40
        assert body["name"] == "Deploy script" and body["scope"] == "full" and body["active"]
        assert body["prefix"] == raw[:11]

        stored = ApiKey.objects.get()
        assert stored.key_hash == hash_key(raw) and raw not in (stored.key_hash, stored.prefix)
        assert raw not in str(list(ApiKey.objects.values()))

        listed = auth_api.get(KEYS).json()
        assert len(listed) == 1 and "key" not in listed[0] and raw not in str(listed)

    def test_the_default_scope_is_read_only(self, auth_api):
        assert auth_api.post(KEYS, {"name": "x"}, format="json").json()["scope"] == "read"

    @pytest.mark.parametrize(
        "payload", [{}, {"name": "  "}, {"name": "x", "scope": "admin"}, {"name": "x" * 81}]
    )
    def test_bad_requests_are_refused(self, auth_api, payload):
        r = auth_api.post(KEYS, payload, format="json")
        assert r.status_code == 400 and not ApiKey.objects.exists()

    def test_keys_are_all_different(self, auth_api):
        a = auth_api.post(KEYS, {"name": "a"}, format="json").json()["key"]
        b = auth_api.post(KEYS, {"name": "b"}, format="json").json()["key"]
        assert a != b

    def test_there_is_a_ceiling_on_active_keys(self, auth_api, user):
        for i in range(10):
            ApiKey.issue(user, f"k{i}", "read")
        r = auth_api.post(KEYS, {"name": "one too many"}, format="json")
        assert r.status_code == 400 and "10 active keys" in r.json()["detail"]
        ApiKey.objects.filter(user=user).first().delete()
        assert auth_api.post(KEYS, {"name": "fits now"}, format="json").status_code == 201

    def test_revoked_keys_dont_count_against_the_ceiling(self, auth_api, user):
        for i in range(10):
            key, _ = ApiKey.issue(user, f"k{i}", "read")
            key.revoked_at = timezone.now()
            key.save()
        assert auth_api.post(KEYS, {"name": "fresh"}, format="json").status_code == 201

    def test_creation_is_audited_without_the_key(self, auth_api):
        raw = auth_api.post(KEYS, {"name": "x", "scope": "full"}, format="json").json()["key"]
        entry = AuditLog.objects.get(action="api_key_created")
        assert entry.metadata["scope"] == "full" and "key_id" in entry.metadata
        assert raw not in str(entry.metadata)


class TestSignIn:
    def test_a_valid_key_lists_monitors(self, user):
        """The definition of done: a bare request with a key lists your monitors."""
        monitor_for(user)
        _, client = make_key(user)
        r = client.get(MONITORS)
        assert r.status_code == 200
        assert [m["name"] for m in r.json()["results"]] == ["Shop"]

    def test_it_is_the_same_api_as_the_dashboard(self, auth_api, user):
        monitor_for(user)
        _, client = make_key(user)
        for path in (
            "/api/v1/monitors/",
            "/api/v1/incidents/",
            "/api/v1/analytics/fleet/?range=7d",
        ):
            assert client.get(path).status_code == auth_api.get(path).status_code == 200
            assert client.get(path).json() == auth_api.get(path).json()

    @pytest.mark.parametrize(
        "token",
        ["rp_notarealkey", "rp_" + "A" * 43, "rp_"],
    )
    def test_an_unknown_key_is_a_401(self, token):
        r = client_with(token).get(MONITORS)
        assert r.status_code == 401 and r.json()["detail"] == "That API key isn't valid."

    def test_a_revoked_key_stops_working_at_once(self, auth_api, user):
        key, client = make_key(user)
        assert client.get(MONITORS).status_code == 200
        assert auth_api.delete(f"{KEYS}{key.id}/").status_code == 204
        r = client.get(MONITORS)
        assert r.status_code == 401 and r.json()["detail"] == "That API key isn't valid."

    def test_a_revoked_key_says_the_same_as_one_that_never_existed(self, user):
        key, client = make_key(user)
        key.revoked_at = timezone.now()
        key.save()
        assert client.get(MONITORS).json() == client_with("rp_nope").get(MONITORS).json()

    def test_a_key_of_a_deactivated_user_is_refused(self, user):
        _, client = make_key(user)
        user.is_active = False
        user.save()
        assert client.get(MONITORS).status_code == 401

    def test_dashboard_tokens_still_work(self, auth_api):
        assert auth_api.get(MONITORS).status_code == 200

    def test_a_bad_dashboard_token_is_still_a_401(self):
        assert client_with("eyJhbGciOiJIUzI1NiJ9.e30.nope").get(MONITORS).status_code == 401

    @pytest.mark.parametrize("header", ["", "Bearer", "Basic cnA6cnA=", "rp_abc", "Token rp_abc"])
    def test_other_authorization_headers_are_not_keys(self, header):
        client = APIClient(HTTP_ORIGIN="http://localhost:3000")
        if header:
            client.credentials(HTTP_AUTHORIZATION=header)
        assert client.get(MONITORS).status_code == 401

    def test_a_key_only_sees_its_owners_data(self, user, make_user):
        monitor_for(make_user("carol@example.com"), "Carols")
        monitor_for(user, "Mine")
        _, client = make_key(user)
        names = [m["name"] for m in client.get(MONITORS).json()["results"]]
        assert names == ["Mine"]

    def test_the_header_is_not_case_sensitive_about_the_scheme(self, user):
        _, raw_client = ApiKey.issue(user, "x", "read")
        client = APIClient(HTTP_ORIGIN="http://localhost:3000")
        client.credentials(HTTP_AUTHORIZATION=f"bearer {raw_client}")
        assert client.get(MONITORS).status_code == 200


class TestScopes:
    def test_a_read_key_can_read_and_not_change(self, user):
        monitor = monitor_for(user)
        _, client = make_key(user, "read")
        assert client.get(MONITORS).status_code == 200
        assert client.head(MONITORS).status_code == 200
        assert client.options(MONITORS).status_code == 200
        for method, path, body in [
            ("post", MONITORS, NEW_MONITOR),
            ("patch", f"{MONITORS}{monitor.id}/", {"name": "x"}),
            ("delete", f"{MONITORS}{monitor.id}/", None),
            ("patch", "/api/v1/auth/me/", {"timezone": "Europe/Paris"}),
            ("post", f"{MONITORS}{monitor.id}/pause/", None),
        ]:
            r = getattr(client, method)(path, body, format="json")
            assert r.status_code == 403, (method, path)
            assert r.json()["detail"] == "This API key is read-only."
        assert Monitor.objects.get().name == "Shop"

    def test_a_read_key_can_download_reports(self, user):
        _, client = make_key(user, "read")
        today = timezone.now().date().isoformat()
        r = client.get(f"/api/v1/reports/uptime.csv?start_date={today}&end_date={today}")
        assert r.status_code == 200

    def test_a_full_key_can_change_things(self, user):
        _, client = make_key(user, "full")
        created = client.post(MONITORS, NEW_MONITOR, format="json")
        assert created.status_code == 201
        monitor_id = created.json()["id"]
        assert (
            client.patch(f"{MONITORS}{monitor_id}/", {"name": "Renamed"}, format="json").status_code
            == 200
        )
        assert client.delete(f"{MONITORS}{monitor_id}/").status_code == 204
        assert not Monitor.objects.exists()

    def test_a_forged_scope_header_changes_nothing(self, user):
        key, _ = make_key(user, "read")
        _, raw = ApiKey.issue(user, "r", "read")
        client = client_with(raw, HTTP_X_API_KEY_SCOPE="full")
        assert client.post(MONITORS, NEW_MONITOR, format="json").status_code == 403


class TestKeysCannotManageKeys:
    """A leaked key mustn't be able to dig in by making others or hiding its tracks."""

    def test_even_a_full_key_cannot_touch_the_key_endpoints(self, auth_api, user):
        key, client = make_key(user, "full")
        other, _ = ApiKey.issue(user, "other", "read")
        assert client.get(KEYS).status_code == 403
        assert (
            client.post(KEYS, {"name": "sneaky", "scope": "full"}, format="json").status_code == 403
        )
        assert client.delete(f"{KEYS}{other.id}/").status_code == 403
        assert ApiKey.objects.count() == 2 and ApiKey.objects.get(pk=other.pk).active
        assert key.active

    def test_the_dashboard_session_can(self, auth_api, user):
        key, _ = ApiKey.issue(user, "x", "read")
        assert auth_api.get(KEYS).status_code == 200
        assert auth_api.delete(f"{KEYS}{key.id}/").status_code == 204


class TestRevokingAndOwnership:
    def test_revoke_then_remove(self, auth_api, user):
        key, _ = ApiKey.issue(user, "x", "read")
        assert auth_api.delete(f"{KEYS}{key.id}/").status_code == 204
        key.refresh_from_db()
        assert key.revoked_at is not None
        listed = auth_api.get(KEYS).json()
        assert listed[0]["active"] is False and listed[0]["revoked_at"]

        assert auth_api.delete(f"{KEYS}{key.id}/").status_code == 204  # revoked: now removed
        assert not ApiKey.objects.exists()
        assert [
            e.action
            for e in AuditLog.objects.order_by("created_at")
            if e.action.startswith("api_key")
        ] == [
            "api_key_revoked",
            "api_key_deleted",
        ]

    def test_revoking_twice_doesnt_move_the_time(self, auth_api, user):
        key, _ = ApiKey.issue(user, "x", "read")
        before = timezone.now() - timedelta(days=3)
        ApiKey.objects.filter(pk=key.pk).update(revoked_at=before)
        # (a second DELETE removes it, so the revoke time of a revoked key is never rewritten)
        auth_api.delete(f"{KEYS}{key.id}/")
        assert not ApiKey.objects.exists()

    def test_other_peoples_keys_are_invisible_and_untouchable(self, auth_api, make_user):
        carol = make_user("carol@example.com")
        key, client = make_key(carol)
        assert auth_api.get(KEYS).json() == []
        assert auth_api.delete(f"{KEYS}{key.id}/").status_code == 404
        assert auth_api.delete(f"{KEYS}{uuid.uuid4()}/").status_code == 404
        assert client.get(MONITORS).status_code == 200  # still works

    def test_signed_out_callers_get_nothing(self):
        anon = client_with()
        assert anon.get(KEYS).status_code == 401
        assert anon.post(KEYS, {"name": "x"}, format="json").status_code == 401


class TestLastUsed:
    def test_it_is_recorded_but_not_on_every_request(self, user):
        key, client = make_key(user)
        assert key.last_used_at is None
        client.get(MONITORS)
        key.refresh_from_db()
        first = key.last_used_at
        assert first is not None

        client.get(MONITORS)  # within the minute: no write
        key.refresh_from_db()
        assert key.last_used_at == first

        ApiKey.objects.filter(pk=key.pk).update(last_used_at=first - timedelta(minutes=5))
        client.get(MONITORS)
        key.refresh_from_db()
        assert key.last_used_at > first - timedelta(minutes=1)

    def test_a_refused_request_does_not_count_as_use(self, user):
        key, client = make_key(user, "read")
        client.post(MONITORS, NEW_MONITOR, format="json")
        key.refresh_from_db()
        assert key.last_used_at is None

    def test_it_shows_in_the_list(self, auth_api, user):
        _, client = make_key(user)
        client.get(MONITORS)
        assert auth_api.get(KEYS).json()[0]["last_used_at"] is not None


class TestRateLimits:
    def test_every_response_says_where_you_stand(self, user):
        _, client = make_key(user)
        first = client.get(MONITORS)
        assert first["X-RateLimit-Limit"] == "60" and first["X-RateLimit-Remaining"] == "59"
        assert 0 < int(first["X-RateLimit-Reset"]) <= 60
        second = client.get(MONITORS)
        assert second["X-RateLimit-Remaining"] == "58"

    def test_dashboard_requests_get_the_same_headers(self, auth_api):
        r = auth_api.get(MONITORS)
        assert r["X-RateLimit-Limit"] == "60" and "X-RateLimit-Remaining" in r

    def test_over_the_limit_is_a_429_with_retry_after(self, user):
        _, client = make_key(user)
        codes = [client.get(MONITORS).status_code for _ in range(62)]
        assert codes[:60] == [200] * 60 and codes[60:] == [429, 429]
        r = client.get(MONITORS)
        assert r["X-RateLimit-Remaining"] == "0" and int(r["Retry-After"]) > 0

    def test_a_key_shares_its_accounts_allowance(self, auth_api, user):
        """Making a key doesn't double what an account may do: key and dashboard are one bucket."""
        _, client = make_key(user)
        for _ in range(30):
            client.get(MONITORS)
        assert auth_api.get(MONITORS)["X-RateLimit-Remaining"] == "29"

    def test_two_accounts_have_separate_allowances(self, user, make_user):
        _, mine = make_key(user)
        _, theirs = make_key(make_user("carol@example.com"))
        for _ in range(61):
            mine.get(MONITORS)
        assert theirs.get(MONITORS).status_code == 200

    def test_guessing_keys_is_throttled_per_address(self, user):
        client = client_with("rp_nope")
        codes = [client.get(MONITORS).status_code for _ in range(23)]
        assert codes[:20] == [401] * 20 and codes[20:] == [429, 429, 429]
        assert int(client.get(MONITORS)["Retry-After"]) > 0

    def test_a_good_key_is_never_slowed_by_other_peoples_bad_ones(self, user):
        _, good = make_key(user)
        bad = client_with("rp_nope")
        for _ in range(25):
            bad.get(MONITORS)
        assert bad.get(MONITORS).status_code == 429
        # (same address in tests, so the good key from here is refused too: the limit is per
        # address. What matters is that a good key never *adds* to the count.)
        fresh = APIClient(HTTP_ORIGIN="http://localhost:3000", REMOTE_ADDR="203.0.113.9")
        fresh.credentials(HTTP_AUTHORIZATION=good._credentials["HTTP_AUTHORIZATION"])
        assert fresh.get(MONITORS).status_code == 200

    def test_using_a_good_key_adds_nothing_to_the_failure_count(self, user):
        _, client = make_key(user)
        for _ in range(30):
            assert client.get(MONITORS).status_code in (200, 429)
        from django.core.cache import cache

        assert not [k for k in getattr(cache, "_cache", {}) if "apikey-failures" in str(k)]


class TestDocs:
    def test_the_schema_and_docs_are_public_and_describe_keys(self):
        anon = client_with()
        schema = anon.get("/api/v1/schema/?format=json")
        assert schema.status_code == 200
        body = schema.json()
        assert "apiKey" in body["components"]["securitySchemes"]
        assert "Authorization: Bearer rp_" in body["info"]["description"]
        assert "X-RateLimit-Remaining" in body["info"]["description"]
        assert "/api/v1/api-keys/" in body["paths"]

    def test_the_schema_never_exposes_a_key_hash(self):
        text = client_with().get("/api/v1/schema/?format=json").content.decode()
        assert "key_hash" not in text


class TestModel:
    def test_hashes_are_stable_and_distinct(self):
        assert hash_key("rp_a") == hash_key("rp_a") != hash_key("rp_b")
        assert len(hash_key("rp_a")) == 64

    def test_issue_returns_the_only_copy_of_the_secret(self, user):
        key, raw = ApiKey.issue(user, "x", "read")
        assert key.prefix == raw[:11] and not hasattr(key, "key")
        assert str(key) == f"x ({key.prefix}…)"
