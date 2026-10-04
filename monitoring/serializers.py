from __future__ import annotations

from django.conf import settings
from drf_spectacular.utils import PolymorphicProxySerializer, extend_schema_field
from rest_framework import serializers

from incidents import services as incidents

from . import target_validation as tv
from .models import Check, Monitor
from .services import first_check_due, summarize
from .types import CONFIG_SERIALIZERS, SPECS


@extend_schema_field(
    PolymorphicProxySerializer(
        component_name="MonitorConfig",
        serializers=CONFIG_SERIALIZERS,
        resource_type_field_name=None,  # the shape follows the monitor's `type`
    )
)
class MonitorConfigField(serializers.JSONField):
    """Per-type settings. Validated against the serializer for the monitor's `type`."""


class MonitorSerializer(serializers.ModelSerializer):
    config = MonitorConfigField(required=False)
    target = serializers.CharField(max_length=2048, required=False, allow_blank=True)
    heartbeat_url = serializers.SerializerMethodField()

    class Meta:
        model = Monitor
        fields = [
            "id",
            "type",
            "name",
            "target",
            "config",
            "interval_seconds",
            "status",
            "next_check_at",
            "last_check_at",
            "last_response_ms",
            "last_status_detail",
            "last_heartbeat_at",
            "heartbeat_url",
            "created_at",
            "updated_at",
        ]
        # Omitted on create = the type's default interval (see validate()).
        extra_kwargs = {"interval_seconds": {"required": False}}
        read_only_fields = [
            "id",
            "status",
            "next_check_at",
            "last_check_at",
            "last_response_ms",
            "last_status_detail",
            "last_heartbeat_at",
            "heartbeat_url",
            "created_at",
            "updated_at",
        ]

    @extend_schema_field({"type": "string", "format": "uri", "nullable": True})
    def get_heartbeat_url(self, obj: Monitor) -> str | None:
        if not obj.heartbeat_token:
            return None
        return f"{settings.API_PUBLIC_URL}/api/v1/heartbeat/{obj.heartbeat_token}/"

    def validate_name(self, value: str) -> str:
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Give the monitor a name.")
        return value

    def validate(self, attrs):
        editing = self.instance is not None
        monitor_type = self.instance.type if editing else attrs.get("type")

        if editing and "type" in attrs and attrs["type"] != self.instance.type:
            raise serializers.ValidationError(
                {"type": "A monitor's type can't be changed. Create a new monitor instead."}
            )
        if not editing and not monitor_type:
            raise serializers.ValidationError({"type": "Choose what to monitor."})

        spec = SPECS[monitor_type]
        errors: dict[str, object] = {}

        # target
        if spec.target is None:
            if attrs.get("target"):
                errors["target"] = "This monitor type doesn't take a target."
            attrs["target"] = ""
        elif "target" in attrs or not editing:
            raw = attrs.get("target", "")
            if not raw.strip():
                errors["target"] = "Enter what to monitor."
            else:
                try:
                    attrs["target"] = spec.target(raw)
                except tv.TargetRejected as exc:
                    errors["target"] = str(exc)

        # config: on edit, a missing config keeps the stored one; a given config replaces it
        if "config" in attrs or not editing:
            config_serializer = spec.config(data=attrs.get("config", {}))
            if config_serializer.is_valid():
                attrs["config"] = config_serializer.validated_data
            else:
                errors["config"] = config_serializer.errors
        else:
            attrs.pop("config", None)

        # interval
        interval = attrs.get("interval_seconds")
        if interval is None:
            if not editing:
                attrs["interval_seconds"] = spec.default_interval
        elif interval not in spec.intervals:
            minutes = ", ".join(
                f"{i // 60}m" if i < 3600 else f"{i // 3600}h" for i in spec.intervals
            )
            errors["interval_seconds"] = f"Choose one of: {minutes}."

        if errors:
            raise serializers.ValidationError(errors)
        return attrs

    def create(self, validated_data):
        monitor = Monitor(user=self.context["request"].user, **validated_data)
        if monitor.type == Monitor.Type.CRON:
            monitor.heartbeat_token = Monitor.new_heartbeat_token()
        monitor.next_check_at = first_check_due(monitor)
        monitor.save()
        return monitor

    def update(self, instance: Monitor, validated_data):
        watched = {"target", "config", "interval_seconds"}
        changed = {
            field
            for field in watched
            if field in validated_data and validated_data[field] != getattr(instance, field)
        }
        for field, value in validated_data.items():
            setattr(instance, field, value)

        if changed and instance.status != Monitor.Status.PAUSED:
            # New settings should be checked promptly; and a different target or
            # config means the old up/down verdict no longer applies.
            was_down = instance.status == Monitor.Status.DOWN
            if changed & {"target", "config"}:
                instance.status = Monitor.Status.PENDING
            instance.next_check_at = first_check_due(instance)
            instance.save()
            if was_down and instance.status == Monitor.Status.PENDING:
                incidents.close_for_edit(instance)  # the old verdict no longer applies
            return instance
        instance.save()
        return instance


class CheckSerializer(serializers.ModelSerializer):
    class Meta:
        model = Check
        fields = [
            "id",
            "region",
            "confirmation",
            "started_at",
            "duration_ms",
            "success",
            "status_detail",
            "status_code",
            "dns_ms",
            "tcp_ms",
            "tls_ms",
            "ttfb_ms",
            "detail",
        ]
        read_only_fields = fields


class MonitorSummarySerializer(serializers.Serializer):
    """The last 24 hours at a glance (scheduled checks from the primary region only)."""

    window_hours = serializers.IntegerField()
    checks = serializers.IntegerField()
    uptime_percent = serializers.FloatField(allow_null=True)
    avg_response_ms = serializers.IntegerField(allow_null=True)
    p95_response_ms = serializers.IntegerField(allow_null=True)


class MonitorDetailSerializer(MonitorSerializer):
    summary = serializers.SerializerMethodField()

    class Meta(MonitorSerializer.Meta):
        fields = [*MonitorSerializer.Meta.fields, "summary"]
        read_only_fields = [*MonitorSerializer.Meta.read_only_fields, "summary"]

    @extend_schema_field(MonitorSummarySerializer)
    def get_summary(self, obj: Monitor):
        return summarize(obj)


class CheckPageSerializer(serializers.Serializer):
    """Documents the cursor-paginated checks response (cursor paging has no `count`)."""

    next = serializers.URLField(allow_null=True)
    previous = serializers.URLField(allow_null=True)
    results = CheckSerializer(many=True)
