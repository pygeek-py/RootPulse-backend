"""One-click connections: Telegram (a bot owned by this deployment), Discord and Slack (OAuth).
Nothing here touches the network: Telegram calls and the OAuth token exchange are stubbed."""

import json
from datetime import timedelta
from io import StringIO
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from django.core import signing
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from accounts.models import AuditLog
from monitoring.models import AlertContact, Monitor
from notifications import channels, integrations
from notifications.channels import CHANNELS
from notifications.http import DeliveryError
from notifications.models import BotState, IntegrationLink

pytestmark = pytest.mark.django_db

BOT_TOKEN = "123456789:" + "B" * 35
DISCORD_HOOK = "https://discord.com/api/webhooks/123456789/abcDEF_ghi-JKL"
SLACK_HOOK = "https://hooks.slack.com/services/T000/B000/abcdefghijkl"
PRIVATE_CHAT = {"id": 4242, "type": "private", "first_name": "Ada", "last_name": "L"}


@pytest.fixture
def telegram(settings, monkeypatch):
    """A configured bot. `telegram.sent` records what the bot said; `telegram.updates` is what
    getUpdates returns next."""
    settings.TELEGRAM_BOT_TOKEN = BOT_TOKEN
    settings.TELEGRAM_BOT_USERNAME = "RootPulseBot"

    class Bot:
        def __init__(self):
            self.sent = []
            self.updates = []
            self.calls = []
            self.conflict = False

    bot = Bot()

    def call(method, payload):
        bot.calls.append((method, payload))
        if method == "sendMessage":
            bot.sent.append((payload["chat_id"], payload["text"]))
            return {}
        if method == "getUpdates":
            if bot.conflict:
                return None
            batch, bot.updates = bot.updates, []
            return batch
        if method == "getMe":
            return {"username": "FetchedBot"}
        return {}

    monkeypatch.setattr(integrations, "_telegram_call", call)
    integrations._bot_username.clear()
    return bot


@pytest.fixture
def discord(settings):
    settings.DISCORD_CLIENT_ID = "discord-client"
    settings.DISCORD_CLIENT_SECRET = "discord-secret"


@pytest.fixture
def slack(settings):
    settings.SLACK_CLIENT_ID = "slack-client"
    settings.SLACK_CLIENT_SECRET = "slack-secret"


def monitor_for(user, name="Shop"):
    return Monitor.objects.create(
        user=user, name=name, type="http", target="https://shop.example.com", interval_seconds=300
    )


def start_update(token, chat=PRIVATE_CHAT, update_id=1, text=None):
    return {
        "update_id": update_id,
        "message": {"chat": chat, "text": text or f"/start {token}"},
    }


class TestAvailability:
    def test_nothing_is_offered_until_configured(self, auth_api):
        body = auth_api.get("/api/v1/integrations/").json()
        assert body == {
            "telegram": {"available": False, "bot_username": None},
            "discord": {"available": False},
            "slack": {"available": False},
        }

    def test_each_one_switches_on_with_its_credentials(self, auth_api, telegram, discord, slack):
        body = auth_api.get("/api/v1/integrations/").json()
        assert body["telegram"] == {"available": True, "bot_username": "RootPulseBot"}
        assert body["discord"]["available"] and body["slack"]["available"]

    def test_the_bot_name_is_looked_up_when_not_configured(self, auth_api, telegram, settings):
        settings.TELEGRAM_BOT_USERNAME = ""
        body = auth_api.get("/api/v1/integrations/").json()
        assert body["telegram"]["bot_username"] == "FetchedBot"

    def test_secrets_are_never_in_the_response(self, auth_api, telegram, discord, slack):
        raw = auth_api.get("/api/v1/integrations/").content.decode()
        for secret in (BOT_TOKEN, "discord-secret", "slack-secret"):
            assert secret not in raw

    def test_requires_sign_in(self, api):
        assert api.get("/api/v1/integrations/").status_code == 401


