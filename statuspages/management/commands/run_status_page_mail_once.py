from django.core.management.base import BaseCommand

from statuspages.mail import run_status_page_mail_once


class Command(BaseCommand):
    help = "Send the queued status page emails (confirmations, incident and announcement updates)."

    def handle(self, *args, **options):
        d = run_status_page_mail_once().as_dict()
        self.stdout.write(
            f"claimed={d['claimed']} sent={d['sent']} retrying={d['retrying']} "
            f"failed={d['failed']} skipped={d['skipped']}"
        )
