import secrets
import uuid

from django.conf import settings
from django.db import models
from django.db.models import F, Q


class AlertContact(models.Model):
    """A reusable notification destination (a place to send alerts). Validated per
    channel in notifications/channels.py; delivery lives in the notifications app."""

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
    # Shape depends on the channel (address, webhook URL, bot token + chat id, ...).
    config = models.JSONField(default=dict, blank=True)
    # Wait this long after an incident opens and only alert if it is still down: filters
    # out outages shorter than the delay. 0 = alert immediately.
    delay_seconds = models.PositiveIntegerField(default=0)
    # While the incident stays open, remind again this often. 0 = never.
    repeat_interval_seconds = models.PositiveIntegerField(default=0)
    enabled = models.BooleanField(default=True)
    # An address we can't prove belongs to the user (email to someone else) must be
    # verified before it gets alerts, so RootPulse can't be used to spam strangers.
    verified_at = models.DateTimeField(null=True, blank=True)
    verification_hash = models.CharField(max_length=64, blank=True)
    verification_expires_at = models.DateTimeField(null=True, blank=True)
    verification_attempts = models.PositiveSmallIntegerField(default=0)
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
    # Which deploys are about this monitor: an incident links to a deploy whose service name
    # (or repo / project id) equals this, case-insensitively. Blank means "never link".
    deploy_service = models.CharField(max_length=100, blank=True)
    interval_seconds = models.PositiveIntegerField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    # What the scheduler claims against (Phase 6). NULL means "never due" (paused).
    next_check_at = models.DateTimeField(null=True, blank=True)
    last_check_at = models.DateTimeField(null=True, blank=True)
    # Denormalised from the latest check so lists and the dashboard don't join `Check`.
    last_response_ms = models.PositiveIntegerField(null=True, blank=True)
    last_status_detail = models.CharField(max_length=64, blank=True)
    # When a heartbeat monitor's job last pinged its URL (type=cron only).
    last_heartbeat_at = models.DateTimeField(null=True, blank=True)
    # Engine bookkeeping that isn't a setting, e.g. a DNS monitor's last-seen records.
    state = models.JSONField(default=dict, blank=True)
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


class Check(models.Model):
    """One executed check. Append-only and high volume (a 5-minute monitor from three
    regions writes ~860 a day), so it uses a bigint key and is indexed for the
    detail page's "latest checks for this monitor" query.

    `success` is three-valued: True = up, False = down, None = inconclusive (a
    problem on our side or an unreadable result, such as a TLD without RDAP).
    Inconclusive checks never change a monitor's status or open an incident.
    """

    id = models.BigAutoField(primary_key=True)
    monitor = models.ForeignKey(Monitor, on_delete=models.CASCADE, related_name="checks")
    # Where it ran from: the API's own region, or a Cloudflare prober's name.
    region = models.CharField(max_length=32)
    # A re-check run to confirm a failure, as opposed to the scheduled check.
    confirmation = models.BooleanField(default=False)
    started_at = models.DateTimeField()
    duration_ms = models.PositiveIntegerField(null=True, blank=True)
    success = models.BooleanField(null=True)
    # Short machine-readable outcome ("ok", "timeout", ...): see monitoring/checks/result.py
    status_detail = models.CharField(max_length=64, blank=True)
    status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    # Timing breakdown for HTTP(S) checks: the raw data behind root-cause analysis (Phase 10).
    dns_ms = models.PositiveIntegerField(null=True, blank=True)
    tcp_ms = models.PositiveIntegerField(null=True, blank=True)
    tls_ms = models.PositiveIntegerField(null=True, blank=True)
    ttfb_ms = models.PositiveIntegerField(null=True, blank=True)
    # Type-specific extras: SSL days remaining, DNS record diff, domain expiry date, ...
    detail = models.JSONField(default=dict, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["monitor", "-started_at"]),
            models.Index(fields=["started_at"]),  # retention sweeps
        ]

    def __str__(self) -> str:
        return f"{self.monitor_id} {self.region} {self.status_detail}"


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
