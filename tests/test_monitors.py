from datetime import timedelta

import pytest
from django.utils import timezone

from accounts.models import AuditLog
from monitoring.models import Monitor

URL = "/api/v1/monitors/"

pytestmark = pytest.mark.django_db

# One valid payload per type (also the "all 8 types can be created" table).
VALID = {
    "http": {"type": "http", "name": "Site", "target": "https://example.com/health"},
    "keyword": {
        "type": "keyword",
        "name": "Keyword",
        "target": "https://example.com",
        "config": {"keyword": "Welcome"},
    },
    "ping": {"type": "ping", "name": "Ping", "target": "example.com"},
    "port": {"type": "port", "name": "Port", "target": "example.com", "config": {"port": 5432}},
    "ssl": {"type": "ssl", "name": "SSL", "target": "example.com"},
    "domain": {"type": "domain", "name": "Domain", "target": "example.com"},
    "cron": {"type": "cron", "name": "Nightly backup"},
    "dns": {"type": "dns", "name": "DNS", "target": "example.com"},
}


def create(client, **overrides):
    return client.post(URL, {**VALID["http"], **overrides}, format="json")


class TestAuthentication:
    def test_every_endpoint_requires_sign_in(self, api, auth_api):
        monitor = create(auth_api).json()
        anon = api.__class__(HTTP_ORIGIN="http://localhost:3000")
        assert anon.get(URL).status_code == 401
        assert anon.post(URL, VALID["http"], format="json").status_code == 401
        assert anon.get(f"{URL}{monitor['id']}/").status_code == 401
        assert anon.post(f"{URL}{monitor['id']}/pause/").status_code == 401
        assert anon.delete(f"{URL}{monitor['id']}/").status_code == 401


class TestCreateEveryType:
    @pytest.mark.parametrize("kind", list(VALID))
    def test_creates_the_monitor_pending_with_a_real_next_check(self, auth_api, user, kind):
        before = timezone.now()
        resp = auth_api.post(URL, VALID[kind], format="json")

        assert resp.status_code == 201, resp.json()
        body = resp.json()
        assert body["type"] == kind
        assert body["status"] == "pending"
        monitor = Monitor.objects.get(id=body["id"])
        assert monitor.user == user
        assert monitor.next_check_at is not None and monitor.next_check_at >= before
        assert monitor.last_check_at is None

    def test_http_defaults_are_filled_in(self, auth_api):
        body = create(auth_api).json()
        assert body["interval_seconds"] == 300
        assert body["config"] == {
            "method": "GET",
            "headers": {},
            "body": "",
            "expected_status": ["2xx", "3xx"],
            "timeout_seconds": 10,
            "follow_redirects": True,
        }

    def test_slow_types_default_to_daily(self, auth_api):
        assert auth_api.post(URL, VALID["ssl"], format="json").json()["interval_seconds"] == 86400
        assert (
            auth_api.post(URL, VALID["domain"], format="json").json()["interval_seconds"] == 86400
        )

    def test_ping_defaults_to_port_443(self, auth_api):
        assert auth_api.post(URL, VALID["ping"], format="json").json()["config"] == {"port": 443}

    def test_dns_defaults_to_a_records(self, auth_api):
        body = auth_api.post(URL, VALID["dns"], format="json").json()
        assert body["config"] == {"record_types": ["A"]}

    def test_the_target_is_normalised(self, auth_api):
        body = auth_api.post(
            URL, {**VALID["ping"], "target": "  Example.COM. "}, format="json"
        ).json()
        assert body["target"] == "example.com"


class TestHeartbeat:
    def test_gets_a_secret_ping_url_and_is_not_due_until_the_grace_period_ends(self, auth_api):
        before = timezone.now()
        body = auth_api.post(
            URL,
            {**VALID["cron"], "interval_seconds": 3600, "config": {"grace_period_seconds": 600}},
            format="json",
        ).json()

        assert body["heartbeat_url"].startswith("http://localhost:8000/api/v1/heartbeat/")
        assert body["heartbeat_url"].endswith("/")
        monitor = Monitor.objects.get(id=body["id"])
        assert len(monitor.heartbeat_token) >= 30
        assert monitor.next_check_at >= before + timedelta(seconds=4200) - timedelta(seconds=5)

    def test_every_heartbeat_gets_a_different_url(self, auth_api):
        a = auth_api.post(URL, VALID["cron"], format="json").json()["heartbeat_url"]
        b = auth_api.post(URL, VALID["cron"], format="json").json()["heartbeat_url"]
        assert a != b

    def test_other_types_have_no_heartbeat_url(self, auth_api):
        assert create(auth_api).json()["heartbeat_url"] is None

    def test_takes_no_target(self, auth_api):
        resp = auth_api.post(URL, {**VALID["cron"], "target": "https://example.com"}, format="json")
        assert resp.status_code == 400
        assert "target" in resp.json()["field_errors"]

    def test_the_token_is_not_editable(self, auth_api):
        monitor = auth_api.post(URL, VALID["cron"], format="json").json()
        auth_api.patch(
            f"{URL}{monitor['id']}/",
            {"heartbeat_url": "http://x/", "name": "Renamed"},
            format="json",
        )
        assert (
            auth_api.get(f"{URL}{monitor['id']}/").json()["heartbeat_url"]
            == monitor["heartbeat_url"]
        )


