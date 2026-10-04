from __future__ import annotations

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from monitoring.models import Monitor
from monitoring.serializers import CheckSerializer

from . import services
from .models import Incident, IncidentComment, IncidentEvent


class MonitorBriefSerializer(serializers.ModelSerializer):
    class Meta:
        model = Monitor
        fields = ["id", "name", "type", "target"]
        read_only_fields = fields


class IncidentSerializer(serializers.ModelSerializer):
    """One row of the incident list."""

    monitor = MonitorBriefSerializer(read_only=True)
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
            "comment_count",
        ]
        read_only_fields = fields

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

    class Meta(IncidentSerializer.Meta):
        fields = [*IncidentSerializer.Meta.fields, "events", "comments", "opening_check"]
        read_only_fields = fields

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
