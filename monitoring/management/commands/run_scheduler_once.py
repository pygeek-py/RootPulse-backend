"""Run one scheduler pass (or, for local development, repeat it).

In production the pass is triggered every five minutes by GitHub Actions through
/internal/run-due-checks/. Locally, run it by hand, or keep it going with --loop.
"""

import time

from django.core.management.base import BaseCommand
from django.db import OperationalError, connections

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
            try:
                summary = run_scheduler_once(batch_size=options.get("batch"))
            except OperationalError as exc:
                # A hosted database (Neon) drops idle connections; one failed pass must not
                # end a long-running loop. Without --loop there is nothing to retry: fail.
                if not options.get("loop"):
                    raise
                self.stderr.write(f"pass failed, will retry: {exc}")
            else:
                data = summary.as_dict()
                self.stdout.write(
                    f"claimed={data['claimed']} checked={data['checked']} up={data['up']} "
                    f"down={data['down']} blips={data['blips']} "
                    f"inconclusive={data['inconclusive']} errors={data['errors']} "
                    f"({data['duration_ms']} ms)"
                )
            if not options.get("loop"):
                return
            # Don't hold a connection open across the sleep: it would be stale by the next pass.
            connections.close_all()
            time.sleep(options["loop"])