class TestValidation:
    @pytest.mark.parametrize(
        "payload,field",
        [
            ({"type": "nope"}, "type"),
            ({"name": ""}, "name"),
            ({"name": "   "}, "name"),
            ({"name": "x" * 121}, "name"),
            ({"target": ""}, "target"),
            ({"target": "ftp://example.com"}, "target"),
            ({"interval_seconds": 60}, "interval_seconds"),
            ({"interval_seconds": 301}, "interval_seconds"),
            ({"config": {"timeout_seconds": 0}}, "config"),
            ({"config": {"timeout_seconds": 31}}, "config"),
            ({"config": {"method": "TRACE"}}, "config"),
            ({"config": {"expected_status": []}}, "config"),
            ({"config": {"expected_status": ["abc"]}}, "config"),
            ({"config": {"expected_status": ["600"]}}, "config"),
            ({"config": {"unknown_setting": 1}}, "config"),
            ({"config": {"headers": {"Host": "evil.com"}}}, "config"),
            ({"config": {"headers": {"X-A": "line\nbreak"}}}, "config"),
            ({"config": {"headers": {"bad name": "v"}}}, "config"),
            ({"config": {"headers": {f"X-{i}": "v" for i in range(11)}}}, "config"),
            ({"config": {"body": "data"}}, "config"),  # GET can't carry a body
        ],
    )
    def test_http_rejects(self, auth_api, payload, field):
        resp = create(auth_api, **payload)
        assert resp.status_code == 400
        errors = resp.json()["field_errors"]
        assert any(k == field or k.startswith(f"{field}.") for k in errors), errors
        assert not Monitor.objects.exists()

    def test_a_post_may_carry_a_body(self, auth_api):
        resp = create(auth_api, config={"method": "POST", "body": '{"ping": true}'})
        assert resp.status_code == 201

    def test_keyword_needs_a_real_keyword(self, auth_api):
        for config in ({}, {"keyword": ""}, {"keyword": "   "}):
            resp = auth_api.post(URL, {**VALID["keyword"], "config": config}, format="json")
            assert resp.status_code == 400
        ok = auth_api.post(
            URL, {**VALID["keyword"], "config": {"keyword": "x", "exists": False}}, format="json"
        )
        assert ok.status_code == 201 and ok.json()["config"]["exists"] is False

    def test_port_monitor_requires_a_valid_port(self, auth_api):
        for config in ({}, {"port": 0}, {"port": 65536}, {"port": "abc"}):
            resp = auth_api.post(URL, {**VALID["port"], "config": config}, format="json")
            assert resp.status_code == 400, config

    def test_ssl_and_domain_warning_windows_are_bounded(self, auth_api):
        for kind, bad in (("ssl", 0), ("ssl", 91), ("domain", 0), ("domain", 366)):
            resp = auth_api.post(URL, {**VALID[kind], "config": {"warn_days": bad}}, format="json")
            assert resp.status_code == 400

    def test_slow_types_refuse_fast_polling(self, auth_api):
        for kind in ("ssl", "domain"):
            resp = auth_api.post(URL, {**VALID[kind], "interval_seconds": 300}, format="json")
            assert resp.status_code == 400

    def test_heartbeat_grace_is_bounded(self, auth_api):
        for grace in (59, 86401):
            resp = auth_api.post(
                URL, {**VALID["cron"], "config": {"grace_period_seconds": grace}}, format="json"
            )
            assert resp.status_code == 400

    def test_dns_record_types(self, auth_api):
        ok = auth_api.post(
            URL, {**VALID["dns"], "config": {"record_types": ["A", "MX", "TXT"]}}, format="json"
        )
        assert ok.status_code == 201
        for bad in ([], ["SRV"], ["A", "A"]):
            resp = auth_api.post(
                URL, {**VALID["dns"], "config": {"record_types": bad}}, format="json"
            )
            assert resp.status_code == 400, bad

    def test_dns_and_domain_monitors_reject_ip_addresses(self, auth_api):
        for kind in ("dns", "domain"):
            resp = auth_api.post(URL, {**VALID[kind], "target": "1.2.3.4"}, format="json")
            assert resp.status_code == 400

    def test_errors_use_the_standard_shape(self, auth_api):
        body = create(auth_api, name="").json()
        assert set(body) == {"detail", "field_errors"}