class TestTelegramLink:
    def test_start_gives_a_deep_link_and_stores_only_a_hash(self, auth_api, telegram):
        resp = auth_api.post("/api/v1/integrations/telegram/start/")
        assert resp.status_code == 200
        body = resp.json()
        token = parse_qs(urlsplit(body["url"]).query)["start"][0]
        assert body["url"].startswith("https://t.me/RootPulseBot?start=")
        assert body["group_url"].startswith("https://t.me/RootPulseBot?startgroup=")
        assert body["expires_in"] == 15 * 60
        link = IntegrationLink.objects.get(pk=body["id"])
        assert token not in (link.token_hash, str(link.id)) and len(link.token_hash) == 64

    def test_not_configured_is_a_clear_refusal(self, auth_api):
        resp = auth_api.post("/api/v1/integrations/telegram/start/")
        assert resp.status_code == 400 and "isn't set up" in resp.json()["detail"]

    def test_requires_sign_in_and_is_rate_limited(self, auth_api, telegram):
        from rest_framework.test import APIClient

        from tests.conftest import ORIGIN

        anonymous = APIClient(HTTP_ORIGIN=ORIGIN)
        assert anonymous.post("/api/v1/integrations/telegram/start/").status_code in (401, 403)
        codes = [
            auth_api.post("/api/v1/integrations/telegram/start/").status_code for _ in range(32)
        ]
        assert codes[:30] == [200] * 30 and 429 in codes[30:]

    def start(self, api):
        body = api.post("/api/v1/integrations/telegram/start/").json()
        return body["id"], parse_qs(urlsplit(body["url"]).query)["start"][0]

    def test_pressing_start_connects_the_chat_and_confirms_in_it(self, auth_api, user, telegram):
        shop = monitor_for(user)
        link_id, token = self.start(auth_api)
        status_url = f"/api/v1/integrations/telegram/links/{link_id}/"
        assert auth_api.get(status_url).json() == {"status": "pending", "contact": None}

        integrations.handle_update(start_update(token))

        body = auth_api.get(status_url).json()
        assert body["status"] == "connected"
        contact = body["contact"]
        assert contact["channel"] == "telegram" and contact["name"] == "Telegram: Ada L"
        assert contact["monitor_count"] == 1 and contact["verified"] is True
        assert contact["config"] == {"connected": True, "chat": "Ada L"}
        assert contact["summary"] == "Chat: Ada L"
        stored = AlertContact.objects.get(pk=contact["id"])
        assert stored.config == {"managed": True, "chat_id": "4242", "chat_title": "Ada L"}
        assert stored in shop.alert_contacts.all()
        assert telegram.sent[0][0] == 4242 and "Connected to RootPulse" in telegram.sent[0][1]

    def test_the_token_never_comes_back_in_any_response(self, auth_api, telegram):
        link_id, token = self.start(auth_api)
        integrations.handle_update(start_update(token))
        raw = auth_api.get(f"/api/v1/integrations/telegram/links/{link_id}/").content.decode()
        raw += auth_api.get("/api/v1/alert-contacts/").content.decode()
        assert token not in raw and BOT_TOKEN not in raw

    def test_a_link_works_once(self, auth_api, telegram):
        _, token = self.start(auth_api)
        integrations.handle_update(start_update(token))
        integrations.handle_update(start_update(token, chat={"id": 999, "type": "private"}))
        assert AlertContact.objects.count() == 1
        assert "already used" in telegram.sent[-1][1]

    def test_an_expired_or_unknown_link_connects_nothing(self, auth_api, telegram):
        link_id, token = self.start(auth_api)
        IntegrationLink.objects.filter(pk=link_id).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        integrations.handle_update(start_update(token))
        integrations.handle_update(start_update("not-a-real-token-123"))
        assert AlertContact.objects.count() == 0
        assert all("expired" in text for _, text in telegram.sent)
        status = auth_api.get(f"/api/v1/integrations/telegram/links/{link_id}/").json()
        assert status["status"] == "expired"

    def test_a_bare_start_explains_what_to_do(self, telegram):
        integrations.handle_update(start_update(None, text="/start"))
        assert "Connect Telegram" in telegram.sent[0][1]

    def test_other_messages_are_ignored(self, telegram):
        integrations.handle_update(start_update(None, text="hello"))
        integrations.handle_update({"update_id": 5, "edited_message": {}})
        assert telegram.sent == []

    def test_groups_work_too(self, auth_api, telegram):
        _, token = self.start(auth_api)
        group = {"id": -100123, "type": "group", "title": "Ops"}
        integrations.handle_update(
            start_update(token, chat=group, text=f"/start@RootPulseBot {token}")
        )
        contact = AlertContact.objects.get()
        assert contact.config["chat_id"] == "-100123" and contact.name == "Telegram: Ops"

    def test_connecting_the_same_chat_again_reuses_the_contact(self, auth_api, telegram):
        _, first = self.start(auth_api)
        integrations.handle_update(start_update(first))
        AlertContact.objects.update(enabled=False)
        _, second = self.start(auth_api)
        integrations.handle_update(start_update(second, update_id=2))
        contact = AlertContact.objects.get()
        assert contact.enabled is True

    def test_the_contact_limit_is_respected_and_the_link_is_not_burnt(
        self, auth_api, user, telegram, settings
    ):
        settings.MAX_ALERT_CONTACTS_PER_USER = 1
        AlertContact.objects.create(user=user, name="x", channel="slack", config={})
        link_id, token = self.start(auth_api)
        integrations.handle_update(start_update(token))
        assert AlertContact.objects.count() == 1
        assert "limit" in telegram.sent[-1][1]
        assert IntegrationLink.objects.get(pk=link_id).consumed_at is None

    def test_one_users_link_cannot_be_read_by_another(self, auth_api, make_user, sign_in, telegram):
        from rest_framework.test import APIClient

        from tests.conftest import ORIGIN

        link_id, _ = self.start(auth_api)
        other = APIClient(HTTP_ORIGIN=ORIGIN)
        user = make_user("mallory@example.com")
        other.credentials(
            HTTP_AUTHORIZATION=f"Bearer {sign_in(other, user.email).json()['access_token']}"
        )
        assert other.get(f"/api/v1/integrations/telegram/links/{link_id}/").status_code == 404

    def test_the_status_check_asks_telegram_for_waiting_messages(self, auth_api, user, telegram):
        link_id, token = self.start(auth_api)
        telegram.updates = [start_update(token, update_id=77)]
        body = auth_api.get(f"/api/v1/integrations/telegram/links/{link_id}/").json()
        assert body["status"] == "connected"
        assert BotState.objects.get(key=integrations.POLL_STATE_KEY).value == 78


