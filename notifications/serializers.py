from __future__ import annotations

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from monitoring.models import AlertContact

from .channels import CHANNELS, ConfigError
from .models import NotificationDelivery


class AlertContactSerializer(serializers.ModelSerializer):
    # What you type in: channel-specific (see notifications/channels.py). What comes back:
    # the same shape with secrets removed.
    config = serializers.JSONField(required=False)
    summary = serializers.SerializerMethodField()
    verified = serializers.SerializerMethodField()
    secret = serializers.SerializerMethodField(
        help_text="Webhook signing secret: shown once, on create and rotate."
    )
    attach_to_existing_monitors = serializers.BooleanField(
        write_only=True, required=False, default=False
    )
    monitor_count = serializers.SerializerMethodField()

    class Meta:
        model = AlertContact
        fields = [
            "id",
            "name",
            "channel",
            "config",
            "summary",
            "delay_seconds",
            "repeat_interval_seconds",
            "enabled",
            "verified",
            "secret",
            "attach_to_existing_monitors",
            "monitor_count",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]

    @extend_schema_field(serializers.CharField())
    def get_summary(self, obj: AlertContact) -> str:
        return CHANNELS[obj.channel].summary(obj.config)

    @extend_schema_field(serializers.IntegerField())
    def get_monitor_count(self, obj: AlertContact) -> int:
        # The list annotates this; a freshly created or edited contact doesn't have it yet.
        annotated = getattr(obj, "monitor_count", None)
        return annotated if annotated is not None else obj.monitors.count()

    @extend_schema_field(serializers.BooleanField())
    def get_verified(self, obj: AlertContact) -> bool:
        return obj.verified_at is not None or obj.channel != AlertContact.Channel.EMAIL

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_secret(self, obj: AlertContact):
        # Set by the view only on the one response that is allowed to show it.
        return obj.config.get("secret") if getattr(obj, "reveal_secret", False) else None

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["config"] = CHANNELS[instance.channel].public(instance.config)
        return data

    # -- validation ------------------------------------------------------------------------

    def validate_name(self, value: str) -> str:
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Give the contact a name.")
        return value

    def validate_delay_seconds(self, value: int) -> int:
        if value > 3600:
            raise serializers.ValidationError("Wait at most 1 hour (3600 seconds).")
        return value

    def validate_repeat_interval_seconds(self, value: int) -> int:
        if value != 0 and not 300 <= value <= 86400:
            raise serializers.ValidationError("Use 0 (never) or between 5 minutes and 24 hours.")
        return value

    def validate(self, attrs):
        editing = self.instance is not None
        if editing and "channel" in attrs and attrs["channel"] != self.instance.channel:
            raise serializers.ValidationError(
                {"channel": "A contact's channel can't be changed. Add a new contact instead."}
            )
        channel = self.instance.channel if editing else attrs.get("channel")
        if not channel:
            raise serializers.ValidationError({"channel": "Choose a channel."})

        incoming = attrs.get("config")
        if incoming is not None or not editing:
            if not isinstance(incoming, dict):
                raise serializers.ValidationError(
                    {"config": "Fill in the details for this channel."}
                )
            previous = self.instance.config if editing else None
            # Editing sends only what changed (e.g. a new chat id); keep the rest.
            merged = {**(previous or {}), **incoming} if editing else incoming
            try:
                attrs["config"] = CHANNELS[channel].clean(merged, previous=previous)
            except ConfigError as exc:
                raise serializers.ValidationError(
                    {f"config.{field}": message for field, message in exc.errors.items()}
                ) from exc
        return attrs


class DeliverySerializer(serializers.ModelSerializer):
    contact_id = serializers.UUIDField(read_only=True, allow_null=True)
    monitor_name = serializers.SerializerMethodField()
    incident_id = serializers.UUIDField(read_only=True, allow_null=True)

    class Meta:
        model = NotificationDelivery
        fields = [
            "id",
            "contact_id",
            "contact_name",
            "channel",
            "event",
            "status",
            "attempt_count",
            "sent_at",
            "next_attempt_at",
            "error",
            "monitor_name",
            "incident_id",
            "created_at",
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_monitor_name(self, obj: NotificationDelivery):
        return obj.monitor.name if obj.monitor else None


class ContactVerifySerializer(serializers.Serializer):
    code = serializers.RegexField(r"^\d{6}$", error_messages={"invalid": "Enter the 6-digit code."})


class TelegramDiscoverSerializer(serializers.Serializer):
    bot_token = serializers.CharField(max_length=200)


class TestResultSerializer(serializers.Serializer):
    status = serializers.CharField()
    error = serializers.CharField(allow_blank=True)
