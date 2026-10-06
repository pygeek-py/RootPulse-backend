"""Maintenance windows: planned downtime. Inside one, a monitor isn't checked and nobody is
alerted about it (the scheduler skips it; notifications check again before sending)."""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone
from drf_spectacular.utils import (
    OpenApiParameter,
    extend_schema,
    extend_schema_field,
    extend_schema_view,
)
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from rest_framework.viewsets import ModelViewSet

from .models import MaintenanceWindow, Monitor

MAX_DURATION = timedelta(days=30)
MAX_WINDOWS_PER_USER = 100


class OwnMonitorsField(serializers.PrimaryKeyRelatedField):
    def get_queryset(self):
        request = self.context.get("request")
        if request is None or not request.user.is_authenticated:
            return Monitor.objects.none()
        return Monitor.objects.filter(user=request.user)


class MaintenanceWindowSerializer(serializers.ModelSerializer):
    monitor_ids = OwnMonitorsField(many=True, source="monitors", required=True, allow_empty=False)
    state = serializers.SerializerMethodField()

    class Meta:
        model = MaintenanceWindow
        fields = ["id", "name", "starts_at", "ends_at", "monitor_ids", "state", "created_at"]
        read_only_fields = ["id", "state", "created_at"]

    @extend_schema_field(serializers.ChoiceField(choices=["upcoming", "active", "past"]))
    def get_state(self, obj: MaintenanceWindow) -> str:
        now = timezone.now()
        if obj.ends_at <= now:
            return "past"
        return "active" if obj.starts_at <= now else "upcoming"

    def validate_name(self, value: str) -> str:
        value = " ".join(value.split())  # one line: names end up in email subjects and headers
        if not value:
            raise serializers.ValidationError("Give the window a name.")
        return value

    def validate(self, attrs):
        starts = attrs.get("starts_at", getattr(self.instance, "starts_at", None))
        ends = attrs.get("ends_at", getattr(self.instance, "ends_at", None))
        if starts and ends:
            if ends <= starts:
                raise serializers.ValidationError({"ends_at": "It has to end after it starts."})
            if ends - starts > MAX_DURATION:
                raise serializers.ValidationError({"ends_at": "Keep a window under 30 days."})
        return attrs

    def create(self, validated_data):
        monitors = validated_data.pop("monitors")
        window = MaintenanceWindow.objects.create(
            user=self.context["request"].user, **validated_data
        )
        window.monitors.set(monitors)
        return window

    def update(self, instance, validated_data):
        monitors = validated_data.pop("monitors", None)
        for field, value in validated_data.items():
            setattr(instance, field, value)
        instance.save()
        if monitors is not None:
            instance.monitors.set(monitors)
        return instance


@extend_schema_view(
    list=extend_schema(
        tags=["maintenance-windows"],
        parameters=[OpenApiParameter("state", enum=["upcoming", "active", "past"])],
    ),
    create=extend_schema(tags=["maintenance-windows"]),
    retrieve=extend_schema(tags=["maintenance-windows"]),
    partial_update=extend_schema(tags=["maintenance-windows"]),
    destroy=extend_schema(tags=["maintenance-windows"]),
)
class MaintenanceWindowViewSet(ModelViewSet):
    serializer_class = MaintenanceWindowSerializer
    lookup_value_regex = "[0-9a-fA-F-]{36}"
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return MaintenanceWindow.objects.none()
        qs = MaintenanceWindow.objects.filter(user=self.request.user).prefetch_related("monitors")
        now = timezone.now()
        state = self.request.query_params.get("state")
        if state == "active":
            qs = qs.filter(starts_at__lte=now, ends_at__gt=now)
        elif state == "upcoming":
            qs = qs.filter(starts_at__gt=now)
        elif state == "past":
            qs = qs.filter(ends_at__lte=now)
        elif state:
            raise ValidationError({"state": "Use upcoming, active or past."})
        return qs.order_by("-starts_at")

    def perform_create(self, serializer):
        if MaintenanceWindow.objects.filter(user=self.request.user).count() >= MAX_WINDOWS_PER_USER:
            raise ValidationError("You have too many maintenance windows. Delete old ones first.")
        serializer.save()
