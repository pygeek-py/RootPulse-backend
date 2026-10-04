"""Each alert channel on its own: config validation, what gets sent, how failures are classified.
No test here touches the network; HTTP is served by httpx.MockTransport or a stub."""

import json

import httpx
import pytest
from django.core import mail

from monitoring import signing
from notifications import channels, http
from notifications.channels import CHANNELS, ConfigError
from notifications.http import DeliveryError

PAYLOAD = {
    "event": "opened",
    "severity": "down",
    "title": "Shop is DOWN",
    "summary": "Unexpected status 503.",
    "url": "http://localhost:3000/incidents/abc",
    "created_at": "2026-10-04T10:00:00+00:00",
    "monitor": {"id": "m1", "name": "Shop", "type": "http", "target": "https://shop.example.com"},
    "incident": {
        "id": "i1",
        "started_at": "2026-10-04T09:59:00+00:00",
        "ended_at": None,
        "duration_seconds": 60,
        "reason": "status_mismatch",
        "reason_label": "Unexpected status 503",
        "status_code": 503,
    },
}

SLACK = "https://hooks.slack.com/services/T000/B000/abcdefghijkl"
DISCORD = "https://discord.com/api/webhooks/123456789/abcDEF_ghi-JKL"
TOKEN = "123456789:" + "A" * 35


