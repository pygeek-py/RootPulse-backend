import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


class Incident(models.Model):
    """A confirmed outage: opens when a monitor goes down, closes when it recovers.

    There is at most one *open* incident per monitor (a database constraint, not just
    application logic), so a flaky network, several regions or a retried pass can never
    produce duplicates.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    monitor = models.ForeignKey(
        "monitoring.Monitor", on_delete=models.CASCADE, related_name="incidents"
    )
    started_at = models.DateTimeField()
    # NULL = ongoing.
    ended_at = models.DateTimeField(null=True, blank=True)
    # What the failing check reported (status_detail), e.g. "timeout" or "status_mismatch".
    reason = models.CharField(max_length=64)
    status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    # The check that opened it and the one that closed it (kept if the check is later purged).
    opened_by = models.ForeignKey(
        "monitoring.Check", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    resolved_by = models.ForeignKey(
        "monitoring.Check", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    # Why it closed: "recovered" (a check succeeded), or the monitor was paused / edited.
    resolution = models.CharField(max_length=16, blank=True)
    excluded_from_reports = models.BooleanField(default=False)
    # Which phase of the request went wrong (dns/tcp/tls/server), blank when unknown, and the
    # figures behind it (see deploys/rootcause.py).
    root_cause_stage = models.CharField(max_length=8, blank=True)
    root_cause = models.JSONField(default=dict, blank=True)
    # The deploy this most plausibly followed (see deploys/services.py), if any.
    deploy = models.ForeignKey(
        "deploys.Deploy", null=True, blank=True, on_delete=models.SET_NULL, related_name="incidents"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["monitor", "-started_at"]),
            models.Index(fields=["-started_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["monitor"],
                condition=Q(ended_at__isnull=True),
                name="one_open_incident_per_monitor",
            ),
            models.CheckConstraint(
                condition=Q(ended_at__isnull=True) | Q(ended_at__gte=models.F("started_at")),
                name="incident_ends_after_it_starts",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.monitor_id} down: {self.reason}"

    @property
    def ongoing(self) -> bool:
        return self.ended_at is None


class IncidentEvent(models.Model):
    """A system-written timeline entry (as opposed to a comment a person wrote)."""

    class Kind(models.TextChoices):
        OPENED = "opened", "Opened"
        CONFIRMED = "recheck_confirmed", "Confirmed by re-check"
        RESOLVED = "resolved", "Resolved"
        CLOSED_PAUSED = "closed_paused", "Closed: monitor paused"
        CLOSED_EDITED = "closed_edited", "Closed: monitor changed"
        EXCLUDED = "excluded", "Excluded from reports"
        INCLUDED = "included", "Included in reports"
        NOTIFICATION_SENT = "notification_sent", "Alert sent"
        NOTIFICATION_FAILED = "notification_failed", "Alert failed"
        DEPLOY_LINKED = "deploy_linked", "Linked to a deploy"

    id = models.BigAutoField(primary_key=True)
    incident = models.ForeignKey(Incident, on_delete=models.CASCADE, related_name="events")
    kind = models.CharField(max_length=24, choices=Kind.choices)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField()

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["incident", "created_at"])]

    def __str__(self) -> str:
        return f"{self.incident_id} {self.kind}"


class IncidentComment(models.Model):
    """A note a person wrote on an incident."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    incident = models.ForeignKey(Incident, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    body = models.TextField(max_length=5000)
    # Whether the public status page shows it (Phase 13). Off by default: notes are private.
    visible_on_status_page = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [models.Index(fields=["incident", "created_at"])]

    def __str__(self) -> str:
        return f"comment on {self.incident_id}"
