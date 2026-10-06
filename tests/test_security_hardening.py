"""Phase 16 findings, each pinned by a test so it can't come back:

* names are one line (a newline in a name made alert emails fail to send)
* odd spellings of private addresses are refused by the validator itself, not by luck of DNS
* provider status pages can't redirect the poller to an internal address
* a client can't set fields it has no business setting (mass assignment)
* forged and tampered tokens are refused
"""

import base64
import json
import uuid

import httpx
import pytest
from django.core import mail
from django.utils import timezone

from accounts.models import User
from apikeys.models import ApiKey
from incidents.models import Incident
from monitoring import target_validation as tv
from monitoring.models import AlertContact, Monitor
from notifications.channels import CHANNELS
from providers import poller
from providers.poller import new_client as real_new_client  # (conftest swaps the module's copy)
from statuspages.models import StatusPage

pytestmark = pytest.mark.django_db

NEWLINE_NAME = "Shop\r\nBcc: attacker@example.com"


def b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


# --- one-line names ---------------------------------------------------------------------------


class TestNamesAreOneLine:
    def test_a_monitor_name_cannot_hold_a_newline(self, auth_api):
        r = auth_api.post(
            "/api/v1/monitors/",
            {"type": "http", "name": NEWLINE_NAME, "target": "https://example.com"},
            format="json",
        )
        assert r.status_code == 201
        assert r.json()["name"] == "Shop Bcc: attacker@example.com"

    def test_nor_can_a_contact_a_connection_or_a_maintenance_window(self, auth_api, user):
        monitor = Monitor.objects.create(
            user=user,
            name="m",
            type="http",
            target="https://example.com",
            interval_seconds=300,
            config={},
            next_check_at=timezone.now(),
        )
        contact = auth_api.post(
            "/api/v1/alert-contacts/",
            {"name": "A\nB", "channel": "slack", "config": {
                "webhook_url": "https://hooks.slack.com/services/T0/B0/abcdefghijkl"}},
            format="json",
        )  # fmt: skip
        source = auth_api.post(
            "/api/v1/deploy-sources/", {"name": "A\nB", "type": "generic"}, format="json"
        )
        start = timezone.now() + timezone.timedelta(days=1)
        window = auth_api.post(
            "/api/v1/maintenance-windows/",
            {
                "name": "A\nB",
                "starts_at": start.isoformat(),
                "ends_at": (start + timezone.timedelta(hours=1)).isoformat(),
                "monitor_ids": [str(monitor.id)],
            },
            format="json",
        )
        for response in (contact, source, window):
            assert response.status_code == 201, response.json()
            assert response.json()["name"] == "A B"

    def test_an_alert_email_goes_out_even_for_a_name_stored_before_this_rule(self):
        """Old data may still hold a newline; the subject must not make Django refuse to send."""
        mail.outbox.clear()
        payload = {
            "severity": "down",
            "title": f"{NEWLINE_NAME} is down",
            "summary": "It stopped responding.",
            "monitor": {"target": "https://example.com", "id": "x"},
            "incident": None,
            "url": "https://app.example.com/incidents/1",
        }
        CHANNELS["email"].send({"address": "owner@example.com"}, payload, 1)
        assert len(mail.outbox) == 1
        subject = mail.outbox[0].subject
        assert "\n" not in subject and "\r" not in subject and mail.outbox[0].bcc == []
        assert subject == "[DOWN] Shop Bcc: attacker@example.com is down"


# --- the SSRF validator, without leaning on DNS ----------------------------------------------


