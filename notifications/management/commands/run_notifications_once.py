"""Send every alert that is due (one pass). In production this runs inside the signed
scheduler trigger; run it by hand, or use `run_engine --loop` for local development."""

from django.core.management.base import BaseCommand

from notifications.dispatcher import run_notifications_once


class Command(BaseCommand):
    help = "Create due reminders and send every pending alert."

    def handle(self, *args, **options):
        d = run_notifications_once().as_dict()
        self.stdout.write(
            f"claimed={d['claimed']} sent={d['sent']} retrying={d['retrying']} "
            f"failed={d['failed']} skipped={d['skipped']} reminders={d['reminders_created']} "
            f"({d['duration_ms']} ms)"
        )
