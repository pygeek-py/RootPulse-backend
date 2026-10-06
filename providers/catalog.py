"""The curated list of providers (about twenty, not a licensed dataset).

Each entry names the status page people read, and the address its machine-readable feed lives
under. `adapter` says which parser understands that feed (see `providers.adapters`). Most
providers run Atlassian Statuspage, whose JSON is the same everywhere; Slack and Google Cloud
publish their own formats.

Stripe is deliberately absent: the only public JSON it offers (status.stripe.com/current) is
frozen at an old date, so reading it would report "all clear" regardless. Add it when it
publishes a live feed.

`sync_catalog()` is idempotent and runs after every migrate, so a deployment always has the
list; it never touches status or polling state.
"""

from __future__ import annotations

from dataclasses import dataclass

STATUSPAGE = "statuspage"
SLACK = "slack"
GCP = "gcp"


@dataclass(frozen=True)
class Entry:
    slug: str
    name: str
    category: str
    description: str
    website: str
    page: str  # status page people read
    feed: str  # base URL the adapter appends its paths to
    adapter: str = STATUSPAGE


def _sp(slug, name, category, description, website, base):
    return Entry(slug, name, category, description, website, base, base, STATUSPAGE)


CATALOG: list[Entry] = [
    _sp("github", "GitHub", "Developer tools", "Git hosting, Actions, Copilot", "https://github.com", "https://www.githubstatus.com"),
    _sp("npm", "npm", "Developer tools", "The JavaScript package registry", "https://www.npmjs.com", "https://status.npmjs.org"),
    _sp("circleci", "CircleCI", "Developer tools", "Continuous integration", "https://circleci.com", "https://status.circleci.com"),
    _sp("atlassian", "Atlassian", "Developer tools", "Jira, Confluence, Bitbucket, Trello", "https://www.atlassian.com", "https://status.atlassian.com"),
    _sp("sentry", "Sentry", "Developer tools", "Error tracking", "https://sentry.io", "https://status.sentry.io"),
    _sp("datadog", "Datadog", "Developer tools", "Monitoring and observability", "https://www.datadoghq.com", "https://status.datadoghq.com"),
    _sp("cloudflare", "Cloudflare", "Cloud and hosting", "CDN, DNS, Workers, security", "https://www.cloudflare.com", "https://www.cloudflarestatus.com"),
    _sp("vercel", "Vercel", "Cloud and hosting", "Frontend hosting and edge functions", "https://vercel.com", "https://www.vercel-status.com"),
    _sp("netlify", "Netlify", "Cloud and hosting", "Web hosting and serverless", "https://www.netlify.com", "https://www.netlifystatus.com"),
    _sp("render", "Render", "Cloud and hosting", "Web services, databases, cron jobs", "https://render.com", "https://status.render.com"),
    _sp("flyio", "Fly.io", "Cloud and hosting", "Run apps close to users", "https://fly.io", "https://status.flyio.net"),
    _sp("digitalocean", "DigitalOcean", "Cloud and hosting", "Droplets, Kubernetes, managed databases", "https://www.digitalocean.com", "https://status.digitalocean.com"),
    Entry("gcp", "Google Cloud", "Cloud and hosting", "Compute, storage, networking and data", "https://cloud.google.com", "https://status.cloud.google.com", "https://status.cloud.google.com", GCP),
    _sp("supabase", "Supabase", "Databases", "Postgres, auth and storage", "https://supabase.com", "https://status.supabase.com"),
    _sp("mongodb", "MongoDB", "Databases", "Atlas, Charts and Realm", "https://www.mongodb.com", "https://status.mongodb.com"),
    _sp("twilio", "Twilio", "Communications", "SMS, voice and email (SendGrid)", "https://www.twilio.com", "https://status.twilio.com"),
    _sp("discord", "Discord", "Communications", "Chat and community platform", "https://discord.com", "https://discordstatus.com"),
    Entry("slack", "Slack", "Communications", "Team messaging", "https://slack.com", "https://slack-status.com", "https://slack-status.com", SLACK),
    _sp("openai", "OpenAI", "AI", "ChatGPT and the OpenAI API", "https://openai.com", "https://status.openai.com"),
    _sp("anthropic", "Anthropic", "AI", "Claude and the Anthropic API", "https://www.anthropic.com", "https://status.claude.com"),
]  # fmt: skip


def sync_catalog(**_ignored) -> int:
    """Create or refresh the catalogue. Returns how many providers exist from it."""
    from django.db import connection

    from .models import Provider

    # `migrate providers zero` (rolling a bad migration back) fires this hook with the table
    # already gone: nothing to sync then, and it mustn't turn a clean rollback into a crash.
    if Provider._meta.db_table not in connection.introspection.table_names():
        return 0

    for entry in CATALOG:
        Provider.objects.update_or_create(
            slug=entry.slug,
            defaults={
                "name": entry.name,
                "category": entry.category,
                "description": entry.description,
                "website_url": entry.website,
                "status_page_url": entry.page,
                "feed_url": entry.feed,
                "adapter": entry.adapter,
            },
        )
    return len(CATALOG)
