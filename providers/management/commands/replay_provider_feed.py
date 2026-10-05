"""Feed a saved status-page response through exactly the code that handles a live one.

    python manage.py replay_provider_feed github \\
        --doc summary=tests/fixtures/providers/github_summary.json \\
        --doc incidents=tests/fixtures/providers/github_incidents.json \\
        --as-of 2026-10-01T15:00:00Z

`--as-of` is "now" for the replay, and the saved response is rewound to it first: an outage that
started at 14:47 and ended at 17:56 is *open* when replayed as of 15:00 (with only the updates the
provider had published by then) and resolved when replayed as of 18:00. Anyone tracking the
provider is alerted as they would have been at that moment. It overwrites the provider's
displayed status with what the replay said, until the next real poll corrects it, so it is for
testing and demos.
"""

import json
from datetime import UTC, datetime

from django.core.management.base import BaseCommand, CommandError

from providers.adapters import ADAPTERS, FeedError
from providers.ingest import ingest
from providers.models import Provider
from providers.replay import rewind


class Command(BaseCommand):
    help = "Replay a saved status-page response as though it had just been read."

    def add_arguments(self, parser):
        parser.add_argument("slug")
        parser.add_argument(
            "--doc",
            action="append",
            default=[],
            metavar="NAME=PATH",
            help="A document the adapter expects (e.g. summary=..., incidents=...). Repeatable.",
        )
        parser.add_argument("--as-of", help="ISO time to treat as now (default: now).")

    def handle(self, *args, slug, doc, as_of, **options):
        try:
            provider = Provider.objects.get(slug=slug)
        except Provider.DoesNotExist as exc:
            raise CommandError(f"No provider '{slug}'.") from exc
        adapter = ADAPTERS[provider.adapter]
        docs = {}
        for item in doc:
            name, _, path = item.partition("=")
            if not path:
                raise CommandError("Use --doc NAME=PATH.")
            with open(path, encoding="utf-8") as handle:
                docs[name] = json.load(handle)
        now = datetime.fromisoformat(as_of.replace("Z", "+00:00")) if as_of else None
        if now is not None and now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        try:
            result = adapter.parse(docs)
        except FeedError as exc:
            raise CommandError(f"That isn't a valid {adapter.key} feed: {exc}") from exc
        if now is not None:
            result = rewind(result, now)  # what the feed looked like at that moment
        summary = ingest(provider, result, now=now)
        self.stdout.write(
            f"{provider.name}: status={result.status}, {len(result.incidents)} incidents read, "
            f"{summary.created} new, {summary.updated} updated, {summary.alerts} alerts queued."
        )
