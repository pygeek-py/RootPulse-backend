"""Run one scheduler pass (or, for local development, repeat it).

In production the pass is triggered every five minutes by GitHub Actions through
/internal/run-due-checks/. Locally, run it by hand, or keep it going with --loop.
"""

import time

from django.core.management.base import BaseCommand

from monitoring.engine import run_scheduler_once


class Command(BaseCommand):
    help = "Claim and run every monitor check that is due."

    def add_arguments(self, parser):
        parser.add_argument(
            "--loop",
            type=int,
            metavar="SECONDS",
            help="Keep running a pass every SECONDS until stopped (development only).",
        )
        parser.add_argument("--batch", type=int, help="Maximum monitors to claim per pass.")

    def handle(self, *args, **options):
        while True:
            summary = run_scheduler_once(batch_size=options.get("batch"))
            data = summary.as_dict()
            self.stdout.write(
                f"claimed={data['claimed']} checked={data['checked']} up={data['up']} "
                f"down={data['down']} blips={data['blips']} "
                f"inconclusive={data['inconclusive']} errors={data['errors']} "
                f"({data['duration_ms']} ms)"
            )
            if not options.get("loop"):
                return
            time.sleep(options["loop"])