class TestPolling:
    def test_remembers_where_it_left_off(self, telegram):
        telegram.updates = [
            start_update(None, text="hi", update_id=10),
            start_update(None, text="yo", update_id=11),
        ]
        assert integrations.poll_once() == 2
        assert BotState.objects.get().value == 12
        telegram.updates = []
        from django.core.cache import cache

        cache.clear()
        integrations.poll_once()
        assert telegram.calls[-1][1]["offset"] == 12

    def test_a_bad_update_does_not_wedge_the_queue(self, telegram, monkeypatch):
        def explode(update):
            raise RuntimeError("bug")

        monkeypatch.setattr(integrations, "handle_update", explode)
        telegram.updates = [start_update(None, text="/start x", update_id=3)]
        assert integrations.poll_once() == 1
        assert BotState.objects.get().value == 4

    def test_does_nothing_when_a_webhook_is_in_charge(self, telegram, settings):
        settings.TELEGRAM_WEBHOOK_SECRET = "s3cret"
        assert integrations.poll_enabled() is False
        assert integrations.poll_once() == 0
        assert telegram.calls == []

    def test_does_nothing_without_a_bot(self):
        assert integrations.poll_once() == 0

    def test_a_telegram_conflict_is_quietly_ignored(self, telegram):
        telegram.conflict = True
        assert integrations.poll_once() == 0

    def test_run_engine_listens_too(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            "monitoring.management.commands.run_engine.poll_once", lambda: calls.append(1)
        )
        call_command("run_engine", stdout=StringIO())
        assert calls == [1]


