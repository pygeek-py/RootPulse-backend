"""The six alert channels: how each is configured, shown, and sent.

Every channel validates its own config (so a typo is caught when the contact is saved,
not at 3 a.m.), hides its secrets when shown back, and turns a failure into either
"try again later" or "this will never work" (`DeliveryError.retryable`).

Error messages never contain URLs or tokens: for Slack, Discord and webhooks the URL
*is* the credential.
"""

from __future__ import annotations

import html
import json
import re
import secrets
from typing import Any
from urllib.parse import urlsplit

import httpx
from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.mail import send_mail
from django.core.validators import validate_email

from monitoring import signing
from monitoring import target_validation as tv

from .http import DeliveryError, classify_status, post_json, safe_post

try:  # imported lazily in tests that don't need push
    from pywebpush import WebPushException, webpush
except ImportError:  # pragma: no cover
    webpush = None
    WebPushException = Exception

COLORS = {"down": "#dc2626", "up": "#16a34a", "test": "#6c5ce7"}
DISCORD_COLORS = {"down": 0xDC2626, "up": 0x16A34A, "test": 0x6C5CE7}
EVENT_NAMES = {
    "opened": "incident.opened",
    "reminder": "incident.reminder",
    "resolved": "incident.resolved",
    "test": "test",
}


class ConfigError(ValueError):
    """Invalid channel config. `errors` maps a config field to a message."""

    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(errors.values()))
        self.errors = errors


def _require(config: dict, *keys: str) -> None:
    missing = {k: "This is required." for k in keys if not str(config.get(k, "")).strip()}
    if missing:
        raise ConfigError(missing)


def _no_extras(config: dict, allowed: set[str]) -> None:
    extra = set(config) - allowed
    if extra:
        raise ConfigError({k: "Not a setting for this channel." for k in sorted(extra)})


class Channel:
    key = ""
    label = ""
    #: A contact of this channel only gets alerts once it has been verified.
    needs_verification = False

    def clean(
        self, config: dict[str, Any], *, previous: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        raise NotImplementedError

    def summary(self, config: dict[str, Any]) -> str:
        raise NotImplementedError

    def public(self, config: dict[str, Any]) -> dict[str, Any]:
        """The config as shown back to the browser: secrets removed."""
        return {}

    def send(self, config: dict[str, Any], payload: dict[str, Any], delivery_id: int) -> None:
        raise NotImplementedError


# --- email ---------------------------------------------------------------------------------


class EmailChannel(Channel):
    key, label = "email", "Email"

    def clean(self, config, *, previous=None):
        _no_extras(config, {"address"})
        _require(config, "address")
        address = str(config["address"]).strip().lower()
        try:
            validate_email(address)
        except DjangoValidationError as exc:
            raise ConfigError({"address": "Enter a valid email address."}) from exc
        return {"address": address}

    def summary(self, config):
        return config.get("address", "")

    def public(self, config):
        return {"address": config.get("address", "")}

    def send(self, config, payload, delivery_id):
        subject = f"[{payload['severity'].upper()}] {payload['title']}"
        lines = [payload["title"], "", payload["summary"], ""]
        if payload["monitor"]["target"]:
            lines.append(f"Target: {payload['monitor']['target']}")
        lines.append(f"Details: {payload['url']}")
        body_html = (
            f"<p><strong>{html.escape(payload['title'])}</strong></p>"
            f"<p>{html.escape(payload['summary'])}</p>"
            f"<p><a href=\"{html.escape(payload['url'], quote=True)}\">View details</a></p>"
        )
        try:
            send_mail(
                subject,
                "\n".join(lines),
                settings.DEFAULT_FROM_EMAIL,
                [config["address"]],
                html_message=body_html,
            )
        except Exception as exc:  # noqa: BLE001 - classify below
            import smtplib

            refused = isinstance(exc, smtplib.SMTPRecipientsRefused)
            raise DeliveryError(
                "The mail server refused that address" if refused else "Couldn't send the email",
                retryable=not refused,
            ) from exc


# --- webhook ----------------------------------------------------------------------------------


class WebhookChannel(Channel):
    key, label = "webhook", "Webhook"

    def clean(self, config, *, previous=None):
        _no_extras(config, {"url", "secret"})
        _require(config, "url")
        try:
            url = tv.validate_http_url(str(config["url"]))
        except tv.TargetRejected as exc:
            raise ConfigError({"url": str(exc)}) from exc
        # The signing secret is generated here, never chosen by the user: they copy it
        # from the response and the receiver uses it to verify each request.
        secret = (previous or {}).get("secret") or secrets.token_urlsafe(32)
        return {"url": url, "secret": secret}

    def summary(self, config):
        return urlsplit(config.get("url", "")).hostname or ""

    def public(self, config):
        return {"host": self.summary(config)}

    def send(self, config, payload, delivery_id):
        body = json.dumps(
            {
                "id": delivery_id,
                "event": EVENT_NAMES[payload["event"]],
                "created_at": payload["created_at"],
                "title": payload["title"],
                "summary": payload["summary"],
                "url": payload["url"],
                "monitor": payload["monitor"],
                "incident": payload["incident"],
            },
            separators=(",", ":"),
        ).encode()
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "RootPulse-Webhook/1.0",
            "X-RootPulse-Event": EVENT_NAMES[payload["event"]],
            "X-RootPulse-Delivery": str(delivery_id),
            # The same scheme the API uses internally: HMAC-SHA256 over "<t>." + body.
            signing.HEADER: signing.sign(config["secret"], body),
        }
        status, _ = safe_post(config["url"], body, headers)
        if not 200 <= status < 300:
            raise classify_status(status, self.summary(config))


