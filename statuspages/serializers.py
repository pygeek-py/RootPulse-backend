from __future__ import annotations

import re
from datetime import timedelta

from django.utils import timezone
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from monitoring.models import Monitor

from . import services
from .models import (
    StatusPage,
    StatusPageAnnouncement,
    StatusPageComponent,
    StatusPageSubscriber,
)

SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
ACCENT_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
MAX_MAINTENANCE = timedelta(days=30)
MAX_ANNOUNCEMENTS = 200
# Named once, so the generated client has a single AnnouncementStateEnum.
ANNOUNCEMENT_STATES = [
    (value, value) for value in ("active", "resolved", "upcoming", "in_progress", "completed")
]


def _single_line(value: str) -> str:
    return " ".join(value.split())


class BrandingSerializer(serializers.Serializer):
    """The page's look. Only these keys, each checked, so nothing here can carry markup or a
    script: colours are #rrggbb, links are https, text is shown as plain text."""

    accent = serializers.CharField(required=False, allow_blank=True, max_length=7)
    logo_url = serializers.URLField(required=False, allow_blank=True, max_length=500)
    description = serializers.CharField(required=False, allow_blank=True, max_length=280)
    support_url = serializers.URLField(required=False, allow_blank=True, max_length=500)

    def validate_accent(self, value: str) -> str:
        if value and not ACCENT_RE.match(value):
            raise serializers.ValidationError("Use a colour like #2563eb.")
        return value.lower()

    def _https(self, value: str) -> str:
        if value and not value.lower().startswith("https://"):
            raise serializers.ValidationError("The link has to start with https://.")
        return value

    def validate_logo_url(self, value):
        return self._https(value)

    def validate_support_url(self, value):
        return self._https(value)

    def validate_description(self, value):
        return value.strip()


class StatusPageSerializer(serializers.ModelSerializer):
    branding = BrandingSerializer(required=False)
    has_password = serializers.BooleanField(read_only=True)
    # Write-only. A string sets the password, null removes it; it is never sent back.
    password = serializers.CharField(
        write_only=True, required=False, allow_null=True, min_length=6, max_length=128
    )
    url = serializers.SerializerMethodField()
    component_count = serializers.SerializerMethodField()
    subscriber_count = serializers.SerializerMethodField()

    class Meta:
        model = StatusPage
        fields = [
            "id",
            "name",
            "slug",
            "is_public",
            "branding",
            "has_password",
            "password",
            "url",
            "component_count",
            "subscriber_count",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]
        # The address rules (and their wording) are validate_slug's, not Django's defaults.
        extra_kwargs = {"slug": {"validators": []}}

    @extend_schema_field(serializers.URLField())
    def get_url(self, obj: StatusPage) -> str:
        return services.page_url(obj)

    def get_component_count(self, obj: StatusPage) -> int:
        count = getattr(obj, "component_total", None)
        return count if count is not None else obj.components.count()

    def get_subscriber_count(self, obj: StatusPage) -> int:
        count = getattr(obj, "subscriber_total", None)
        return (
            count
            if count is not None
            else obj.subscribers.filter(confirmed_at__isnull=False).count()
        )

    def validate_name(self, value: str) -> str:
        value = _single_line(value)
        if not value:
            raise serializers.ValidationError("Give the page a name.")
        return value

    def validate_slug(self, value: str) -> str:
        value = value.strip().lower()
        if len(value) < 3:
            raise serializers.ValidationError("Use at least 3 characters.")
        if not SLUG_RE.match(value):
            raise serializers.ValidationError(
                "Use lowercase letters, numbers and hyphens, not starting or ending with one."
            )
        taken = StatusPage.objects.filter(slug=value)
        if self.instance is not None:
            taken = taken.exclude(pk=self.instance.pk)
        if taken.exists():
            raise serializers.ValidationError("That address is taken.")
        return value

    def create(self, validated_data):
        password = validated_data.pop("password", None)
        branding = validated_data.pop("branding", {})
        page = StatusPage(user=self.context["request"].user, **validated_data)
        page.branding = dict(branding)
        services.set_password(page, password)
        page.save()
        return page

    def update(self, instance, validated_data):
        if "password" in validated_data:
            services.set_password(instance, validated_data.pop("password"))
        if "branding" in validated_data:
            instance.branding = {**services.branding_of(instance), **validated_data.pop("branding")}
        return super().update(instance, validated_data)


class PageMonitorSerializer(serializers.ModelSerializer):
    class Meta:
        model = Monitor
        fields = ["id", "name", "type", "status"]
        read_only_fields = fields


class OwnMonitorField(serializers.PrimaryKeyRelatedField):
    def get_queryset(self):
        request = self.context.get("request")
        if request is None or not request.user.is_authenticated:
            return Monitor.objects.none()
        return Monitor.objects.filter(user=request.user)


class PageComponentSerializer(serializers.ModelSerializer):
    monitor_id = OwnMonitorField(source="monitor", write_only=True, required=True)
    monitor = PageMonitorSerializer(read_only=True)
    display_name = serializers.CharField(max_length=80, required=False, allow_blank=True)
    group = serializers.CharField(max_length=60, required=False, allow_blank=True)

    class Meta:
        model = StatusPageComponent
        fields = ["id", "monitor_id", "monitor", "display_name", "group", "order"]
        read_only_fields = ["id", "order"]

    def validate_display_name(self, value: str) -> str:
        return _single_line(value)

    def validate_group(self, value: str) -> str:
        return _single_line(value)

    def validate(self, attrs):
        if self.instance is not None:
            attrs.pop("monitor", None)  # which monitor it shows can't be changed, only its label
            if "display_name" in attrs and not attrs["display_name"]:
                raise serializers.ValidationError({"display_name": "A component needs a name."})
        return attrs