@pytest.fixture
def served(monkeypatch):
    """Route post_json through a MockTransport; `served.requests` records what was sent."""
    state = {"status": 200, "body": {}, "headers": {}, "raise": None}
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if state["raise"]:
            raise state["raise"]
        return httpx.Response(state["status"], json=state["body"], headers=state["headers"])

    real_post_json = http.post_json

    def fake(url, body, headers, *, transport=None):
        return real_post_json(url, body, headers, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(channels, "post_json", fake)
    fake.requests = requests
    fake.state = state
    return fake


class TestEmail:
    def test_validates_and_normalises_the_address(self):
        assert CHANNELS["email"].clean({"address": " Ops@Example.COM "}) == {
            "address": "ops@example.com"
        }
        with pytest.raises(ConfigError) as exc:
            CHANNELS["email"].clean({"address": "nope"})
        assert "address" in exc.value.errors
        with pytest.raises(ConfigError):
            CHANNELS["email"].clean({"address": "a@b.co", "extra": 1})

    def test_sends_a_plain_and_html_message(self):
        CHANNELS["email"].send({"address": "ops@example.com"}, PAYLOAD, 1)
        message = mail.outbox[-1]
        assert message.to == ["ops@example.com"]
        assert message.subject == "[DOWN] Shop is DOWN"
        assert "https://shop.example.com" in message.body
        assert PAYLOAD["url"] in message.body
        assert "View details" in message.alternatives[0][0]

    def test_html_in_a_monitor_name_is_escaped(self):
        evil = {**PAYLOAD, "title": "<script>alert(1)</script> is DOWN"}
        CHANNELS["email"].send({"address": "ops@example.com"}, evil, 1)
        html_body = mail.outbox[-1].alternatives[0][0]
        assert "<script>" not in html_body and "&lt;script&gt;" in html_body

    def test_a_refused_recipient_is_permanent_and_other_errors_are_retried(self, monkeypatch):
        import smtplib

        def refuse(*a, **k):
            raise smtplib.SMTPRecipientsRefused({"x@y.z": (550, b"no")})

        monkeypatch.setattr(channels, "send_mail", refuse)
        with pytest.raises(DeliveryError) as exc:
            CHANNELS["email"].send({"address": "x@y.z"}, PAYLOAD, 1)
        assert exc.value.retryable is False

        def flaky(*a, **k):
            raise ConnectionError("smtp down")

        monkeypatch.setattr(channels, "send_mail", flaky)
        with pytest.raises(DeliveryError) as exc:
            CHANNELS["email"].send({"address": "x@y.z"}, PAYLOAD, 1)
        assert exc.value.retryable is True


class TestWebhook:
    def test_generates_a_secret_and_keeps_it_across_edits(self):
        first = CHANNELS["webhook"].clean({"url": "https://hooks.example.com/in"})
        assert len(first["secret"]) >= 32
        again = CHANNELS["webhook"].clean(
            {"url": "https://hooks.example.com/other"}, previous=first
        )
        assert again["secret"] == first["secret"]

    def test_the_secret_cannot_be_chosen_by_the_caller_on_create(self):
        cleaned = CHANNELS["webhook"].clean({"url": "https://hooks.example.com/in", "secret": "x"})
        assert cleaned["secret"] != "x" and len(cleaned["secret"]) >= 32

    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost/hook",
            "http://127.0.0.1/hook",
            "http://169.254.169.254/latest/meta-data",
            "ftp://example.com/x",
            "https://user:pass@example.com/x",
        ],
    )
    def test_rejects_private_and_odd_urls(self, url):
        with pytest.raises(ConfigError) as exc:
            CHANNELS["webhook"].clean({"url": url})
        assert "url" in exc.value.errors

    def test_only_the_host_is_shown_back(self):
        config = CHANNELS["webhook"].clean({"url": "https://hooks.example.com/in?token=s3cret"})
        shown = CHANNELS["webhook"].public(config)
        assert shown == {"host": "hooks.example.com"}
        assert "s3cret" not in json.dumps(shown)
        assert "s3cret" not in CHANNELS["webhook"].summary(config)

    def test_sends_signed_json(self, monkeypatch):
        seen = {}

        def fake_safe_post(url, body, headers):
            seen.update(url=url, body=body, headers=headers)
            return 200, "ok"

        monkeypatch.setattr(channels, "safe_post", fake_safe_post)
        config = {"url": "https://hooks.example.com/in", "secret": "topsecret"}
        CHANNELS["webhook"].send(config, PAYLOAD, 42)

        body = json.loads(seen["body"])
        assert body["event"] == "incident.opened" and body["id"] == 42
        assert body["monitor"]["name"] == "Shop"
        assert seen["headers"]["X-RootPulse-Event"] == "incident.opened"
        assert seen["headers"]["X-RootPulse-Delivery"] == "42"
        assert signing.verify("topsecret", seen["headers"][signing.HEADER], seen["body"])
        assert not signing.verify("wrong", seen["headers"][signing.HEADER], seen["body"])

    @pytest.mark.parametrize(
        ("status", "retryable"),
        [(500, True), (503, True), (429, True), (400, False), (401, False), (404, False)],
    )
    def test_failures_are_classified(self, monkeypatch, status, retryable):
        monkeypatch.setattr(channels, "safe_post", lambda *a, **k: (status, ""))
        with pytest.raises(DeliveryError) as exc:
            CHANNELS["webhook"].send({"url": "https://h.example.com/x", "secret": "s"}, PAYLOAD, 1)
        assert exc.value.retryable is retryable
        assert "h.example.com/x" not in exc.value.message


