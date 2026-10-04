import secrets
import uuid

from django.conf import settings
from django.db import models
from django.db.models import F, Q


class AlertContact(models.Model):
    """A reusable notification destination. Endpoints and delivery arrive in Phase 8."""

    class Channel(models.TextChoices):
        EMAIL = "email", "Email"
        WEBHOOK = "webhook", "Webhook"
        TELEGRAM = "telegram", "Telegram"
        DISCORD = "discord", "Discord"
        SLACK = "slack", "Slack"
        PUSH = "push", "Web push"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="alert_contacts"
    )
    name = models.CharField(max_length=120)
    channel = models.CharField(max_length=16, choices=Channel.choices)
    # Shape depends on the channel (webhook URL, chat ID, ...); validated per channel in Phase 8.
    config = models.JSONField(default=dict, blank=True)
    delay_seconds = models.PositiveIntegerField(default=0)
    repeat_interval_seconds = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["user"])]

    def __str__(self) -> str:
        return f"{self.name} ({self.channel})"


def _new_heartbeat_token() -> str:
    return secrets.token_urlsafe(24)


class Monitor(models.Model):
    """One thing being checked. Which fields apply depends on `type`; see monitoring/types.py."""

    class Type(models.TextChoices):
        HTTP = "http", "HTTP(S)"
        KEYWORD = "keyword", "Keyword"
        PING = "ping", "Ping"
        PORT = "port", "Port"
        SSL = "ssl", "SSL certificate"
        DOMAIN = "domain", "Domain expiration"
        CRON = "cron", "Heartbeat"
        DNS = "dns", "DNS"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"  # created, not checked yet
        UP = "up", "Up"
        DOWN = "down", "Down"
        PAUSED = "paused", "Paused"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="monitors"
    )
    type = models.CharField(max_length=16, choices=Type.choices)
    name = models.CharField(max_length=120)
    # URL, host or domain depending on the type; blank for heartbeat monitors.
    target = models.CharField(max_length=2048, blank=True)
    config = models.JSONField(default=dict, blank=True)
    interval_seconds = models.PositiveIntegerField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    # What the scheduler claims against (Phase 6). NULL means "never due" (paused).
    next_check_at = models.DateTimeField(null=True, blank=True)
    last_check_at = models.DateTimeField(null=True, blank=True)
    # Secret path segment of a heartbeat monitor's ping URL. Only set for type=cron.
    heartbeat_token = models.CharField(max_length=64, unique=True, null=True, blank=True)
    alert_contacts = models.ManyToManyField(
        AlertContact, through="MonitorAlertContact", related_name="monitors", blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["next_check_at"]),
            models.Index(fields=["user", "-created_at"]),
            models.Index(fields=["user", "status"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(type="cron", heartbeat_token__isnull=False)
                | (~Q(type="cron") & Q(heartbeat_token__isnull=True)),
                name="heartbeat_token_only_on_cron",
            ),
            models.CheckConstraint(
                condition=~Q(status="paused") | Q(next_check_at__isnull=True),
                name="paused_monitors_are_never_due",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.type})"

    @staticmethod
    def new_heartbeat_token() -> str:
        return _new_heartbeat_token()


class MonitorAlertContact(models.Model):
    """Which contacts get told about a monitor. Deleting a contact detaches it from
    monitors (this row goes) but never touches the monitor itself."""

    monitor = models.ForeignKey(Monitor, on_delete=models.CASCADE)
    alert_contact = models.ForeignKey(AlertContact, on_delete=models.CASCADE)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["monitor", "alert_contact"], name="unique_monitor_alert_contact"
            )
        ]

    def __str__(self) -> str:
        return f"{self.alert_contact} -> {self.monitor}"


class MaintenanceWindow(models.Model):
    """Scheduled downtime: failures inside it don't alert or count against uptime.
    Endpoints and scheduler integration arrive with the monitoring engine."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="maintenance_windows"
    )
    name = models.CharField(max_length=120)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    recurrence_rule = models.CharField(max_length=255, blank=True)
    monitors = models.ManyToManyField(Monitor, related_name="maintenance_windows", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["starts_at", "ends_at"])]
        constraints = [
            models.CheckConstraint(
                condition=Q(ends_at__gt=F("starts_at")), name="maintenance_ends_after_start"
            )
        ]

    def __str__(self) -> str:
        return self.name