class TestSsrfAtCreation:
    @pytest.mark.parametrize(
        "kind,target",
        [
            ("http", "http://169.254.169.254/latest/meta-data/"),
            ("http", "http://localhost:5432/"),
            ("http", "http://127.0.0.1/"),
            ("http", "http://[::1]/"),
            ("http", "http://10.0.0.5/admin"),
            ("keyword", "http://192.168.1.1/"),
            ("ping", "127.0.0.1"),
            ("ping", "localhost"),
            ("port", "169.254.169.254"),
            ("port", "10.0.0.1"),
            ("ssl", "internal.corp"),
        ],
    )
    def test_internal_targets_are_refused(self, auth_api, kind, target):
        payload = {**VALID[kind], "target": target}
        resp = auth_api.post(URL, payload, format="json")
        assert resp.status_code == 400
        assert "target" in resp.json()["field_errors"]
        assert not Monitor.objects.exists()

    def test_a_name_that_resolves_to_a_private_ip_is_refused(self, auth_api, dns):
        dns["looks-public.example.com"] = ["10.1.2.3"]
        resp = create(auth_api, target="https://looks-public.example.com/")
        assert resp.status_code == 400
        assert "private or reserved" in resp.json()["field_errors"]["target"][0]

    def test_editing_a_target_into_a_private_address_is_refused_too(self, auth_api):
        monitor = create(auth_api).json()
        resp = auth_api.patch(
            f"{URL}{monitor['id']}/", {"target": "http://169.254.169.254/"}, format="json"
        )
        assert resp.status_code == 400
        assert Monitor.objects.get().target == "https://example.com/health"


class TestListing:
    @pytest.fixture
    def many(self, auth_api):
        auth_api.post(
            URL,
            {**VALID["http"], "name": "Alpha", "target": "https://alpha.example.com"},
            format="json",
        )
        auth_api.post(
            URL, {**VALID["ping"], "name": "Bravo", "target": "bravo.example.com"}, format="json"
        )
        paused = auth_api.post(URL, {**VALID["dns"], "name": "Charlie"}, format="json").json()
        auth_api.post(f"{URL}{paused['id']}/pause/")
        return auth_api

    def names(self, resp):
        return [m["name"] for m in resp.json()["results"]]

    def test_is_paginated_and_newest_first_by_default(self, many):
        resp = many.get(URL)
        assert resp.json()["count"] == 3
        assert self.names(resp) == ["Charlie", "Bravo", "Alpha"]

    def test_filters(self, many):
        assert self.names(many.get(URL, {"status": "paused"})) == ["Charlie"]
        assert self.names(many.get(URL, {"type": "ping"})) == ["Bravo"]
        assert self.names(many.get(URL, {"status": "pending", "type": "http"})) == ["Alpha"]

    def test_search_matches_name_and_target(self, many):
        assert self.names(many.get(URL, {"q": "alp"})) == ["Alpha"]
        assert self.names(many.get(URL, {"q": "BRAVO.example"})) == ["Bravo"]
        assert self.names(many.get(URL, {"q": "nothing-matches"})) == []

    def test_sorting(self, many):
        assert self.names(many.get(URL, {"sort": "name"})) == ["Alpha", "Bravo", "Charlie"]
        assert self.names(many.get(URL, {"sort": "-name"})) == ["Charlie", "Bravo", "Alpha"]
        assert self.names(many.get(URL, {"sort": "garbage"})) == ["Charlie", "Bravo", "Alpha"]

    def test_page_size(self, many):
        resp = many.get(URL, {"page_size": 2}).json()
        assert len(resp["results"]) == 2 and resp["next"]


class TestOwnership:
    def test_users_only_see_and_touch_their_own_monitors(self, auth_api, make_user, sign_in, api):
        mine = create(auth_api).json()

        other = make_user("mallory@example.com")
        client = api.__class__(HTTP_ORIGIN="http://localhost:3000")
        token = sign_in(client, other.email).json()["access_token"]
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

        assert client.get(URL).json()["count"] == 0
        assert client.get(f"{URL}{mine['id']}/").status_code == 404
        assert (
            client.patch(f"{URL}{mine['id']}/", {"name": "pwned"}, format="json").status_code == 404
        )
        assert client.post(f"{URL}{mine['id']}/pause/").status_code == 404
        assert client.post(f"{URL}{mine['id']}/resume/").status_code == 404
        assert client.delete(f"{URL}{mine['id']}/").status_code == 404
        assert Monitor.objects.get().name == "Site"


