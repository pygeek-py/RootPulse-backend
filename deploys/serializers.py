from __future__ import annotations

import secrets

from django.conf import settings
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .models import Deploy, DeploySource

#: Sources whose signing secret we generate. For Vercel and Render the provider issues the
#: secret, so the person pastes it in.
GENERATED = {DeploySource.Type.GITHUB, DeploySource.Type.GENERIC}


def webhook_url(source: DeploySource) -> str:
    return f"{settings.API_PUBLIC_URL}/api/v1/deploys/webhook/{source.type}/{source.token}/"


class DeploySourceSerializer(serializers.ModelSerializer):
    webhook_url = serializers.SerializerMethodField()
    secret = serializers.SerializerMethodField(
        help_text="The signing secret: shown once, when it is generated (create or rotate)."
    )
    signing_secret = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=False,
        max_length=255,
        help_text="Vercel and Render only: the secret the provider issued.",
    )
    deploy_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = DeploySource
        fields = [
            "id",
            "name",
            "type",
            "webhook_url",
            "environment_filter",
            "enabled",
            "last_received_at",
            "deploy_count",
            "secret",
            "signing_secret",
            "created_at",
        ]
        read_only_fields = ["id", "last_received_at", "created_at"]

    @extend_schema_field(serializers.CharField())
    def get_webhook_url(self, obj: DeploySource) -> str:
        return webhook_url(obj)

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_secret(self, obj: DeploySource):
        return obj.secret if getattr(obj, "reveal_secret", False) else None

    def validate_name(self, value: str) -> str:
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Give this connection a name.")
        return value

    def validate_environment_filter(self, value: str) -> str:
        return value.strip()

    def validate(self, attrs):
        editing = self.instance is not None
        if editing and "type" in attrs and attrs["type"] != self.instance.type:
            raise serializers.ValidationError(
                {"type": "A connection's type can't be changed. Add a new one instead."}
            )
        kind = self.instance.type if editing else attrs.get("type")
        provided = attrs.get("signing_secret")
        if kind in GENERATED:
            if provided:
                raise serializers.ValidationError(
                    {"signing_secret": "RootPulse generates this secret for you."}
                )
        elif not editing and not provided:
            raise serializers.ValidationError(
                {"signing_secret": f"Paste the signing secret {kind.title()} gave you."}
            )
        return attrs

    def create(self, validated_data):
        provided = validated_data.pop("signing_secret", None)
        kind = validated_data["type"]
        secret = secrets.token_urlsafe(32) if kind in GENERATED else provided
        return DeploySource.objects.create(secret=secret, **validated_data)

    def update(self, instance, validated_data):
        provided = validated_data.pop("signing_secret", None)
        if provided:
            instance.secret = provided
        return super().update(instance, validated_data)


class DeploySerializer(serializers.ModelSerializer):
    class Meta:
        model = Deploy
        fields = [
            "id",
            "source_type",
            "source_name",
            "service_name",
            "environment",
            "version",
            "url",
            "occurred_at",
            "received_at",
        ]
        read_only_fields = fields


class DeployBriefSerializer(serializers.ModelSerializer):
    """The deploy as an incident shows it."""

    seconds_before_incident = serializers.SerializerMethodField()

    class Meta:
        model = Deploy
        fields = [
            "id",
            "source_type",
            "source_name",
            "service_name",
            "environment",
            "version",
            "url",
            "occurred_at",
            "seconds_before_incident",
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.IntegerField(allow_null=True))
    def get_seconds_before_incident(self, obj: Deploy):
        incident = self.context.get("incident")
        if incident is None:
            return None
        return max(0, round((incident.started_at - obj.occurred_at).total_seconds()))


class WebhookResultSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=["recorded", "duplicate", "ignored"])
    deploy_id = serializers.UUIDField(allow_null=True, required=False)
    reason = serializers.CharField(required=False)