class TestWebhook:
    URL = "/api/v1/integrations/telegram/webhook/"

    def test_wrong_or_missing_secret_is_refused(self, api, telegram, settings):
        settings.TELEGRAM_WEBHOOK_SECRET = "s3cret"
        assert api.post(self.URL, {}, format="json").status_code == 401
        resp = api.post(self.URL, {}, format="json", HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="nope")
        assert resp.status_code == 401

    def test_unconfigured_secret_means_nobody_gets_in(self, api, telegram):
        resp = api.post(self.URL, {}, format="json", HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="")
        assert resp.status_code == 401

    def test_the_right_secret_delivers_an_update(self, api, auth_api, telegram, settings):
        settings.TELEGRAM_WEBHOOK_SECRET = "s3cret"
        token = parse_qs(
            urlsplit(auth_api.post("/api/v1/integrations/telegram/start/").json()["url"]).query
        )["start"][0]
        resp = api.post(
            self.URL,
            start_update(token),
            format="json",
            HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="s3cret",
        )
        assert resp.status_code == 200 and AlertContact.objects.count() == 1

    def test_a_broken_update_still_gets_a_200_so_telegram_doesnt_retry_forever(
        self, api, telegram, settings
    ):
        settings.TELEGRAM_WEBHOOK_SECRET = "s3cret"
        resp = api.post(
            self.URL,
            {"message": {"chat": {"id": "x"}, "text": "/start abcdefghij"}},
            format="json",
            HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="s3cret",
        )
        assert resp.status_code == 200

    def test_it_is_not_in_the_public_api_docs(self, client):
        schema = client.get("/api/v1/schema/", HTTP_ACCEPT="application/json").json()
        hidden = ("telegram/webhook", "integrations/{provider}/callback")
        assert not any(h in path for path in schema["paths"] for h in hidden)


class TestManagedTelegramContacts:
    CONFIG = {"managed": True, "chat_id": "4242", "chat_title": "Ada L"}
    PAYLOAD = {
        "event": "opened",
        "severity": "down",
        "title": "Shop is DOWN",
        "summary": "Timed out.",
        "url": "http://localhost:3000/incidents/1",
        "created_at": "2026-10-04T10:00:00+00:00",
        "monitor": {
            "id": "m",
            "name": "Shop",
            "type": "http",
            "target": "https://shop.example.com",
        },
        "incident": None,
    }

    def test_sends_through_the_deployments_bot(self, settings, monkeypatch):
        settings.TELEGRAM_BOT_TOKEN = BOT_TOKEN
        seen = {}

        def fake(url, body, headers, **kw):
            seen.update(url=url, body=json.loads(body))
            return httpx.Response(200, json={"ok": True})

        monkeypatch.setattr(channels, "post_json", fake)
        CHANNELS["telegram"].send(self.CONFIG, self.PAYLOAD, 1)
        assert seen["url"] == f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        assert seen["body"]["chat_id"] == "4242"

    def test_without_a_bot_it_fails_permanently(self):
        with pytest.raises(DeliveryError) as exc:
            CHANNELS["telegram"].send(self.CONFIG, self.PAYLOAD, 1)
        assert exc.value.retryable is False

    def test_a_kicked_bot_is_a_permanent_failure(self, settings, monkeypatch):
        settings.TELEGRAM_BOT_TOKEN = BOT_TOKEN
        monkeypatch.setattr(
            channels,
            "post_json",
            lambda *a, **k: httpx.Response(
                403, json={"ok": False, "description": "Forbidden: bot was blocked by the user"}
            ),
        )
        with pytest.raises(DeliveryError) as exc:
            CHANNELS["telegram"].send(self.CONFIG, self.PAYLOAD, 1)
        assert exc.value.retryable is False and "blocked" in exc.value.message
        assert BOT_TOKEN not in exc.value.message

    def test_people_cannot_forge_a_managed_chat(self, auth_api):
        resp = auth_api.post(
            "/api/v1/alert-contacts/",
            {"name": "x", "channel": "telegram", "config": {"managed": True, "chat_id": "99"}},
            format="json",
        )
        assert resp.status_code == 400 and "config.managed" in resp.json()["field_errors"]
        assert not AlertContact.objects.exists()

    def test_a_connected_chat_cannot_be_repointed_but_can_be_renamed_or_switched_off(
        self, auth_api, user
    ):
        contact = AlertContact.objects.create(
            user=user, name="Telegram: Ada", channel="telegram", config=self.CONFIG
        )
        url = f"/api/v1/alert-contacts/{contact.id}/"
        bad = auth_api.patch(url, {"config": {"chat_id": "99"}}, format="json")
        assert bad.status_code == 400
        ok = auth_api.patch(url, {"name": "My phone", "enabled": False}, format="json")
        assert ok.status_code == 200 and ok.json()["name"] == "My phone"
        contact.refresh_from_db()
        assert contact.config == self.CONFIG

    def test_manual_telegram_still_needs_its_own_token(self, auth_api):
        resp = auth_api.post(
            "/api/v1/alert-contacts/",
            {"name": "x", "channel": "telegram", "config": {"chat_id": "5"}},
            format="json",
        )
        assert resp.status_code == 400 and "config.bot_token" in resp.json()["field_errors"]


