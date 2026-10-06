"""Can every migration be undone, and redone? (Phase 17: "can a bad migration be reverted
cleanly".)

For each of our apps, in reverse dependency order, migrate it back to nothing; then migrate
everything forward again and confirm the schema matches the models (no drift).

THIS DROPS EVERY TABLE OF THE DATABASE IT RUNS AGAINST. It refuses to run unless that database is
SQLite or is plainly a scratch database (its name starts with "test_" or contains "scratch"), and
it never takes a DATABASE_URL from you on the command line: point DATABASE_URL (or DB_NAME) at a
scratch database yourself.

    DATABASE_URL=sqlite:///scratch.db python manage.py check_migrations_reversible
"""

from django.apps import apps
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.migrations.loader import MigrationLoader

OUR_APPS = [
    "accounts",
    "monitoring",
    "incidents",
    "notifications",
    "analytics",
    "deploys",
    "providers",
    "statuspages",
    "reports",
    "apikeys",
]


def is_scratch() -> bool:
    db = settings.DATABASES["default"]
    if db["ENGINE"].endswith("sqlite3"):
        return True
    name = str(db.get("NAME", "")).lower()
    return name.startswith("test_") or "scratch" in name


class Command(BaseCommand):
    help = "Migrate every app back to zero and forward again, on a scratch database only."

    def handle(self, *args, **options):
        if not is_scratch():
            raise CommandError(
                "Refusing to run: this drops every table, and the database isn't SQLite or "
                "named like a scratch database (test_... / ...scratch...)."
            )
        loader = MigrationLoader(connection)
        ours = [label for label in OUR_APPS if label in loader.migrated_apps]
        self.stdout.write(f"Database: {settings.DATABASES['default']['ENGINE']}")

        call_command("migrate", verbosity=0, interactive=False)
        migrations = sum(1 for key in loader.disk_migrations if key[0] in ours)
        self.stdout.write(f"Forward: ok ({migrations} migrations in our apps)")

        # Reverse dependency order: an app that others depend on can only go back after them.
        for label in reversed(self.order(loader, ours)):
            call_command("migrate", label, "zero", verbosity=0, interactive=False)
            self.stdout.write(f"Back to zero: {label}")

        remaining = [t for t in connection.introspection.table_names() if t.split("_")[0] in ours]
        if remaining:
            raise CommandError(f"Tables left behind after rolling everything back: {remaining}")
        self.stdout.write("Rolled back: no tables of ours remain")

        call_command("migrate", verbosity=0, interactive=False)
        self.stdout.write("Forward again: ok")

        call_command("makemigrations", check=True, dry_run=True, verbosity=0)
        self.stdout.write("No drift between the migrations and the models")
        self.stdout.write(self.style.SUCCESS("Every migration is reversible."))

    @staticmethod
    def order(loader, ours: list[str]) -> list[str]:
        """Our apps, dependencies first."""
        seen: list[str] = []

        def visit(label: str) -> None:
            if label in seen or label not in ours:
                return
            for key in loader.graph.leaf_nodes(label):
                for parent in loader.graph.forwards_plan(key):
                    if parent[0] != label:
                        visit(parent[0])
            seen.append(label)

        for label in ours:
            apps.get_app_config(label)  # fail loudly if the list above goes stale
            visit(label)
        return seen
