import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


class NotificationDelivery(models.Model):
    """One alert to one contact: the queue the dispatcher works through, and the record
    of what happened to each one.

    Rows are written when an incident opens, resolves or stays open past a contact's
    reminder interval, then claimed by `run_notifications_once` (the same
    select_for_update(skip_locked) pattern as the scheduler). What to say is frozen into
    `payload` when the row is created, so a retry five minutes later still describes the
    moment it was about, not whatever the monitor looks like by then.
    """

    class Event(models.TextChoices):
        OPENED = "opened", "Incident opened"
        REMINDER = "reminder", "Still down"
        RESOLVED = "resolved", "Recovered"
        TEST = "test", "Test"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"  # deliberately not sent (maintenance, recovered first, ...)

    id = models.BigAutoField(primary_key=True)
    incident = models.ForeignKey(
        "incidents.Incident",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="deliveries",
    )
    monitor = models.ForeignKey(
        "monitoring.Monitor",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="deliveries",
    )
    # SET_NULL: deleting a contact keeps the history of what was sent to it.
    contact = models.ForeignKey(
        "monitoring.AlertContact", null=True, on_delete=models.SET_NULL, related_name="deliveries"
    )
    contact_name = models.CharField(max_length=120)  # as it was when sent
    channel = models.CharField(max_length=16)
    event = models.CharField(max_length=16, choices=Event.choices)
    # Makes creation idempotent: "opened", "resolved", "reminder:3". A retried pass can
    # never alert twice for the same thing.
    dedupe_key = models.CharField(max_length=32)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    attempt_count = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    # Why it failed or was skipped, in words a person can act on. Never contains secrets.
    error = models.CharField(max_length=300, blank=True)
    payload = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["status", "next_attempt_at"]),  # the dispatcher's claim query
            models.Index(fields=["incident", "-created_at"]),
            models.Index(fields=["monitor", "-created_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["incident", "contact", "dedupe_key"],
                condition=Q(incident__isnull=False, contact__isnull=False),
                name="one_delivery_per_incident_contact_event",
            ),
            models.CheckConstraint(
                condition=Q(status="pending", next_attempt_at__isnull=False) | ~Q(status="pending"),
                name="pending_deliveries_have_a_due_time",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.event} -> {self.contact_name} ({self.channel}): {self.status}"


class IntegrationLink(models.Model):
    """A one-time "connect my Telegram" request.

    The person clicks Connect, gets a link containing a random token, and presses Start in
    Telegram. The bot then receives `/start <token>` from *that* chat, which proves whose chat
    it is. Only a hash of the token is stored, it works once, and it expires in minutes.
    """

    class Provider(models.TextChoices):
        TELEGRAM = "telegram", "Telegram"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="integration_links"
    )
    provider = models.CharField(max_length=16, choices=Provider.choices)
    token_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)
    contact = models.ForeignKey(
        "monitoring.AlertContact",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["user", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.provider} link for {self.user_id}"


class BotState(models.Model):
    """Tiny key/number store: where the Telegram poller left off."""

    key = models.CharField(max_length=40, primary_key=True)
    value = models.BigIntegerField(default=0)

    def __str__(self) -> str:
        return f"{self.key}={self.value}"