class TestOAuthStart:
    @pytest.mark.parametrize("provider", ["discord", "slack"])
    def test_returns_the_providers_page_with_a_state_and_our_redirect(
        self, auth_api, discord, slack, provider
    ):
        resp = auth_api.post(f"/api/v1/integrations/{provider}/start/")
        assert resp.status_code == 200
        url = urlsplit(resp.json()["authorize_url"])
        query = parse_qs(url.query)
        assert url.hostname in ("discord.com", "slack.com")
        assert query["client_id"] == [f"{provider}-client"]
        assert query["redirect_uri"] == [
            f"http://localhost:8000/api/v1/integrations/{provider}/callback/"
        ]
        assert query["scope"] == (
            ["webhook.incoming"] if provider == "discord" else ["incoming-webhook"]
        )
        assert query["state"][0]
        assert f"{provider}-secret" not in resp.content.decode()

    def test_unconfigured_is_a_clear_refusal(self, auth_api):
        resp = auth_api.post("/api/v1/integrations/discord/start/")
        assert resp.status_code == 400 and "isn't set up" in resp.json()["detail"]

    def test_unknown_providers_do_not_exist(self):
        from django.urls import Resolver404, resolve

        with pytest.raises(Resolver404):
            resolve("/api/v1/integrations/github/start/")

    def test_requires_sign_in(self, api, discord):
        assert api.post("/api/v1/integrations/discord/start/").status_code in (401, 403)


def callback(api, provider, **params):
    return api.get(f"/api/v1/integrations/{provider}/callback/", params)


def location(resp):
    return resp["Location"]