class TestAddressSpellings:
    """Before: `127.1`, `0x7f.0.0.1` and `0177.0.0.1` were accepted as host *names* and only
    refused because the system resolver happened to turn them into 127.0.0.1. Now the validator
    refuses them itself. (No DNS in these: the rule is about spelling.)"""

    @pytest.mark.parametrize(
        "host",
        [
            "127.1",
            "0x7f.0.0.1",
            "0177.0.0.1",
            "2130706433",
            "017700000001",
            "10.1",
            "1.2.3.4.5",
            "example.123",
            "a.0x7f",
            "127。0。0。1",  # ideographic full stops, which IDNA turns into dots
        ],
    )
    def test_numeric_looking_names_are_refused(self, host):
        for check in (tv.validate_public_host, tv.validate_domain_name):
            with pytest.raises(tv.TargetRejected):
                check(host)
        with pytest.raises(tv.TargetRejected):
            tv.validate_http_url(f"http://{host}/")

    @pytest.mark.parametrize(
        "ip",
        ["::7f00:1", "::127.0.0.1", "::a9fe:a9fe", "::ffff:0:7f00:1", "fec0::1", "5f00::1"],
    )
    def test_ipv6_spellings_of_private_addresses_are_forbidden(self, ip):
        import ipaddress

        assert tv.ip_is_forbidden(ipaddress.ip_address(ip))

    def test_the_deprecated_6to4_relay_range_is_forbidden_but_ordinary_addresses_are_not(self):
        import ipaddress

        assert tv.ip_is_forbidden(ipaddress.ip_address("192.88.99.1"))
        for fine in ("93.184.216.34", "1.1.1.1", "2606:4700:4700::1111"):
            assert not tv.ip_is_forbidden(ipaddress.ip_address(fine))

    @pytest.mark.parametrize(
        "url",
        [
            "http://example.com\\@127.0.0.1/",  # a backslash some parsers treat as a slash
            "http://127.0.0.1\\.example.com/",
            "http://example.com%00.127.0.0.1/",
            "http://127.0.0.1%2f@example.com/",
            "http://[::1]\\@example.com/",
            "http://example.com:80@127.0.0.1/",
            "http://example.com#@127.0.0.1/",
        ],
    )
    def test_urls_that_different_parsers_read_differently_are_refused_or_safe(self, url):
        try:
            clean = tv.validate_http_url(url)
        except tv.TargetRejected:
            return
        # If it is accepted, the host the validator judged must be the host that gets contacted.
        from urllib.parse import urlsplit

        assert urlsplit(clean).hostname == "example.com"


# --- provider status pages -------------------------------------------------------------------


class TestProviderRedirects:
    def client(self, handler, dns, **table):
        dns.update(table)
        return real_new_client(transport=httpx.MockTransport(handler))

    def test_a_redirect_to_an_internal_address_is_refused(self, dns):
        calls = []

        def handler(request):
            calls.append(str(request.url))
            if request.url.host == "status.example.com":
                return httpx.Response(302, headers={"location": "http://metadata.internal/x"})
            return httpx.Response(200, json={"leaked": True})

        client = self.client(handler, dns, **{"metadata.internal": ["169.254.169.254"]})
        with pytest.raises(poller.FeedError):
            poller.fetch_json(client, "https://status.example.com/api/v2/summary.json")
        assert calls == ["https://status.example.com/api/v2/summary.json"]  # never asked the hop

    def test_a_redirect_to_a_public_host_is_followed(self, dns):
        def handler(request):
            if request.url.host == "old.example.com":
                return httpx.Response(301, headers={"location": "https://new.example.com/a.json"})
            return httpx.Response(200, json={"ok": True})

        client = self.client(handler, dns)
        assert poller.fetch_json(client, "https://old.example.com/a.json") == {"ok": True}

    def test_a_public_name_that_resolves_to_a_private_address_is_refused_outright(self, dns):
        client = self.client(
            lambda r: httpx.Response(200, json={}), dns, **{"rebind.example.com": ["10.0.0.5"]}
        )
        with pytest.raises(poller.FeedError):
            poller.fetch_json(client, "https://rebind.example.com/a.json")

    def test_redirect_loops_end(self, dns):
        def handler(request):
            return httpx.Response(302, headers={"location": str(request.url)})

        with pytest.raises(poller.FeedError):
            poller.fetch_json(self.client(handler, dns), "https://loop.example.com/a.json")

    def test_every_catalogue_feed_is_https_on_a_public_looking_host(self):
        from providers.catalog import CATALOG

        for entry in CATALOG:
            assert entry.feed.startswith("https://"), entry.slug
            assert tv.validate_http_url(entry.feed)
            assert entry.page.startswith("https://"), entry.slug


# --- fields a client must not be able to set -------------------------------------------------


