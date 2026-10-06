from __future__ import annotations

from rest_framework import serializers

from .models import ApiKey


class ApiKeySerializer(serializers.ModelSerializer):
    active = serializers.BooleanField(read_only=True)

    class Meta:
        model = ApiKey
        fields = [
            "id",
            "name",
            "prefix",
            "scope",
            "active",
            "last_used_at",
            "revoked_at",
            "created_at",
        ]
        read_only_fields = [f for f in fields if f not in ("name", "scope")]

    def validate_name(self, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise serializers.ValidationError("Give the key a name, so you know what it's for.")
        return value


class ApiKeyCreatedSerializer(ApiKeySerializer):
    """The one response that carries the key itself. It is never returned again."""

    key = serializers.CharField(read_only=True)

    class Meta(ApiKeySerializer.Meta):
        fields = [*ApiKeySerializer.Meta.fields, "key"]
        read_only_fields = [f for f in fields if f not in ("name", "scope")]
