from __future__ import annotations

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from deploys.serializers import DeployBriefSerializer
from monitoring.models import Monitor
from monitoring.serializers import CheckSerializer
from notifications.serializers import DeliverySerializer

from . import services
from .models import Incident, IncidentComment, IncidentEvent


class MonitorBriefSerializer(serializers.ModelSerializer):
    class Meta:
        model = Monitor
        fields = ["id", "name", "type", "target"]
        read_only_fields = fields


class StageFigureSerializer(serializers.Serializer):
    stage = serializers.ChoiceField(choices=["dns", "tcp", "tls", "server"])
    ms = serializers.IntegerField(allow_null=True)
    baseline_ms = serializers.IntegerField(allow_null=True)
    regressed = serializers.BooleanField()


class RootCauseSerializer(serializers.Serializer):
    """Where the failing request went wrong. `basis` says how we know: the failure named the
    stage, one stage ran much slower than usual, or the server answered with an error."""

    stage = serializers.CharField(allow_blank=True)
    basis = serializers.ChoiceField(choices=["failure", "timing", "response", "unknown"])
    stages = StageFigureSerializer(many=True)


class IncidentSerializer(serializers.ModelSerializer):
    """One row of the incident list."""

    monitor = MonitorBriefSerializer(read_only=True)
    deploy = serializers.SerializerMethodField()
    ongoing = serializers.BooleanField(read_only=True)
    duration_seconds = serializers.SerializerMethodField()
    comment_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = Incident
        fields = [
            "id",
            "monitor",
            "started_at",
            "ended_at",
            "ongoing",
            "duration_seconds",
            "reason",
            "status_code",
            "resolution",
            "excluded_from_reports",
            "root_cause_stage",
            "deploy",
            "comment_count",
        ]
        read_only_fields = fields

    @extend_schema_field(DeployBriefSerializer(allow_null=True))
    def get_deploy(self, obj: Incident):
        if not obj.deploy_id:
            return None
        return DeployBriefSerializer(obj.deploy, context={"incident": obj}).data

    @extend_schema_field(serializers.IntegerField())
    def get_duration_seconds(self, obj: Incident) -> int:
        return services.duration_seconds(obj)


class IncidentEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = IncidentEvent
        fields = ["id", "kind", "metadata", "created_at"]
        read_only_fields = fields


class CommentSerializer(serializers.ModelSerializer):
    author_email = serializers.SerializerMethodField()
    is_mine = serializers.SerializerMethodField()

    class Meta:
        model = IncidentComment
        fields = [
            "id",
            "author_email",
            "is_mine",
            "body",
            "visible_on_status_page",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "author_email", "is_mine", "created_at", "updated_at"]

    @extend_schema_field(serializers.EmailField(allow_null=True))
    def get_author_email(self, obj: IncidentComment):
        return obj.author.email if obj.author else None

    @extend_schema_field(serializers.BooleanField())
    def get_is_mine(self, obj: IncidentComment) -> bool:
        request = self.context.get("request")
        return bool(request and obj.author_id == request.user.pk)

    def validate_body(self, value: str) -> str:
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Write something first.")
        if len(value) > 5000:
            raise serializers.ValidationError("Keep it under 5000 characters.")
        return value


class CommentCreateSerializer(CommentSerializer):
    class Meta(CommentSerializer.Meta):
        extra_kwargs = {"visible_on_status_page": {"required": False}}


class IncidentDetailSerializer(IncidentSerializer):
    """The full incident: timeline, comments and the check that opened it."""

    events = IncidentEventSerializer(many=True, read_only=True)
    comments = serializers.SerializerMethodField()
    opening_check = CheckSerializer(source="opened_by", read_only=True, allow_null=True)
    notifications = serializers.SerializerMethodField()
    root_cause = serializers.SerializerMethodField()

    class Meta(IncidentSerializer.Meta):
        fields = [
            *IncidentSerializer.Meta.fields,
            "events",
            "comments",
            "opening_check",
            "notifications",
            "root_cause",
        ]
        read_only_fields = fields

    @extend_schema_field(RootCauseSerializer(allow_null=True))
    def get_root_cause(self, obj: Incident):
        return obj.root_cause or None

    @extend_schema_field(DeliverySerializer(many=True))
    def get_notifications(self, obj: Incident):
        deliveries = obj.deliveries.select_related("monitor").order_by("created_at", "id")
        return DeliverySerializer(deliveries, many=True).data

    @extend_schema_field(CommentSerializer(many=True))
    def get_comments(self, obj: Incident):
        return CommentSerializer(obj.comments.all(), many=True, context=self.context).data


class IncidentUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Incident
        fields = ["excluded_from_reports"]


class PostmortemSerializer(serializers.Serializer):
    markdown = serializers.CharField()
    filename = serializers.CharField()
