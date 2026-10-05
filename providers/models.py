"""Dependency monitoring: the third-party services you rely on, and what they say about themselves.

RootPulse doesn't probe these services. It reads the public status page each one already
publishes (a JSON feed, in a few different formats: see `providers.adapters`) and tells you when
that page reports trouble. The list of providers is curated (`providers.catalog`), not user
supplied, so a feed address is never something a user typed in.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models

IMPACTS = ["none", "minor", "major", "critical"]


class Provider(models.Model):
    class Status(models.TextChoices):
        OPERATIONAL = "operational", "Operational"
        DEGRADED = "degraded", "Degraded performance"
        PARTIAL_OUTAGE = "partial_outage", "Partial outage"
        MAJOR_OUTAGE = "major_outage", "Major outage"
        MAINTENANCE = "maintenance", "Under maintenance"
        UNKNOWN = "unknown", "Unknown"

    slug = models.SlugField(max_length=40, unique=True)
    name = models.CharField(max_length=80)
    category = models.CharField(max_length=40)
    description = models.CharField(max_length=200, blank=True)
    website_url = models.URLField(max_length=200)
    status_page_url = models.URLField(max_length=200)  # the human-readable page
    feed_url = models.URLField(max_length=200)  # the base the adapter reads from
    adapter = models.CharField(max_length=20)
    enabled = models.BooleanField(default=True)

    # What the status page said the last time we read it.
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.UNKNOWN)
    status_description = models.CharField(max_length=200, blank=True)
    # Only components that aren't operational, trimmed (some providers list hundreds).
    components = models.JSONField(default=list, blank=True)
    component_count = models.PositiveIntegerField(default=0)

    # Polling bookkeeping (providers/poller.py).
    status_checked_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    consecutive_failures = models.PositiveSmallIntegerField(default=0)
    last_error = models.CharField(max_length=200, blank=True)
    next_poll_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["name"]
        indexes = [models.Index(fields=["enabled", "next_poll_at"])]

    def __str__(self) -> str:
        return self.name


class ProviderIncident(models.Model):
    """One incident a provider reported on its own status page."""

    class Impact(models.TextChoices):
        NONE = "none", "Informational"
        MINOR = "minor", "Minor"
        MAJOR = "major", "Major"
        CRITICAL = "critical", "Critical"

    class Stage(models.TextChoices):
        INVESTIGATING = "investigating", "Investigating"
        IDENTIFIED = "identified", "Identified"
        MONITORING = "monitoring", "Monitoring"
        RESOLVED = "resolved", "Resolved"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    provider = models.ForeignKey(Provider, on_delete=models.CASCADE, related_name="incidents")
    # The provider's own id for it: makes ingestion idempotent.
    external_id = models.CharField(max_length=128)
    title = models.CharField(max_length=300)
    impact = models.CharField(max_length=10, choices=Impact.choices, default=Impact.MINOR)
    stage = models.CharField(max_length=16, choices=Stage.choices, default=Stage.INVESTIGATING)
    started_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)  # null = still going
    url = models.CharField(max_length=500, blank=True)
    # The provider's own updates, newest first, as short plain text: [{stage, body, at}].
    updates = models.JSONField(default=list, blank=True)
    affected = models.JSONField(default=list, blank=True)  # component / product names
    first_seen_at = models.DateTimeField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["provider", "-started_at"]),
            models.Index(fields=["-started_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "external_id"], name="one_incident_per_provider_ref"
            )
        ]

    def __str__(self) -> str:
        return f"{self.provider_id}: {self.title}"

    @property
    def ongoing(self) -> bool:
        return self.ended_at is None


class UserProviderSubscription(models.Model):
    """A provider a user tracks. Doesn't count against the monitor quota."""

    class MinImpact(models.TextChoices):
        ANY = "none", "Any incident"
        MINOR = "minor", "Minor and above"
        MAJOR = "major", "Major and above"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="provider_subscriptions"
    )
    provider = models.ForeignKey(Provider, on_delete=models.CASCADE, related_name="subscriptions")
    # Which incidents are worth an alert. Many providers post minor notices constantly.
    min_impact = models.CharField(max_length=10, choices=MinImpact.choices, default="minor")
    alert_contacts = models.ManyToManyField(
        "monitoring.AlertContact", blank=True, related_name="provider_subscriptions"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "provider"], name="one_subscription_per_provider"
            )
        ]
        indexes = [models.Index(fields=["user", "created_at"])]

    def __str__(self) -> str:
        return f"{self.user_id} -> {self.provider_id}"
