"""One-click connections for Telegram, Discord and Slack.

Pasting webhook URLs and bot tokens is how developers connect things; this is the shortcut for
everyone else. None of it is required: each channel only offers "Connect" when the deployment
has its credentials, and the paste-it-yourself route always remains.

* **Telegram** uses one bot owned by this deployment. The person opens a link containing a
  one-time token and presses Start; the bot receives `/start <token>` from their chat, which
  is how we know whose chat it is. No token or chat id is ever typed.
* **Discord and Slack** use OAuth: the person approves on Discord's/Slack's own page, picks a
  channel, and the provider hands back an incoming-webhook URL, which becomes an ordinary
  contact (so it is masked, tested and removed like any other).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlencode

import httpx
from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone

from accounts.models import User
from monitoring.models import AlertContact, Monitor, MonitorAlertContact

from .channels import CHANNELS, ConfigError
from .http import DeliveryError, post_json
from .models import BotState, IntegrationLink

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
STATE_SALT = "rootpulse.integrations.state"
STATE_MAX_AGE = 10 * 60
POLL_STATE_KEY = "telegram_update_offset"

OAUTH_PROVIDERS = ("discord", "slack")


class IntegrationError(Exception):
    """Something went wrong while connecting. `code` is a short, safe reason the frontend maps
    to words; nothing from the provider's reply is ever shown or logged with secrets."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# --- what this deployment offers --------------------------------------------------------------


def telegram_available() -> bool:
    return bool(settings.TELEGRAM_BOT_TOKEN)


def oauth_available(provider: str) -> bool:
    if provider == "discord":
        return bool(settings.DISCORD_CLIENT_ID and settings.DISCORD_CLIENT_SECRET)
    if provider == "slack":
        return bool(settings.SLACK_CLIENT_ID and settings.SLACK_CLIENT_SECRET)
    return False


_bot_username: dict[str, str] = {}


def bot_username() -> str:
    """The bot's @name (for the t.me link). From settings, else asked of Telegram once."""
    if settings.TELEGRAM_BOT_USERNAME:
        return settings.TELEGRAM_BOT_USERNAME
    token = settings.TELEGRAM_BOT_TOKEN
    if token in _bot_username:
        return _bot_username[token]
    result = _telegram_call("getMe", {})
    username = (result or {}).get("username", "")
    if username:
        _bot_username[token] = username
    return username


def availability() -> dict:
    telegram = telegram_available()
    username = ""
    if telegram:
        try:
            username = bot_username()
        except DeliveryError:
            username = ""
    return {
        "telegram": {"available": telegram and bool(username), "bot_username": username or None},
        "discord": {"available": oauth_available("discord")},
        "slack": {"available": oauth_available("slack")},
    }


def redirect_uri(provider: str) -> str:
    return f"{settings.API_PUBLIC_URL}/api/v1/integrations/{provider}/callback/"


def frontend_result(provider: str, error: str | None = None) -> str:
    base = f"{settings.FRONTEND_URL}/settings/notifications"
    if error:
        return f"{base}?{urlencode({'connect_error': error, 'provider': provider})}"
    return f"{base}?{urlencode({'connected': provider})}"


# --- turning a connection into a contact --------------------------------------------------------


def _attach_to_all_monitors(user: User, contact: AlertContact) -> None:
    MonitorAlertContact.objects.bulk_create(
        [
            MonitorAlertContact(monitor=m, alert_contact=contact)
            for m in Monitor.objects.filter(user=user)
        ],
        ignore_conflicts=True,
    )


def _check_room(user: User) -> None:
    if AlertContact.objects.filter(user=user).count() >= settings.MAX_ALERT_CONTACTS_PER_USER:
        raise IntegrationError("limit")


def create_connected_contact(
    user: User, channel: str, name: str, config: dict, *, trusted_config: bool = False
) -> AlertContact:
    """Save a contact that came from a provider, and use it for the user's existing monitors
    (connecting is meant to be the whole setup). `config` is validated like anything typed in,
    except for Telegram chats, whose config only this module can produce."""
    _check_room(user)
    if not trusted_config:
        try:
            config = CHANNELS[channel].clean(config)
        except ConfigError as exc:
            logger.warning("connected %s config rejected: %s", channel, exc.errors)
            raise IntegrationError("invalid") from exc
    with transaction.atomic():
        contact = AlertContact.objects.create(
            user=user, name=name[:120], channel=channel, config=config
        )
        _attach_to_all_monitors(user, contact)
    return contact