class TestSafePost:
    """The webhook URL is user supplied, so it gets the same SSRF treatment as monitoring."""

    def run(self, url, *, status=200, resolve=None, redirect=None):
        seen = []

        def handler(request):
            seen.append(request)
            if redirect:
                return httpx.Response(302, headers={"location": redirect})
            return httpx.Response(status, text="fine")

        kwargs = {"transport": httpx.MockTransport(handler)}
        if resolve:
            kwargs["resolve"] = resolve
        return http.safe_post(url, b"{}", {"Content-Type": "application/json"}, **kwargs), seen

    def test_connects_to_the_vetted_ip_and_keeps_the_host_header(self, dns):
        dns["hooks.example.com"] = ["93.184.216.34"]
        (status, text), seen = self.run("https://hooks.example.com/in?x=1")
        assert (status, text) == (200, "fine")
        assert seen[0].url.host == "93.184.216.34"
        assert seen[0].headers["host"] == "hooks.example.com"
        assert seen[0].url.query == b"x=1"

    def test_refuses_a_name_that_resolves_privately(self, dns):
        dns["sneaky.example.com"] = ["10.0.0.5"]
        with pytest.raises(DeliveryError) as exc:
            self.run("https://sneaky.example.com/in")
        assert exc.value.retryable is False

    def test_a_failed_lookup_is_worth_retrying(self, dns):
        dns["gone.example.com"] = None
        with pytest.raises(DeliveryError) as exc:
            self.run("https://gone.example.com/in")
        assert exc.value.retryable is True

    def test_never_follows_redirects(self, dns):
        (status, _), seen = self.run("https://hooks.example.com/in", redirect="http://10.0.0.1/")
        assert status == 302 and len(seen) == 1

    def test_credentials_in_the_url_are_refused(self):
        with pytest.raises(DeliveryError):
            self.run("https://user:pw@hooks.example.com/in")

    def test_transport_errors_never_leak_the_url(self, dns):
        def boom(request):
            raise httpx.ConnectError("could not connect to https://hooks.example.com/in?token=abc")

        with pytest.raises(DeliveryError) as exc:
            http.safe_post(
                "https://hooks.example.com/in?token=abc",
                b"{}",
                {},
                transport=httpx.MockTransport(boom),
            )
        assert "token" not in exc.value.message and exc.value.retryable is True


class TestSlack:
    def test_only_real_slack_hooks_are_accepted(self):
        assert CHANNELS["slack"].clean({"webhook_url": SLACK}) == {"webhook_url": SLACK}
        for bad in (
            "https://evil.example.com/services/T/B/x",
            "http://hooks.slack.com/services/T/B/x",
            "https://hooks.slack.com.evil.com/services/T/B/x",
            "https://hooks.slack.com/other/T/B/x",
        ):
            with pytest.raises(ConfigError):
                CHANNELS["slack"].clean({"webhook_url": bad})

    def test_the_url_is_a_credential_and_is_masked(self):
        shown = CHANNELS["slack"].public({"webhook_url": SLACK})
        assert SLACK not in json.dumps(shown) and "T000" not in json.dumps(shown)

    def test_sends_a_coloured_message_with_escaped_text(self, served):
        evil = {**PAYLOAD, "title": "<!channel> & <http://x|click> is DOWN"}
        CHANNELS["slack"].send({"webhook_url": SLACK}, evil, 1)
        body = json.loads(served.requests[0].content)
        text = json.dumps(body)
        assert "<!channel>" not in text and "&lt;!channel&gt;" in text
        assert body["attachments"][0]["color"] == "#dc2626"
        assert str(served.requests[0].url) == SLACK

    @pytest.mark.parametrize(
        ("status", "retryable"), [(429, True), (500, True), (403, False), (404, False)]
    )
    def test_failures(self, served, status, retryable):
        served.state["status"] = status
        with pytest.raises(DeliveryError) as exc:
            CHANNELS["slack"].send({"webhook_url": SLACK}, PAYLOAD, 1)
        assert exc.value.retryable is retryable
        assert "T000" not in exc.value.message

    def test_retry_after_is_honoured(self, served):
        served.state.update(status=429, headers={"retry-after": "30"})
        with pytest.raises(DeliveryError) as exc:
            CHANNELS["slack"].send({"webhook_url": SLACK}, PAYLOAD, 1)
        assert exc.value.retry_after == 30

    def test_a_network_failure_is_retried_without_leaking_the_url(self, served):
        served.state["raise"] = httpx.ConnectError(f"cannot reach {SLACK}")
        with pytest.raises(DeliveryError) as exc:
            CHANNELS["slack"].send({"webhook_url": SLACK}, PAYLOAD, 1)
        assert exc.value.retryable and "T000" not in exc.value.message