class TestMassAssignment:
    def test_a_monitor_cannot_be_given_to_someone_else_or_made_to_look_healthy(
        self, auth_api, user, make_user
    ):
        other = make_user("carol@example.com")
        forced = str(uuid.uuid4())
        r = auth_api.post(
            "/api/v1/monitors/",
            {
                "type": "http",
                "name": "Mine",
                "target": "https://example.com",
                "user": str(other.id),
                "user_id": str(other.id),
                "id": forced,
                "status": "up",
                "next_check_at": "2099-01-01T00:00:00Z",
                "heartbeat_token": "stolen",
            },
            format="json",
        )
        assert r.status_code == 201
        monitor = Monitor.objects.get()
        assert monitor.user_id == user.id and str(monitor.id) != forced
        assert monitor.status == "pending" and monitor.heartbeat_token is None
        assert monitor.next_check_at.year != 2099

    def test_a_contact_cannot_declare_itself_verified(self, auth_api):
        r = auth_api.post(
            "/api/v1/alert-contacts/",
            {
                "name": "Mail",
                "channel": "email",
                "config": {"address": "someone-else@example.com"},
                "verified_at": "2026-01-01T00:00:00Z",
                "verified": True,
                "enabled": True,
            },
            format="json",
        )
        assert r.status_code == 201
        contact = AlertContact.objects.get()
        assert contact.verified_at is None  # an address nobody proved is never "verified"

    def test_a_status_page_cannot_be_created_as_someone_elses_or_with_a_ready_made_hash(
        self, auth_api, user, make_user
    ):
        other = make_user("carol@example.com")
        r = auth_api.post(
            "/api/v1/status-pages/",
            {
                "name": "Mine",
                "slug": "mine",
                "user": str(other.id),
                "password_hash": "pbkdf2_sha256$1$x$y",
                "is_public": True,
            },
            format="json",
        )
        assert r.status_code == 201
        page = StatusPage.objects.get()
        assert page.user_id == user.id and page.password_hash == ""

    def test_an_api_key_cannot_be_created_with_its_own_hash_or_as_already_used(self, auth_api):
        r = auth_api.post(
            "/api/v1/api-keys/",
            {
                "name": "k",
                "key_hash": "0" * 64,
                "prefix": "rp_fake",
                "last_used_at": "2026-01-01T00:00:00Z",
                "revoked_at": None,
                "user": "x",
            },
            format="json",
        )
        assert r.status_code == 201
        key = ApiKey.objects.get()
        assert key.key_hash != "0" * 64 and key.prefix != "rp_fake" and key.last_used_at is None
        assert key.key_hash == __import__("apikeys.models", fromlist=["hash_key"]).hash_key(
            r.json()["key"]
        )

    def test_an_incident_can_only_be_excluded_never_rewritten(self, auth_api, user):
        monitor = Monitor.objects.create(
            user=user,
            name="m",
            type="http",
            target="https://example.com",
            interval_seconds=300,
            config={},
            next_check_at=timezone.now(),
        )
        incident = Incident.objects.create(
            monitor=monitor, started_at=timezone.now(), reason="timeout"
        )
        r = auth_api.patch(
            f"/api/v1/incidents/{incident.id}/",
            {
                "excluded_from_reports": True,
                "ended_at": "2030-01-01T00:00:00Z",
                "reason": "forged",
                "resolution": "recovered",
                "started_at": "2000-01-01T00:00:00Z",
            },
            format="json",
        )
        assert r.status_code == 200
        incident.refresh_from_db()
        assert incident.excluded_from_reports is True
        assert incident.ended_at is None and incident.reason == "timeout"
        assert incident.resolution == "" and incident.started_at.year != 2000

    def test_the_account_endpoint_only_changes_the_time_zone(self, auth_api, user):
        r = auth_api.patch(
            "/api/v1/auth/me/",
            {
                "timezone": "Europe/Paris",
                "email": "attacker@example.com",
                "is_staff": True,
                "is_superuser": True,
                "github_id": "123",
            },
            format="json",
        )
        assert r.status_code == 200
        user.refresh_from_db()
        assert user.timezone == "Europe/Paris" and user.email == "alice@example.com"
        assert not user.is_staff and not user.is_superuser and user.github_id is None


# --- forged and tampered tokens --------------------------------------------------------------


class TestTokens:
    def test_an_unsigned_token_with_alg_none_is_refused(self, user, api):
        header = b64({"alg": "none", "typ": "JWT"})
        body = b64({"token_type": "access", "user_id": str(user.id), "exp": 4102444800, "jti": "x"})
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {header}.{body}.")
        assert api.get("/api/v1/monitors/").status_code == 401

    def test_a_token_signed_with_the_wrong_key_is_refused(self, user, api):
        import jwt

        forged = jwt.encode(
            {"token_type": "access", "user_id": str(user.id), "exp": 4102444800, "jti": "x"},
            "not-the-secret" * 4,
            algorithm="HS256",
        )
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {forged}")
        assert api.get("/api/v1/monitors/").status_code == 401

    def test_a_refresh_token_cannot_be_used_as_an_access_token(self, user, api):
        from rest_framework_simplejwt.tokens import RefreshToken

        api.credentials(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(user)}")
        assert api.get("/api/v1/monitors/").status_code == 401

    def test_an_access_token_for_a_deleted_user_is_refused(self, auth_api, user):
        User.objects.filter(pk=user.pk).delete()
        assert auth_api.get("/api/v1/monitors/").status_code == 401

    def test_the_unlock_token_of_a_status_page_is_not_an_api_credential(self, user, api):
        from statuspages import services

        page = StatusPage.objects.create(user=user, name="P", slug="p", is_public=True)
        services.set_password(page, "correct horse")
        page.save()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {services.make_unlock_token(page)}")
        assert api.get("/api/v1/monitors/").status_code == 401
