"""Create or refresh the curated provider list. Runs automatically after `migrate`; this is
for doing it by hand. It never touches a provider's status or polling state."""

from django.core.management.base import BaseCommand

from providers.catalog import sync_catalog


class Command(BaseCommand):
    help = "Create or update the curated list of providers."

    def handle(self, *args, **options):
        self.stdout.write(f"{sync_catalog()} providers in the catalogue.")
