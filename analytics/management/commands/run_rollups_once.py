"""Fold recent checks into hourly, daily and fleet rollups, and delete raw checks past retention.

Safe to run as often as you like: every row is recomputed and replaced, never added to.

    python manage.py run_rollups_once          # the last couple of hours (what the cron does)
    python manage.py run_rollups_once --full   # rebuild everything from the first check
"""

from django.core.management.base import BaseCommand

from analytics.rollups import run_rollups_once


class Command(BaseCommand):
    help = "Build the analytics rollups."

    def add_arguments(self, parser):
        parser.add_argument("--full", action="store_true", help="Recompute from each first check.")
        parser.add_argument(
            "--no-prune", action="store_true", help="Keep raw checks past retention."
        )

    def handle(self, *args, **options):
        s = run_rollups_once(full=options["full"], prune=not options["no_prune"])
        self.stdout.write(
            f"monitors={s.monitors} hours={s.hours} days={s.days} fleet_days={s.fleet_days} "
            f"pruned={s.pruned} failed={s.failed}"
        )