# --- Telegram -------------------------------------------------------------------------------------


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _telegram_call(method: str, payload: dict) -> dict | None:
    """Call the deployment's bot. Returns Telegram's `result`, or None if it said no."""
    response = post_json(
        f"{TELEGRAM_API}/bot{settings.TELEGRAM_BOT_TOKEN}/{method}",
        json.dumps(payload).encode(),
        {"Content-Type": "application/json"},
    )
    if response.status_code == 409:  # a webhook is registered, so getUpdates is refused
        return None
    if response.status_code != 200:
        raise DeliveryError(f"Telegram answered {response.status_code}", retryable=True)
    return response.json().get("result")


def start_telegram_link(user: User) -> tuple[IntegrationLink, str]:
    """A fresh one-time link. Returns it and the raw token (which is never stored)."""
    token = secrets.token_urlsafe(18)
    link = IntegrationLink.objects.create(
        user=user,
        provider=IntegrationLink.Provider.TELEGRAM,
        token_hash=_digest(token),
        expires_at=timezone.now() + timedelta(seconds=settings.INTEGRATION_LINK_TTL_SECONDS),
    )
    return link, token


def telegram_urls(token: str) -> dict[str, str]:
    name = bot_username()
    return {
        "url": f"https://t.me/{name}?start={token}",
        "group_url": f"https://t.me/{name}?startgroup={token}",
    }


START_RE = re.compile(r"^/start(?:@\w+)?\s+([A-Za-z0-9_-]{8,64})\s*$")


def _say(chat_id: int, text: str) -> None:
    try:
        _telegram_call("sendMessage", {"chat_id": chat_id, "text": text})
    except DeliveryError:
        logger.warning("couldn't reply in a Telegram chat")


def _chat_title(chat: dict) -> str:
    return (
        chat.get("title")
        or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
        or chat.get("username")
        or "Telegram chat"
    )


def connect_telegram(token: str, chat: dict) -> str:
    """Complete a link from `/start <token>`. Returns what to tell the person in the chat."""
    link = IntegrationLink.objects.filter(token_hash=_digest(token)).select_related("user").first()
    if link is None or link.expires_at < timezone.now():
        return "That link has expired. Go back to RootPulse and press Connect Telegram again."
    if link.consumed_at is not None:
        return "That link was already used. If you want another chat, start a new connection."
    chat_id = str(chat["id"])
    title = _chat_title(chat)
    user = link.user

    with transaction.atomic():
        claimed = IntegrationLink.objects.filter(pk=link.pk, consumed_at__isnull=True).update(
            consumed_at=timezone.now()
        )
        if not claimed:  # a second delivery of the same update
            return "That link was already used."
        existing = next(
            (
                c
                for c in AlertContact.objects.filter(user=user, channel="telegram")
                if c.config.get("managed") and c.config.get("chat_id") == chat_id
            ),
            None,
        )
        if existing:
            existing.enabled = True
            existing.save(update_fields=["enabled"])
            contact = existing
        else:
            try:
                contact = create_connected_contact(
                    user,
                    "telegram",
                    f"Telegram: {title}",
                    {"managed": True, "chat_id": chat_id, "chat_title": title},
                    trusted_config=True,
                )
            except IntegrationError:
                IntegrationLink.objects.filter(pk=link.pk).update(consumed_at=None)
                return "You've reached your limit of alert contacts. Delete one in RootPulse first."
        IntegrationLink.objects.filter(pk=link.pk).update(contact=contact)
    return "Connected to RootPulse. Alerts for your monitors will arrive in this chat."


def handle_update(update: dict) -> None:
    """One Telegram update. Only `/start <token>` in a message does anything."""
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    text = (message.get("text") or "").strip()
    if not chat.get("id") or not text.startswith("/start"):
        return
    match = START_RE.match(text)
    if match:
        _say(chat["id"], connect_telegram(match.group(1), chat))
    else:
        _say(
            chat["id"],
            "Hi! To get RootPulse alerts here, open RootPulse, go to Settings, then "
            "Notifications, and press Connect Telegram.",
        )


def poll_enabled() -> bool:
    """Local development: ask Telegram for updates ourselves. With a webhook secret set
    (production), Telegram pushes them to us instead."""
    return telegram_available() and not settings.TELEGRAM_WEBHOOK_SECRET