class TestRetrieveAndUpdate:
    def test_retrieve(self, auth_api):
        monitor = create(auth_api).json()
        detail = auth_api.get(f"{URL}{monitor['id']}/").json()
        summary = detail.pop("summary")  # only the detail view carries the 24h summary
        assert detail == monitor
        assert summary["checks"] == 0 and summary["uptime_percent"] is None

    def test_unknown_id(self, auth_api):
        assert auth_api.get(f"{URL}00000000-0000-4000-8000-000000000000/").status_code == 404
        assert auth_api.get(f"{URL}not-a-uuid/").status_code == 404

    def test_put_is_not_offered(self, auth_api):
        monitor = create(auth_api).json()
        assert (
            auth_api.put(f"{URL}{monitor['id']}/", VALID["http"], format="json").status_code == 405
        )

    def test_renaming_leaves_the_status_and_schedule_alone(self, auth_api):
        monitor = create(auth_api).json()
        Monitor.objects.update(status="up", next_check_at=timezone.now() + timedelta(minutes=3))
        stamp = Monitor.objects.get().next_check_at

        resp = auth_api.patch(f"{URL}{monitor['id']}/", {"name": "Renamed"}, format="json")

        assert resp.status_code == 200 and resp.json()["name"] == "Renamed"
        fresh = Monitor.objects.get()
        assert fresh.status == "up" and fresh.next_check_at == stamp

    def test_changing_the_target_resets_to_pending_and_checks_soon(self, auth_api):
        monitor = create(auth_api).json()
        Monitor.objects.update(status="up", next_check_at=timezone.now() + timedelta(minutes=4))

        resp = auth_api.patch(
            f"{URL}{monitor['id']}/", {"target": "https://other.example.com"}, format="json"
        )

        assert resp.status_code == 200
        fresh = Monitor.objects.get()
        assert fresh.status == "pending"
        assert fresh.next_check_at <= timezone.now()

    def test_changing_only_the_interval_keeps_the_status(self, auth_api):
        monitor = create(auth_api).json()
        Monitor.objects.update(status="down")
        auth_api.patch(f"{URL}{monitor['id']}/", {"interval_seconds": 900}, format="json")
        fresh = Monitor.objects.get()
        assert fresh.status == "down" and fresh.interval_seconds == 900

    def test_config_is_replaced_and_revalidated(self, auth_api):
        monitor = create(auth_api).json()
        resp = auth_api.patch(
            f"{URL}{monitor['id']}/", {"config": {"timeout_seconds": 99}}, format="json"
        )
        assert resp.status_code == 400
        resp = auth_api.patch(
            f"{URL}{monitor['id']}/", {"config": {"timeout_seconds": 20}}, format="json"
        )
        assert resp.json()["config"]["timeout_seconds"] == 20
        assert resp.json()["config"]["method"] == "GET"  # defaults refilled

    def test_the_type_cannot_change(self, auth_api):
        monitor = create(auth_api).json()
        resp = auth_api.patch(f"{URL}{monitor['id']}/", {"type": "ping"}, format="json")
        assert resp.status_code == 400
        assert Monitor.objects.get().type == "http"

    def test_editing_a_paused_monitor_keeps_it_paused_and_never_due(self, auth_api):
        monitor = create(auth_api).json()
        auth_api.post(f"{URL}{monitor['id']}/pause/")
        auth_api.patch(
            f"{URL}{monitor['id']}/", {"target": "https://new.example.com"}, format="json"
        )
        fresh = Monitor.objects.get()
        assert fresh.status == "paused" and fresh.next_check_at is None

    def test_read_only_fields_cannot_be_set(self, auth_api):
        monitor = create(auth_api).json()
        auth_api.patch(
            f"{URL}{monitor['id']}/",
            {"status": "up", "last_check_at": "2020-01-01T00:00:00Z", "id": "x"},
            format="json",
        )
        fresh = Monitor.objects.get()
        assert fresh.status == "pending" and fresh.last_check_at is None


