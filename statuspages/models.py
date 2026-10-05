import secrets
import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


def new_token() -> str:
    return secrets.token_urlsafe(24)


class StatusPage(models.Model):
    """A public page showing the state of some of the owner's monitors.

    The page is the only unauthenticated surface that shows a user's data, so what it may reveal
    is narrow and decided in `statuspages/services.py`: component names the owner chose, their
    status and uptime, incidents without their technical detail, and the notes the owner marked
    visible. Never a monitor's target, id or the reason a check failed.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="status_pages"
    )
    name = models.CharField(max_length=80)
    slug = models.SlugField(max_length=40, unique=True)
    # Empty = open to anyone with the link. Otherwise a hash (Django's password hasher).
    password_hash = models.CharField(max_length=256, blank=True)
    # A draft (False) is invisible to everyone but its owner, who can preview it.
    is_public = models.BooleanField(default=False)
    # accent (a #rrggbb colour), logo_url (https), description, support_url (https).
    branding = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["user"])]

    def __str__(self) -> str:
        return f"{self.name} ({self.slug})"

    @property
    def has_password(self) -> bool:
        return bool(self.password_hash)


class StatusPageComponent(models.Model):
    """One monitor shown on a page, under a name the owner picked."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    page = models.ForeignKey(StatusPage, on_delete=models.CASCADE, related_name="components")
    monitor = models.ForeignKey(
        "monitoring.Monitor", on_delete=models.CASCADE, related_name="status_page_components"
    )
    display_name = models.CharField(max_length=80)
    # A heading to list it under ("API", "Website"); blank = ungrouped.
    group = models.CharField(max_length=60, blank=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "display_name"]
        constraints = [
            models.UniqueConstraint(fields=["page", "monitor"], name="one_component_per_monitor")
        ]

    def __str__(self) -> str:
        return self.display_name


class StatusPageAnnouncement(models.Model):
    """Something the owner wants said on the page: an incident write-up or planned maintenance."""

    class Kind(models.TextChoices):
        INCIDENT = "incident", "Incident"
        MAINTENANCE = "maintenance", "Maintenance"

    class Impact(models.TextChoices):
        NONE = "none", "Informational"
        MINOR = "minor", "Minor"
        MAJOR = "major", "Major"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    page = models.ForeignKey(StatusPage, on_delete=models.CASCADE, related_name="announcements")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    impact = models.CharField(max_length=8, choices=Impact.choices, default=Impact.NONE)
    title = models.CharField(max_length=120)
    body = models.TextField(max_length=2000, blank=True)
    # Maintenance only: when it is planned to run. An incident is "active" from creation.
    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    # An incident is active until resolved; a maintenance until it has ended or is resolved.
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["page", "-created_at"])]

    def __str__(self) -> str:
        return self.title


class StatusPageSubscriber(models.Model):
    """Someone who asked to be emailed. Nothing is sent to them until they click the link in a
    confirmation email (double opt-in), so a page can't be used to mail strangers."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    page = models.ForeignKey(StatusPage, on_delete=models.CASCADE, related_name="subscribers")
    email = models.EmailField()  # always lower-case
    confirmed_at = models.DateTimeField(null=True, blank=True)
    # Cleared once used. Random and unguessable, so a link proves the owner of the inbox.
    confirm_token = models.CharField(max_length=64, blank=True, default=new_token)
    # How many confirmation emails have been queued (the per-address cool-down and dedupe key).
    confirmations_sent = models.PositiveSmallIntegerField(default=0)
    last_confirmation_at = models.DateTimeField(null=True, blank=True)
    # In every email, so anyone can leave with one click.
    unsubscribe_token = models.CharField(max_length=64, unique=True, default=new_token)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["page", "email"], name="one_subscription_per_email")
        ]
        indexes = [models.Index(fields=["confirm_token"])]

    def __str__(self) -> str:
        return f"{self.email} -> {self.page_id}"

    @property
    def confirmed(self) -> bool:
        return self.confirmed_at is not None


class StatusPageEmail(models.Model):
    """One email to one subscriber: the queue, and the record of what happened.

    The same shape as the alert queue: written when something happens, claimed by
    `run_status_page_mail_once` with `FOR UPDATE SKIP LOCKED`, retried with back-off. The text is
    frozen into `payload` when the row is made, so a retry describes the moment it was about.
    """

    class Kind(models.TextChoices):
        CONFIRM = "confirm", "Confirm subscription"
        INCIDENT_OPENED = "incident_opened", "Component down"
        INCIDENT_RESOLVED = "incident_resolved", "Component recovered"
        ANNOUNCEMENT = "announcement", "Announcement posted"
        ANNOUNCEMENT_RESOLVED = "announcement_resolved", "Announcement resolved"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"

    id = models.BigAutoField(primary_key=True)
    subscriber = models.ForeignKey(
        StatusPageSubscriber, on_delete=models.CASCADE, related_name="emails"
    )
    kind = models.CharField(max_length=24, choices=Kind.choices)
    # What it is about, so "did they get the opened one?" can be answered and so unpublishing
    # or resolving can cancel what hasn't gone out. Plain ids (the rows may be deleted).
    incident = models.ForeignKey(
        "incidents.Incident", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    announcement = models.ForeignKey(
        StatusPageAnnouncement,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="emails",
    )
    dedupe_key = models.CharField(max_length=64)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    attempt_count = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    error = models.CharField(max_length=300, blank=True)
    payload = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["status", "next_attempt_at"])]
        constraints = [
            models.UniqueConstraint(
                fields=["subscriber", "dedupe_key"], name="one_email_per_subscriber_and_event"
            ),
            models.CheckConstraint(
                condition=Q(status="pending", next_attempt_at__isnull=False) | ~Q(status="pending"),
                name="pending_status_emails_have_a_due_time",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.kind} -> {self.subscriber_id}: {self.status}"
