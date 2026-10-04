"""One full engine pass: run the due checks, then send the alerts they caused.

This is what the signed /internal/run-due-checks/ trigger does in production. Locally,
`python manage.py run_engine --loop 60` keeps monitoring and alerting going.
"""

import time

from django.core.management.base import BaseCommand
from django.db import OperationalError, connections

from monitoring.engine import run_scheduler_once
from notifications.dispatcher import run_notifications_once
from notifications.integrations import poll_once


class Command(BaseCommand):
    help = "Run due checks, then send due alerts."

    def add_arguments(self, parser):
        parser.add_argument(
            "--loop", type=int, metavar="SECONDS", help="Repeat until stopped (development only)."
        )

    def handle(self, *args, **options):
        while True:
            try:
                checks = run_scheduler_once().as_dict()
                alerts = run_notifications_once().as_dict()
                poll_once()  # development: pick up "Connect Telegram" presses (no-op in production)
            except OperationalError as exc:
                if not options.get("loop"):
                    raise
                self.stderr.write(f"pass failed, will retry: {exc}")
            else:
                self.stdout.write(
                    f"checks: claimed={checks['claimed']} up={checks['up']} down={checks['down']} "
                    f"blips={checks['blips']} | alerts: sent={alerts['sent']} "
                    f"retrying={alerts['retrying']} failed={alerts['failed']} "
                    f"skipped={alerts['skipped']}"
                )
            if not options.get("loop"):
                return
            connections.close_all()  # a hosted database drops idle connections
            time.sleep(options["loop"])