class TestOAuthCallback:
    def state(self, user, provider):
        return integrations.make_state(user, provider)

    def grant(self, monkeypatch, provider, body):
        monkeypatch.setattr(integrations, "_exchange", lambda p, code: body)

    def test_discord_creates_a_contact_and_sends_you_back(self, api, user, discord, monkeypatch):
        shop = monitor_for(user)
        self.grant(monkeypatch, "discord", {"webhook": {"url": DISCORD_HOOK, "channel_id": "1"}})
        resp = callback(api, "discord", code="abc", state=self.state(user, "discord"))
        assert resp.status_code == 302
        assert location(resp) == "http://localhost:3000/settings/notifications?connected=discord"
        contact = AlertContact.objects.get()
        assert (contact.channel, contact.name, contact.user) == ("discord", "Discord", user)
        assert contact.config == {"webhook_url": DISCORD_HOOK}
        assert contact in shop.alert_contacts.all()
        assert DISCORD_HOOK not in location(resp)
        assert AuditLog.objects.filter(user=user, action="alert_contact_created").exists()

    def test_slack_names_the_contact_after_the_channel(self, api, user, slack, monkeypatch):
        self.grant(
            monkeypatch,
            "slack",
            {
                "ok": True,
                "incoming_webhook": {"url": SLACK_HOOK, "channel": "#alerts"},
                "team": {"name": "Acme"},
            },
        )
        resp = callback(api, "slack", code="abc", state=self.state(user, "slack"))
        assert "connected=slack" in location(resp)
        assert AlertContact.objects.get().name == "Slack: #alerts (Acme)"

    def test_the_connected_contact_is_masked_like_any_other(
        self, api, auth_api, user, discord, monkeypatch
    ):
        self.grant(monkeypatch, "discord", {"webhook": {"url": DISCORD_HOOK}})
        callback(api, "discord", code="abc", state=self.state(user, "discord"))
        raw = auth_api.get("/api/v1/alert-contacts/").content.decode()
        assert "abcDEF" not in raw and "123456789/" not in raw

    @pytest.mark.parametrize(
        "params",
        [
            {},
            {"code": "abc"},
            {"code": "abc", "state": "garbage"},
        ],
    )
    def test_a_missing_or_forged_state_is_refused(self, api, user, discord, monkeypatch, params):
        self.grant(monkeypatch, "discord", {"webhook": {"url": DISCORD_HOOK}})
        resp = callback(api, "discord", **params)
        assert "connect_error=state" in location(resp)
        assert not AlertContact.objects.exists()

    def test_a_state_for_another_provider_is_refused(self, api, user, discord, slack, monkeypatch):
        self.grant(monkeypatch, "slack", {"ok": True, "incoming_webhook": {"url": SLACK_HOOK}})
        resp = callback(api, "slack", code="abc", state=self.state(user, "discord"))
        assert "connect_error=state" in location(resp)

    def test_an_old_state_is_refused(self, api, user, discord, monkeypatch):
        self.grant(monkeypatch, "discord", {"webhook": {"url": DISCORD_HOOK}})
        state = self.state(user, "discord")
        monkeypatch.setattr(integrations, "STATE_MAX_AGE", -1)
        assert "connect_error=state" in location(callback(api, "discord", code="abc", state=state))

    def test_a_state_signed_with_another_key_is_refused(self, api, user, discord):
        forged = signing.dumps({"u": str(user.pk), "p": "discord"}, salt="some-other-salt")
        assert "connect_error=state" in location(callback(api, "discord", code="abc", state=forged))

    def test_a_disabled_account_cannot_be_connected(self, api, user, discord, monkeypatch):
        self.grant(monkeypatch, "discord", {"webhook": {"url": DISCORD_HOOK}})
        state = self.state(user, "discord")
        user.is_active = False
        user.save()
        assert "connect_error=state" in location(callback(api, "discord", code="abc", state=state))

    def test_saying_no_on_the_providers_page_is_handled(self, api, user, discord):
        resp = callback(api, "discord", error="access_denied", state=self.state(user, "discord"))
        assert "connect_error=denied" in location(resp)
        assert not AlertContact.objects.exists()

    @pytest.mark.parametrize("provider", ["discord", "slack"])
    def test_an_unconfigured_server_says_so(self, api, user, provider):
        resp = callback(api, provider, code="abc", state="x")
        assert "connect_error=not_configured" in location(resp)

    def test_a_failed_exchange_is_reported_without_details(self, api, user, discord, monkeypatch):
        def fail(provider, code):
            raise integrations.IntegrationError("failed")

        monkeypatch.setattr(integrations, "_exchange", fail)
        resp = callback(api, "discord", code="abc", state=self.state(user, "discord"))
        assert "connect_error=failed" in location(resp) and "abc" not in location(resp)

    def test_a_webhook_url_of_the_wrong_shape_is_refused(self, api, user, discord, monkeypatch):
        self.grant(monkeypatch, "discord", {"webhook": {"url": "https://evil.example.com/x"}})
        resp = callback(api, "discord", code="abc", state=self.state(user, "discord"))
        assert "connect_error=invalid" in location(resp)
        assert not AlertContact.objects.exists()

    def test_the_contact_limit_is_respected(self, api, user, discord, monkeypatch, settings):
        settings.MAX_ALERT_CONTACTS_PER_USER = 0
        self.grant(monkeypatch, "discord", {"webhook": {"url": DISCORD_HOOK}})
        resp = callback(api, "discord", code="abc", state=self.state(user, "discord"))
        assert "connect_error=limit" in location(resp)

    def test_slack_refusing_is_handled(self, api, user, slack, monkeypatch):
        self.grant(monkeypatch, "slack", {"ok": False, "error": "invalid_code"})
        resp = callback(api, "slack", code="abc", state=self.state(user, "slack"))
        assert "connect_error=failed" in location(resp)

    def test_the_callback_is_rate_limited(self, api, user, discord):
        codes = [callback(api, "discord", code="a", state="x").status_code for _ in range(32)]
        assert 429 in codes