# --- slack ------------------------------------------------------------------------------------

SLACK_URL = re.compile(r"^https://hooks\.slack\.com/(services|workflows|triggers)/[A-Za-z0-9/_-]+$")


def slack_escape(text: str) -> str:
    """Slack treats <...> as links and mentions; neutralise anything a monitor name could inject."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class SlackChannel(Channel):
    key, label = "slack", "Slack"

    def clean(self, config, *, previous=None):
        _no_extras(config, {"webhook_url"})
        _require(config, "webhook_url")
        url = str(config["webhook_url"]).strip()
        if not SLACK_URL.match(url):
            raise ConfigError(
                {"webhook_url": "That isn't a Slack incoming-webhook URL (hooks.slack.com/…)."}
            )
        return {"webhook_url": url}

    def summary(self, config):
        return "hooks.slack.com/…" + config.get("webhook_url", "")[-6:]

    def public(self, config):
        return {"webhook_url": self.summary(config)}

    def send(self, config, payload, delivery_id):
        title, text = slack_escape(payload["title"]), slack_escape(payload["summary"])
        blocks: list[dict[str, Any]] = [
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*{title}*\n{text}"}},
        ]
        if payload["monitor"]["target"]:
            blocks.append(
                {
                    "type": "context",
                    "elements": [
                        {"type": "mrkdwn", "text": slack_escape(payload["monitor"]["target"])}
                    ],
                }
            )
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "View details"},
                        "url": payload["url"],
                    }
                ],
            }
        )
        body = json.dumps(
            {
                "text": f"{title}: {text}",  # notification fallback (also parsed as mrkdwn)
                "attachments": [{"color": COLORS[payload["severity"]], "blocks": blocks}],
            }
        ).encode()
        response = post_json(config["webhook_url"], body, {"Content-Type": "application/json"})
        if response.status_code != 200:
            raise classify_status(
                response.status_code, "Slack", response.headers.get("retry-after")
            )


# --- discord ----------------------------------------------------------------------------------

DISCORD_URL = re.compile(
    r"^https://(?:canary\.|ptb\.)?(?:discord|discordapp)\.com/api(?:/v\d+)?/webhooks/\d+/[A-Za-z0-9_-]+$"
)


class DiscordChannel(Channel):
    key, label = "discord", "Discord"

    def clean(self, config, *, previous=None):
        _no_extras(config, {"webhook_url"})
        _require(config, "webhook_url")
        url = str(config["webhook_url"]).strip()
        if not DISCORD_URL.match(url):
            raise ConfigError(
                {"webhook_url": "That isn't a Discord webhook URL (discord.com/api/webhooks/…)."}
            )
        return {"webhook_url": url}

    def summary(self, config):
        return "discord.com/…" + config.get("webhook_url", "")[-6:]

    def public(self, config):
        return {"webhook_url": self.summary(config)}

    def send(self, config, payload, delivery_id):
        embed: dict[str, Any] = {
            "title": payload["title"][:256],
            "description": payload["summary"][:2000],
            "url": payload["url"],
            "color": DISCORD_COLORS[payload["severity"]],
            "timestamp": payload["created_at"],
        }
        if payload["monitor"]["target"]:
            embed["fields"] = [{"name": "Target", "value": payload["monitor"]["target"][:1000]}]
        body = json.dumps(
            {
                "username": "RootPulse",
                "embeds": [embed],
                # A monitor named "@everyone" must not ping the whole server.
                "allowed_mentions": {"parse": []},
            }
        ).encode()
        response = post_json(config["webhook_url"], body, {"Content-Type": "application/json"})
        if response.status_code not in (200, 204):
            raise classify_status(
                response.status_code, "Discord", response.headers.get("retry-after")
            )


# --- telegram ---------------------------------------------------------------------------------

TELEGRAM_TOKEN = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")
TELEGRAM_CHAT = re.compile(r"^(-?\d{1,20}|@[A-Za-z][A-Za-z0-9_]{4,31})$")
TELEGRAM_API = "https://api.telegram.org"


def _telegram_failure(response: httpx.Response) -> DeliveryError:
    """Telegram describes failures in the JSON body; its `description` never contains the token."""
    try:
        data = response.json()
    except ValueError:
        return classify_status(response.status_code, "Telegram")
    description = str(data.get("description", "")).strip()[:150]
    params = data.get("parameters") or {}
    if response.status_code == 429:
        return DeliveryError(
            "Telegram is rate limiting us", retryable=True, retry_after=params.get("retry_after")
        )
    if response.status_code >= 500:
        return DeliveryError("Telegram had a server error", retryable=True)
    # 400 (bad chat id), 401 (bad token), 403 (bot blocked/kicked), 404: none will fix themselves.
    return DeliveryError(f"Telegram: {description or response.status_code}", retryable=False)


class TelegramChannel(Channel):
    key, label = "telegram", "Telegram"

    def clean(self, config, *, previous=None):
        _no_extras(config, {"bot_token", "chat_id"})
        _require(config, "bot_token", "chat_id")
        token, chat = str(config["bot_token"]).strip(), str(config["chat_id"]).strip()
        errors = {}
        if not TELEGRAM_TOKEN.match(token):
            errors["bot_token"] = (
                "That doesn't look like a bot token (123456:ABC…). Get one from @BotFather."
            )
        if not TELEGRAM_CHAT.match(chat):
            errors["chat_id"] = (
                "Use a numeric chat id (like 123456789 or -100123…) or @channelname."
            )
        if errors:
            raise ConfigError(errors)
        return {"bot_token": token, "chat_id": chat}

    def summary(self, config):
        return f"Chat {config.get('chat_id', '')}"

    def public(self, config):
        token = config.get("bot_token", "")
        return {"bot_token": token.split(":")[0] + ":••••", "chat_id": config.get("chat_id", "")}

    def send(self, config, payload, delivery_id):
        text = (
            f"<b>{html.escape(payload['title'])}</b>\n{html.escape(payload['summary'])}"
            + (
                f"\n{html.escape(payload['monitor']['target'])}"
                if payload["monitor"]["target"]
                else ""
            )
            + f"\n<a href=\"{html.escape(payload['url'], quote=True)}\">View details</a>"
        )
        body = json.dumps(
            {
                "chat_id": config["chat_id"],
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
        ).encode()
        response = post_json(
            f"{TELEGRAM_API}/bot{config['bot_token']}/sendMessage",
            body,
            {"Content-Type": "application/json"},
        )
        if response.status_code != 200:
            raise _telegram_failure(response)


def discover_telegram_chats(
    bot_token: str, *, transport: httpx.BaseTransport | None = None
) -> list[dict]:
    """Find the chats that have recently messaged this bot, so the user can pick theirs
    instead of hunting for a numeric id. (They message the bot first; Telegram only lets a
    bot see chats that did.)"""
    if not TELEGRAM_TOKEN.match(bot_token):
        raise ConfigError({"bot_token": "That doesn't look like a bot token."})
    response = post_json(
        f"{TELEGRAM_API}/bot{bot_token}/getUpdates",
        json.dumps({"limit": 100, "timeout": 0}).encode(),
        {"Content-Type": "application/json"},
        transport=transport,
    )
    if response.status_code != 200:
        error = _telegram_failure(response)
        raise ConfigError({"bot_token": error.message})
    chats: dict[int, dict] = {}
    for update in response.json().get("result", []):
        message = (
            update.get("message")
            or update.get("channel_post")
            or update.get("my_chat_member")
            or {}
        )
        chat = message.get("chat")
        if chat and chat.get("id") not in chats:
            chats[chat["id"]] = {
                "id": str(chat["id"]),
                "type": chat.get("type", "private"),
                "title": chat.get("title")
                or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
                or chat.get("username")
                or "Chat",
            }
    return list(chats.values())


# --- web push ---------------------------------------------------------------------------------

# Push endpoints are chosen by the browser, so a malicious client could point one at an
# internal address. Only the real push services are accepted (and still vetted as public).
PUSH_HOSTS = (
    "fcm.googleapis.com",
    "android.googleapis.com",
    "push.services.mozilla.com",
    "notify.windows.com",
    "push.apple.com",
)
_B64URL = re.compile(r"^[A-Za-z0-9_-]{16,200}={0,2}$")


class PushChannel(Channel):
    key, label = "push", "Web push"

    def clean(self, config, *, previous=None):
        _no_extras(config, {"subscription", "device"})
        subscription = config.get("subscription")
        if not isinstance(subscription, dict):
            raise ConfigError({"subscription": "Allow notifications in your browser first."})
        keys = subscription.get("keys") or {}
        endpoint = str(subscription.get("endpoint", ""))
        host = urlsplit(endpoint).hostname or ""
        if urlsplit(endpoint).scheme != "https" or not any(
            host == h or host.endswith("." + h) for h in PUSH_HOSTS
        ):
            raise ConfigError({"subscription": "That isn't a browser push endpoint."})
        if not (
            _B64URL.match(str(keys.get("p256dh", ""))) and _B64URL.match(str(keys.get("auth", "")))
        ):
            raise ConfigError({"subscription": "The push subscription is incomplete."})
        device = str(config.get("device", "")).strip()[:80]
        return {
            "subscription": {
                "endpoint": endpoint,
                "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]},
            },
            "device": device,
        }

    def summary(self, config):
        return config.get("device") or "This browser"

    def public(self, config):
        return {"device": config.get("device", "")}

    def send(self, config, payload, delivery_id):
        if webpush is None or not settings.VAPID_PRIVATE_KEY:
            raise DeliveryError("Push notifications aren't set up on this server", retryable=False)
        data = json.dumps(
            {
                "title": payload["title"],
                "body": payload["summary"],
                "url": payload["url"],
                "tag": (payload["incident"] or payload["monitor"])["id"],
                "severity": payload["severity"],
            }
        )
        try:
            webpush(
                subscription_info=config["subscription"],
                data=data,
                vapid_private_key=settings.VAPID_PRIVATE_KEY,
                vapid_claims={"sub": settings.VAPID_SUBJECT},
                ttl=3600,
                timeout=settings.NOTIFY_HTTP_TIMEOUT,
            )
        except WebPushException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (404, 410):
                error = DeliveryError(
                    "This browser's push subscription has expired. Add the device again.",
                    retryable=False,
                )
                error.disable_contact = True  # type: ignore[attr-defined]
                raise error from exc
            if status == 429 or (status and status >= 500) or status is None:
                raise DeliveryError("The push service is unavailable", retryable=True) from exc
            raise DeliveryError(
                f"The push service rejected the message ({status})", retryable=False
            ) from exc


CHANNELS: dict[str, Channel] = {
    c.key: c
    for c in (
        EmailChannel(),
        WebhookChannel(),
        SlackChannel(),
        DiscordChannel(),
        TelegramChannel(),
        PushChannel(),
    )
}