class ReorderSerializer(serializers.Serializer):
    ids = serializers.ListField(child=serializers.UUIDField(), allow_empty=False)


class AnnouncementSerializer(serializers.ModelSerializer):
    state = serializers.SerializerMethodField()
    # Write-only, on create: email the confirmed subscribers (default yes).
    notify = serializers.BooleanField(write_only=True, required=False, default=True)

    class Meta:
        model = StatusPageAnnouncement
        fields = [
            "id",
            "kind",
            "impact",
            "title",
            "body",
            "starts_at",
            "ends_at",
            "resolved_at",
            "state",
            "notify",
            "created_at",
        ]
        read_only_fields = ["id", "resolved_at", "created_at"]

    @extend_schema_field(serializers.ChoiceField(choices=ANNOUNCEMENT_STATES))
    def get_state(self, obj: StatusPageAnnouncement) -> str:
        return services.announcement_state(obj, timezone.now())

    def validate_title(self, value: str) -> str:
        value = _single_line(value)
        if not value:
            raise serializers.ValidationError("Give it a title.")
        return value

    def validate(self, attrs):
        if self.instance is not None and "kind" in attrs and attrs["kind"] != self.instance.kind:
            raise serializers.ValidationError(
                {"kind": "You can't change the kind. Delete it and post again."}
            )
        kind = attrs.get("kind", getattr(self.instance, "kind", None))
        starts = attrs.get("starts_at", getattr(self.instance, "starts_at", None))
        ends = attrs.get("ends_at", getattr(self.instance, "ends_at", None))
        if kind == StatusPageAnnouncement.Kind.MAINTENANCE:
            if not starts or not ends:
                raise serializers.ValidationError(
                    {"starts_at": "Say when the maintenance starts and ends."}
                )
            if ends <= starts:
                raise serializers.ValidationError({"ends_at": "It has to end after it starts."})
            if ends - starts > MAX_MAINTENANCE:
                raise serializers.ValidationError({"ends_at": "Keep maintenance under 30 days."})
        else:
            attrs["starts_at"] = None
            attrs["ends_at"] = None
        return attrs


class SubscriberSerializer(serializers.ModelSerializer):
    confirmed = serializers.BooleanField(read_only=True)

    class Meta:
        model = StatusPageSubscriber
        fields = ["id", "email", "confirmed", "created_at"]
        read_only_fields = fields


# --- the public page (schema only: the payload is built by services.build_public) ----------


class PublicBrandingSerializer(serializers.Serializer):
    accent = serializers.CharField()
    logo_url = serializers.CharField()
    description = serializers.CharField()
    support_url = serializers.CharField()


class PublicDaySerializer(serializers.Serializer):
    date = serializers.DateField()
    uptime_percent = serializers.FloatField(allow_null=True)


class PublicComponentSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    group = serializers.CharField()
    status = serializers.ChoiceField(
        choices=["operational", "major_outage", "maintenance", "paused", "unknown"]
    )
    uptime_percent = serializers.FloatField(allow_null=True)
    days = PublicDaySerializer(many=True)


class PublicAnnouncementSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    kind = serializers.ChoiceField(choices=StatusPageAnnouncement.Kind.choices)
    impact = serializers.ChoiceField(choices=StatusPageAnnouncement.Impact.choices)
    title = serializers.CharField()
    body = serializers.CharField()
    state = serializers.ChoiceField(choices=ANNOUNCEMENT_STATES)
    starts_at = serializers.DateTimeField(allow_null=True)
    ends_at = serializers.DateTimeField(allow_null=True)
    resolved_at = serializers.DateTimeField(allow_null=True)
    created_at = serializers.DateTimeField()


class PublicAnnouncementsSerializer(serializers.Serializer):
    active = PublicAnnouncementSerializer(many=True)
    past = PublicAnnouncementSerializer(many=True)


class PublicUpdateSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    body = serializers.CharField()
    at = serializers.DateTimeField()


class PublicIncidentSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    component = serializers.CharField()
    started_at = serializers.DateTimeField()
    ended_at = serializers.DateTimeField(allow_null=True)
    ongoing = serializers.BooleanField()
    updates = PublicUpdateSerializer(many=True)


class PublicOverallSerializer(serializers.Serializer):
    status = serializers.ChoiceField(
        choices=["operational", "partial_outage", "major_outage", "maintenance", "unknown"]
    )


class PublicStatusPageSerializer(serializers.Serializer):
    """`password_required` pages send only name, slug, branding and that flag until unlocked."""

    name = serializers.CharField()
    slug = serializers.CharField()
    branding = PublicBrandingSerializer()
    password_required = serializers.BooleanField()
    generated_at = serializers.DateTimeField(required=False)
    overall = PublicOverallSerializer(required=False)
    components = PublicComponentSerializer(many=True, required=False)
    announcements = PublicAnnouncementsSerializer(required=False)
    incidents = PublicIncidentSerializer(many=True, required=False)
    uptime_days = serializers.IntegerField(required=False)


class UnlockSerializer(serializers.Serializer):
    password = serializers.CharField(max_length=128)


class UnlockResultSerializer(serializers.Serializer):
    token = serializers.CharField()
    expires_in = serializers.IntegerField()


class PageSubscribeSerializer(serializers.Serializer):
    email = serializers.EmailField(max_length=254)


class MessageSerializer(serializers.Serializer):
    detail = serializers.CharField()


class SubscriptionInfoSerializer(serializers.Serializer):
    page_name = serializers.CharField()
    page_slug = serializers.CharField()
    email = serializers.CharField(help_text="Masked, for example a***@example.com.")
