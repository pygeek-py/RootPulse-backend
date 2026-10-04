"""Point Telegram at this API (production), or take it back (to poll again in development).

python manage.py telegram_webhook set      # needs TELEGRAM_BOT_TOKEN, TELEGRAM_WEBHOOK_SECRET
python manage.py telegram_webhook info
python manage.py telegram_webhook delete
"""

import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from notifications.http import post_json

API = "https://api.telegram.org"


def call(method: str, payload: dict) -> dict:
    response = post_json(
        f"{API}/bot{settings.TELEGRAM_BOT_TOKEN}/{method}",
        json.dumps(payload).encode(),
        {"Content-Type": "application/json"},
    )
    data = response.json()
    if response.status_code != 200 or not data.get("ok"):
        raise CommandError(f"Telegram said no: {data.get('description', response.status_code)}")
    return data["result"]


class Command(BaseCommand):
    help = "Register, inspect or remove the Telegram bot's webhook."

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["set", "info", "delete"])

    def handle(self, *args, action, **options):
        if not settings.TELEGRAM_BOT_TOKEN:
            raise CommandError("TELEGRAM_BOT_TOKEN isn't set.")
        if action == "set":
            if not settings.TELEGRAM_WEBHOOK_SECRET:
                raise CommandError("Set TELEGRAM_WEBHOOK_SECRET first (any long random string).")
            if settings.API_PUBLIC_URL.startswith("http://"):
                raise CommandError(
                    "Telegram only calls https URLs; set API_PUBLIC_URL to the API's https address."
                )
            url = f"{settings.API_PUBLIC_URL}/api/v1/integrations/telegram/webhook/"
            call(
                "setWebhook",
                {
                    "url": url,
                    "secret_token": settings.TELEGRAM_WEBHOOK_SECRET,
                    "allowed_updates": ["message"],
                    "drop_pending_updates": False,
                },
            )
            self.stdout.write(f"Webhook set: {url}")
        elif action == "delete":
            call("deleteWebhook", {})
            self.stdout.write("Webhook removed. Local polling works again.")
        else:
            info = call("getWebhookInfo", {})
            self.stdout.write(
                f"url={info.get('url') or '(none)'} pending={info.get('pending_update_count', 0)} "
                f"last_error={info.get('last_error_message') or '(none)'}"
            )