class TestDiscord:
    def test_only_real_discord_webhooks_are_accepted(self):
        assert CHANNELS["discord"].clean({"webhook_url": DISCORD})
        for bad in (
            "https://discord.com.evil.com/api/webhooks/1/x",
            "https://example.com/api/webhooks/1/x",
            "https://discord.com/api/webhooks/notanumber/x",
        ):
            with pytest.raises(ConfigError):
                CHANNELS["discord"].clean({"webhook_url": bad})

    def test_mentions_are_disabled(self, served):
        evil = {**PAYLOAD, "title": "@everyone is DOWN"}
        CHANNELS["discord"].send({"webhook_url": DISCORD}, evil, 1)
        body = json.loads(served.requests[0].content)
        assert body["allowed_mentions"] == {"parse": []}
        assert body["embeds"][0]["color"] == 0xDC2626

    def test_204_is_success_and_errors_are_classified(self, served):
        served.state["status"] = 204
        CHANNELS["discord"].send({"webhook_url": DISCORD}, PAYLOAD, 1)
        served.state["status"] = 404
        with pytest.raises(DeliveryError) as exc:
            CHANNELS["discord"].send({"webhook_url": DISCORD}, PAYLOAD, 1)
        assert exc.value.retryable is False and "abcDEF" not in exc.value.message

    def test_the_url_is_masked(self):
        assert "123456789" not in json.dumps(CHANNELS["discord"].public({"webhook_url": DISCORD}))


class TestTelegram:
    def test_validates_token_and_chat(self):
        assert CHANNELS["telegram"].clean({"bot_token": TOKEN, "chat_id": "-100123"})
        assert CHANNELS["telegram"].clean({"bot_token": TOKEN, "chat_id": "@mychannel"})
        with pytest.raises(ConfigError) as exc:
            CHANNELS["telegram"].clean({"bot_token": "abc", "chat_id": "hello world"})
        assert set(exc.value.errors) == {"bot_token", "chat_id"}

    def test_the_token_is_masked(self):
        shown = CHANNELS["telegram"].public({"bot_token": TOKEN, "chat_id": "5"})
        assert shown["bot_token"] == "123456789:••••" and "AAAA" not in json.dumps(shown)

    def test_sends_html_escaped_text(self, served):
        evil = {**PAYLOAD, "title": "<b>x</b> & more is DOWN"}
        CHANNELS["telegram"].send({"bot_token": TOKEN, "chat_id": "5"}, evil, 1)
        request = served.requests[0]
        assert f"/bot{TOKEN}/sendMessage" in str(request.url)
        body = json.loads(request.content)
        assert body["chat_id"] == "5" and body["parse_mode"] == "HTML"
        assert "&lt;b&gt;x&lt;/b&gt; &amp; more" in body["text"]

    def test_bad_chat_is_permanent_with_telegrams_own_words(self, served):
        served.state.update(
            status=400, body={"ok": False, "description": "Bad Request: chat not found"}
        )
        with pytest.raises(DeliveryError) as exc:
            CHANNELS["telegram"].send({"bot_token": TOKEN, "chat_id": "5"}, PAYLOAD, 1)
        assert exc.value.retryable is False and "chat not found" in exc.value.message
        assert TOKEN not in exc.value.message

    def test_rate_limits_use_telegrams_retry_after(self, served):
        served.state.update(
            status=429, body={"ok": False, "parameters": {"retry_after": 12}, "description": "slow"}
        )
        with pytest.raises(DeliveryError) as exc:
            CHANNELS["telegram"].send({"bot_token": TOKEN, "chat_id": "5"}, PAYLOAD, 1)
        assert exc.value.retryable and exc.value.retry_after == 12

    def test_server_errors_retry(self, served):
        served.state.update(status=502, body={})
        with pytest.raises(DeliveryError) as exc:
            CHANNELS["telegram"].send({"bot_token": TOKEN, "chat_id": "5"}, PAYLOAD, 1)
        assert exc.value.retryable

    def test_discovers_the_chats_that_messaged_the_bot(self):
        updates = {
            "ok": True,
            "result": [
                {
                    "message": {
                        "chat": {"id": 11, "type": "private", "first_name": "Ada", "last_name": "L"}
                    }
                },
                {"message": {"chat": {"id": 11, "type": "private", "first_name": "Ada"}}},
                {"channel_post": {"chat": {"id": -100, "type": "channel", "title": "Alerts"}}},
                {"edited_message": {"chat": {"id": 99}}},
            ],
        }
        transport = httpx.MockTransport(lambda r: httpx.Response(200, json=updates))
        found = channels.discover_telegram_chats(TOKEN, transport=transport)
        assert found == [
            {"id": "11", "type": "private", "title": "Ada L"},
            {"id": "-100", "type": "channel", "title": "Alerts"},
        ]

    def test_discovery_reports_a_bad_token(self):
        transport = httpx.MockTransport(
            lambda r: httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
        )
        with pytest.raises(ConfigError) as exc:
            channels.discover_telegram_chats(TOKEN, transport=transport)
        assert "bot_token" in exc.value.errors
        with pytest.raises(ConfigError):
            channels.discover_telegram_chats("nonsense")


