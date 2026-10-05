"""Read every provider whose status page is due (one pass). In production this runs inside the
signed scheduler trigger; run it by hand, or use `run_engine --loop` for local development."""

from django.core.management.base import BaseCommand

from providers.poller import run_provider_polls_once


class Command(BaseCommand):
    help = "Poll the providers' status pages that are due."

    def handle(self, *args, **options):
        s = run_provider_polls_once().as_dict()
        self.stdout.write(
            f"claimed={s['claimed']} ok={s['ok']} failed={s['failed']} "
            f"new_incidents={s['new_incidents']} alerts={s['alerts']} ({s['duration_ms']} ms)"
        )
