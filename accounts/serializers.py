import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from .models import User


def validate_new_password(password: str, user: User | None, field: str) -> str:
    """Run Django's password validators; errors are attached to `field` so the
    form can show them under the right input."""
    try:
        validate_password(password, user=user)
    except DjangoValidationError as exc:
        raise serializers.ValidationError({field: list(exc.messages)}) from exc
    return password


class UserSerializer(serializers.ModelSerializer):
    has_password = serializers.SerializerMethodField()
    has_github = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ["id", "email", "timezone", "has_password", "has_github", "created_at"]
        read_only_fields = fields

    def get_has_password(self, obj: User) -> bool:
        return obj.has_usable_password()

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


class RegisterSerializer(serializers.Serializer):
    email = serializers.EmailField(max_length=254)
    password = serializers.CharField(write_only=True, max_length=128, trim_whitespace=False)

    def validate_email(self, value: str) -> str:
        email = value.strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            # 409 per docs/plan/02-database-and-api.md; raised as a conflict
            # in the view so the status code isn't a generic 400.
            raise serializers.ValidationError("An account with this email already exists.")
        return email

    def validate(self, attrs):
        # Run the similarity validator against the email being registered.
        validate_new_password(attrs["password"], User(email=attrs["email"]), "password")
        return attrs

    def create(self, validated_data) -> User:
        email = validated_data["email"]
        # `username` is required by AbstractUser but unused: login is by email.
        return User.objects.create_user(
            username=uuid.uuid4().hex, email=email, password=validated_data["password"]
        )


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField(max_length=254)
    password = serializers.CharField(write_only=True, max_length=128, trim_whitespace=False)

    def validate_email(self, value: str) -> str:
        return value.strip().lower()


class PasswordForgotSerializer(serializers.Serializer):
    email = serializers.EmailField(max_length=254)

    def validate_email(self, value: str) -> str:
        return value.strip().lower()


class PasswordResetSerializer(serializers.Serializer):
    token = serializers.CharField(max_length=256)
    new_password = serializers.CharField(write_only=True, max_length=128, trim_whitespace=False)


class AuthResponseSerializer(serializers.Serializer):
    """Documents the login/register/refresh response body for the OpenAPI schema."""

    user = UserSerializer()
    access_token = serializers.CharField()


class RefreshResponseSerializer(serializers.Serializer):
    access_token = serializers.CharField()