class TestTokenExchange:
    """The real `_exchange`, against a stub transport."""

    def serve(self, monkeypatch, handler):
        real = httpx.Client
        monkeypatch.setattr(
            integrations.httpx,
            "Client",
            lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
        )

    def test_discord_posts_the_code_and_returns_the_webhook(self, discord, monkeypatch):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["form"] = parse_qs(request.content.decode())
            return httpx.Response(200, json={"webhook": {"url": DISCORD_HOOK}})

        self.serve(monkeypatch, handler)
        granted = integrations.redeem("discord", "the-code")
        assert granted.webhook_url == DISCORD_HOOK
        assert seen["url"] == "https://discord.com/api/oauth2/token"
        assert seen["form"]["code"] == ["the-code"]
        assert seen["form"]["grant_type"] == ["authorization_code"]

    def test_slack_posts_to_oauth_v2_access(self, slack, monkeypatch):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(
                200, json={"ok": True, "incoming_webhook": {"url": SLACK_HOOK, "channel": "#a"}}
            )

        self.serve(monkeypatch, handler)
        assert integrations.redeem("slack", "c").webhook_url == SLACK_HOOK
        assert seen["url"] == "https://slack.com/api/oauth.v2.access"

    @pytest.mark.parametrize("status", [400, 401, 500])
    def test_error_statuses_fail_cleanly(self, discord, monkeypatch, status):
        self.serve(monkeypatch, lambda r: httpx.Response(status, json={"error": "nope"}))
        with pytest.raises(integrations.IntegrationError) as exc:
            integrations.redeem("discord", "c")
        assert exc.value.code == "failed"

    def test_network_errors_never_leak_the_code(self, discord, monkeypatch):
        def boom(request):
            raise httpx.ConnectError("could not connect, code=the-secret-code")

        self.serve(monkeypatch, boom)
        with pytest.raises(integrations.IntegrationError) as exc:
            integrations.redeem("discord", "the-secret-code")
        assert "the-secret-code" not in str(exc.value) and exc.value.__cause__ is not None

    def test_a_reply_without_a_webhook_is_a_failure(self, discord, monkeypatch):
        self.serve(monkeypatch, lambda r: httpx.Response(200, json={"access_token": "x"}))
        with pytest.raises(integrations.IntegrationError):
            integrations.redeem("discord", "c")

    def test_a_non_json_reply_is_a_failure(self, discord, monkeypatch):
        self.serve(monkeypatch, lambda r: httpx.Response(200, text="<html>"))
        with pytest.raises(integrations.IntegrationError):
            integrations.redeem("discord", "c")


class TestWebhookCommand:
    def run(self, *args):
        out = StringIO()
        call_command("telegram_webhook", *args, stdout=out)
        return out.getvalue()

    def test_needs_a_bot(self):
        with pytest.raises(CommandError, match="TELEGRAM_BOT_TOKEN"):
            self.run("info")

    def test_set_needs_a_secret_and_https(self, settings):
        settings.TELEGRAM_BOT_TOKEN = BOT_TOKEN
        with pytest.raises(CommandError, match="TELEGRAM_WEBHOOK_SECRET"):
            self.run("set")
        settings.TELEGRAM_WEBHOOK_SECRET = "s3cret"
        settings.API_PUBLIC_URL = "http://localhost:8000"
        with pytest.raises(CommandError, match="https"):
            self.run("set")

    def test_set_registers_the_url_with_the_secret(self, settings, monkeypatch):
        settings.TELEGRAM_BOT_TOKEN = BOT_TOKEN
        settings.TELEGRAM_WEBHOOK_SECRET = "s3cret"
        settings.API_PUBLIC_URL = "https://api.example.com"
        seen = {}

        def fake(url, body, headers, **kw):
            seen.update(url=url, body=json.loads(body))
            return httpx.Response(200, json={"ok": True, "result": True})

        monkeypatch.setattr("notifications.management.commands.telegram_webhook.post_json", fake)
        assert "https://api.example.com/api/v1/integrations/telegram/webhook/" in self.run("set")
        assert seen["url"].endswith("/setWebhook")
        assert seen["body"]["secret_token"] == "s3cret"
        assert seen["body"]["allowed_updates"] == ["message"]

    def test_telegrams_refusal_is_reported(self, settings, monkeypatch):
        settings.TELEGRAM_BOT_TOKEN = BOT_TOKEN
        monkeypatch.setattr(
            "notifications.management.commands.telegram_webhook.post_json",
            lambda *a, **k: httpx.Response(401, json={"ok": False, "description": "Unauthorized"}),
        )
        with pytest.raises(CommandError, match="Unauthorized"):
            self.run("delete")