SUB = {
    "endpoint": "https://fcm.googleapis.com/fcm/send/abcdef",
    "keys": {"p256dh": "B" * 87, "auth": "a" * 22},
}


class TestPush:
    def test_accepts_only_real_push_services(self):
        cleaned = CHANNELS["push"].clean({"subscription": SUB, "device": "Laptop"})
        assert cleaned["subscription"]["endpoint"] == SUB["endpoint"]
        for endpoint in (
            "https://internal.example.com/push",
            "http://fcm.googleapis.com/x",
            "https://fcm.googleapis.com.evil.com/x",
            "https://169.254.169.254/x",
        ):
            with pytest.raises(ConfigError):
                CHANNELS["push"].clean({"subscription": {**SUB, "endpoint": endpoint}})

    def test_requires_the_keys(self):
        with pytest.raises(ConfigError):
            CHANNELS["push"].clean({"subscription": {"endpoint": SUB["endpoint"], "keys": {}}})
        with pytest.raises(ConfigError):
            CHANNELS["push"].clean({})

    def test_unconfigured_server_fails_permanently(self, settings):
        settings.VAPID_PRIVATE_KEY = ""
        with pytest.raises(DeliveryError) as exc:
            CHANNELS["push"].send({"subscription": SUB}, PAYLOAD, 1)
        assert exc.value.retryable is False

    def test_sends_through_webpush(self, settings, monkeypatch):
        settings.VAPID_PRIVATE_KEY = "private"
        settings.VAPID_SUBJECT = "mailto:me@example.com"
        calls = []
        monkeypatch.setattr(channels, "webpush", lambda **kw: calls.append(kw))
        CHANNELS["push"].send({"subscription": SUB}, PAYLOAD, 1)
        assert calls[0]["subscription_info"] == SUB
        assert calls[0]["vapid_claims"] == {"sub": "mailto:me@example.com"}
        assert json.loads(calls[0]["data"])["title"] == "Shop is DOWN"

    @pytest.mark.parametrize(
        ("status", "retryable", "disables"),
        [
            (410, False, True),
            (404, False, True),
            (503, True, False),
            (429, True, False),
            (400, False, False),
        ],
    )
    def test_push_service_errors(self, settings, monkeypatch, status, retryable, disables):
        settings.VAPID_PRIVATE_KEY = "private"

        def fail(**kw):
            raise channels.WebPushException("nope", response=httpx.Response(status))

        monkeypatch.setattr(channels, "webpush", fail)
        with pytest.raises(DeliveryError) as exc:
            CHANNELS["push"].send({"subscription": SUB}, PAYLOAD, 1)
        assert exc.value.retryable is retryable
        assert bool(getattr(exc.value, "disable_contact", False)) is disables


def test_every_channel_is_registered():
    assert set(CHANNELS) == {"email", "webhook", "slack", "discord", "telegram", "push"}