class TestPauseResumeDelete:
    def test_pause_then_resume(self, auth_api):
        monitor = create(auth_api).json()

        paused = auth_api.post(f"{URL}{monitor['id']}/pause/").json()
        assert paused["status"] == "paused" and paused["next_check_at"] is None

        resumed = auth_api.post(f"{URL}{monitor['id']}/resume/").json()
        assert resumed["status"] == "pending" and resumed["next_check_at"] is not None

    def test_both_are_idempotent(self, auth_api):
        monitor = create(auth_api).json()
        assert auth_api.post(f"{URL}{monitor['id']}/resume/").json()["status"] == "pending"
        auth_api.post(f"{URL}{monitor['id']}/pause/")
        assert auth_api.post(f"{URL}{monitor['id']}/pause/").json()["status"] == "paused"

    def test_resuming_a_heartbeat_restarts_its_clock(self, auth_api):
        monitor = auth_api.post(
            URL, {**VALID["cron"], "interval_seconds": 3600}, format="json"
        ).json()
        auth_api.post(f"{URL}{monitor['id']}/pause/")
        resumed = auth_api.post(f"{URL}{monitor['id']}/resume/").json()
        due = Monitor.objects.get(id=resumed["id"]).next_check_at
        assert due > timezone.now() + timedelta(minutes=59)

    def test_delete_removes_it_and_is_audited(self, auth_api, user):
        monitor = create(auth_api).json()
        assert auth_api.delete(f"{URL}{monitor['id']}/").status_code == 204
        assert not Monitor.objects.exists()
        assert auth_api.get(f"{URL}{monitor['id']}/").status_code == 404
        log = AuditLog.objects.get(action="monitor_deleted")
        assert log.user == user and log.metadata["monitor_id"] == monitor["id"]

    def test_create_is_audited(self, auth_api, user):
        monitor = create(auth_api).json()
        assert AuditLog.objects.filter(
            action="monitor_created", user=user, metadata__monitor_id=monitor["id"]
        ).exists()


class TestQuota:
    def test_users_are_capped(self, auth_api, settings):
        settings.MAX_MONITORS_PER_USER = 2
        assert create(auth_api, name="a").status_code == 201
        assert create(auth_api, name="b").status_code == 201
        resp = create(auth_api, name="c")
        assert resp.status_code == 403
        assert "limit of 2 monitors" in resp.json()["detail"]

    def test_the_cap_is_per_user_and_deleting_frees_a_slot(self, auth_api, settings):
        settings.MAX_MONITORS_PER_USER = 1
        first = create(auth_api, name="a").json()
        assert create(auth_api, name="b").status_code == 403
        auth_api.delete(f"{URL}{first['id']}/")
        assert create(auth_api, name="b").status_code == 201


class TestSchemaAndDatabaseRules:
    def test_openapi_documents_the_monitor_endpoints_and_typed_config(self, client):
        schema = client.get("/api/v1/schema/", HTTP_ACCEPT="application/json").json()
        assert "/api/v1/monitors/" in schema["paths"]
        assert "/api/v1/monitors/{id}/pause/" in schema["paths"]
        config = schema["components"]["schemas"]["MonitorConfig"]
        assert len(config["oneOf"]) == 8

    def test_the_database_refuses_a_paused_monitor_that_is_due(self, user):
        from django.db import IntegrityError, transaction

        with pytest.raises(IntegrityError), transaction.atomic():
            Monitor.objects.create(
                user=user,
                type="http",
                name="x",
                target="https://example.com",
                interval_seconds=300,
                status="paused",
                next_check_at=timezone.now(),
            )

    def test_the_database_refuses_a_heartbeat_token_on_other_types(self, user):
        from django.db import IntegrityError, transaction

        with pytest.raises(IntegrityError), transaction.atomic():
            Monitor.objects.create(
                user=user,
                type="http",
                name="x",
                target="https://example.com",
                interval_seconds=300,
                heartbeat_token="abc",
            )


class TestErrorShape:
    """The frontend attaches errors to inputs by their dotted path."""

    def test_config_errors_are_flattened(self, auth_api):
        body = create(auth_api, config={"timeout_seconds": 99, "expected_status": ["abc"]}).json()
        errors = body["field_errors"]
        assert "config.timeout_seconds" in errors
        assert "config.expected_status" in errors
        assert all(
            isinstance(v, list) and all(isinstance(m, str) for m in v) for v in errors.values()
        )

    def test_unknown_settings_are_named(self, auth_api):
        errors = create(auth_api, config={"bogus": 1}).json()["field_errors"]
        assert errors["config.bogus"] == ["Unknown setting for this monitor type."]

    def test_header_errors_point_at_the_headers_field(self, auth_api):
        body = create(auth_api, config={"headers": {"Host": "x"}}).json()
        assert body["field_errors"]["config.headers"] == ["The Host header can't be set."]

    def test_top_level_errors_are_unchanged(self, auth_api):
        body = create(auth_api, name="").json()
        assert "name" in body["field_errors"]
