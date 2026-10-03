from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from rest_framework import serializers

from .models import User


class UserSerializer(serializers.ModelSerializer):
    has_github = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ["id", "email", "timezone", "has_github", "created_at"]
        read_only_fields = fields

    def get_has_github(self, obj: User) -> bool:
        return bool(obj.github_id)


class UserUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["timezone"]

    def validate_timezone(self, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise serializers.ValidationError("Unknown time zone.") from exc
        return value


class EmailStartSerializer(serializers.Serializer):
    """Body for both sign-up and log-in: just an email address."""

    email = serializers.EmailField(max_length=254)

    def validate_email(self, value: str) -> str:
        return value.strip().lower()


class VerifySerializer(serializers.Serializer):
    """Either the emailed link's `token`, or the `email` + 6-digit `code`."""

    token = serializers.CharField(max_length=256, required=False)
    email = serializers.EmailField(max_length=254, required=False)
    code = serializers.RegexField(
        r"^\d{6}$", required=False, error_messages={"invalid": "Enter the 6-digit code."}
    )

    def validate_email(self, value: str) -> str:
        return value.strip().lower()

    def validate(self, attrs):
        if attrs.get("token"):
            return attrs
        if attrs.get("email") and attrs.get("code"):
            return attrs
        raise serializers.ValidationError("Provide the link token, or your email and 6-digit code.")


class AuthResponseSerializer(serializers.Serializer):
    """Documents the login/register/refresh response body for the OpenAPI schema."""

    user = UserSerializer()
    access_token = serializers.CharField()


class RefreshResponseSerializer(serializers.Serializer):
    access_token = serializers.CharField()