def poll_once() -> int:
    """Fetch and handle waiting Telegram updates. Returns how many were handled."""
    if not poll_enabled():
        return 0
    if not cache.add("telegram-poll-lock", 1, 3):  # another request/loop is already on it
        return 0
    state, _ = BotState.objects.get_or_create(key=POLL_STATE_KEY)
    try:
        updates = _telegram_call(
            "getUpdates",
            {"offset": state.value, "timeout": 0, "limit": 50, "allowed_updates": ["message"]},
        )
    except DeliveryError:
        return 0
    handled = 0
    for update in updates or []:
        try:
            handle_update(update)
        except Exception:  # noqa: BLE001 - one bad update mustn't wedge the queue
            logger.exception("telegram update failed")
        state.value = max(state.value, int(update["update_id"]) + 1)
        handled += 1
    if handled:
        state.save(update_fields=["value"])
    return handled


def link_state(link: IntegrationLink) -> str:
    if link.consumed_at is not None and link.contact_id:
        return "connected"
    if link.expires_at < timezone.now():
        return "expired"
    return "pending"


# --- Discord and Slack (OAuth) ------------------------------------------------------------------


def make_state(user: User, provider: str) -> str:
    return signing.dumps(
        {"u": str(user.pk), "p": provider, "n": secrets.token_hex(4)}, salt=STATE_SALT
    )


def read_state(state: str, provider: str) -> User | None:
    try:
        data = signing.loads(state, salt=STATE_SALT, max_age=STATE_MAX_AGE)
    except signing.BadSignature:
        return None
    if data.get("p") != provider:
        return None
    return User.objects.filter(pk=data.get("u"), is_active=True).first()


def authorize_url(provider: str, user: User) -> str:
    state = make_state(user, provider)
    if provider == "discord":
        return "https://discord.com/oauth2/authorize?" + urlencode(
            {
                "client_id": settings.DISCORD_CLIENT_ID,
                "response_type": "code",
                "scope": "webhook.incoming",
                "redirect_uri": redirect_uri("discord"),
                "state": state,
            }
        )
    return "https://slack.com/oauth/v2/authorize?" + urlencode(
        {
            "client_id": settings.SLACK_CLIENT_ID,
            "scope": "incoming-webhook",
            "redirect_uri": redirect_uri("slack"),
            "state": state,
        }
    )


@dataclass(frozen=True)
class Granted:
    name: str
    webhook_url: str


def _exchange(provider: str, code: str) -> dict:
    if provider == "discord":
        url = "https://discord.com/api/oauth2/token"
        data = {
            "client_id": settings.DISCORD_CLIENT_ID,
            "client_secret": settings.DISCORD_CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri("discord"),
        }
    else:
        url = "https://slack.com/api/oauth.v2.access"
        data = {
            "client_id": settings.SLACK_CLIENT_ID,
            "client_secret": settings.SLACK_CLIENT_SECRET,
            "code": code,
            "redirect_uri": redirect_uri("slack"),
        }
    try:
        with httpx.Client(timeout=settings.NOTIFY_HTTP_TIMEOUT, follow_redirects=False) as client:
            response = client.post(url, data=data, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:  # never include the exception text: it can carry the code
        raise IntegrationError("failed") from exc
    if response.status_code != 200:
        raise IntegrationError("failed")
    try:
        return response.json()
    except ValueError as exc:
        raise IntegrationError("failed") from exc


def redeem(provider: str, code: str) -> Granted:
    body = _exchange(provider, code)
    if provider == "discord":
        hook = body.get("webhook") or {}
        url = hook.get("url")
        if not url:
            raise IntegrationError("failed")
        return Granted(name="Discord", webhook_url=url)
    if not body.get("ok"):
        raise IntegrationError("failed")
    hook = body.get("incoming_webhook") or {}
    url = hook.get("url")
    if not url:
        raise IntegrationError("failed")
    team = (body.get("team") or {}).get("name", "")
    channel = hook.get("channel", "")
    label = " ".join(filter(None, [channel, f"({team})" if team else ""]))
    return Granted(name=f"Slack: {label}" if label else "Slack", webhook_url=url)


def finish_oauth(provider: str, user: User, code: str) -> AlertContact:
    granted = redeem(provider, code)
    try:
        return create_connected_contact(
            user, provider, granted.name, {"webhook_url": granted.webhook_url}
        )
    except IntegrityError as exc:  # pragma: no cover - defensive
        raise IntegrationError("failed") from exc
