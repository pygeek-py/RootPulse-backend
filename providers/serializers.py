from __future__ import annotations

from datetime import timedelta

from django.utils import timezone
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from monitoring.models import AlertContact

from .models import Provider, ProviderIncident, UserProviderSubscription

STALE_AFTER = timedelta(hours=2)


class SubscriptionSerializer(serializers.ModelSerializer):
    alert_contact_ids = serializers.SerializerMethodField()

    class Meta:
        model = UserProviderSubscription
        fields = ["min_impact", "alert_contact_ids", "created_at"]
        read_only_fields = fields

    @extend_schema_field(serializers.ListField(child=serializers.UUIDField()))
    def get_alert_contact_ids(self, obj: UserProviderSubscription):
        return sorted(str(c.id) for c in obj.alert_contacts.all())


class ComponentSerializer(serializers.Serializer):
    name = serializers.CharField()
    status = serializers.CharField()


class UpdateSerializer(serializers.Serializer):
    stage = serializers.CharField()
    body = serializers.CharField(allow_blank=True)
    at = serializers.CharField(allow_blank=True)


class ProviderBriefSerializer(serializers.ModelSerializer):
    class Meta:
        model = Provider
        fields = ["slug", "name", "category", "status_page_url"]
        read_only_fields = fields


class ProviderIncidentSerializer(serializers.ModelSerializer):
    provider = ProviderBriefSerializer(read_only=True)
    ongoing = serializers.BooleanField(read_only=True)
    duration_seconds = serializers.SerializerMethodField()
    updates = UpdateSerializer(many=True, read_only=True)

    class Meta:
        model = ProviderIncident
        fields = [
            "id",
            "provider",
            "title",
            "impact",
            "stage",
            "started_at",
            "ended_at",
            "ongoing",
            "duration_seconds",
            "url",
            "updates",
            "affected",
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.IntegerField())
    def get_duration_seconds(self, obj: ProviderIncident) -> int:
        end = obj.ended_at or timezone.now()
        return max(0, round((end - obj.started_at).total_seconds()))


class ProviderSerializer(serializers.ModelSerializer):
    ongoing_incidents = serializers.IntegerField(read_only=True, default=0)
    subscribed = serializers.SerializerMethodField()
    subscription = serializers.SerializerMethodField()
    stale = serializers.SerializerMethodField(
        help_text="True when we haven't been able to read this status page recently."
    )

    class Meta:
        model = Provider
        fields = [
            "slug",
            "name",
            "category",
            "description",
            "website_url",
            "status_page_url",
            "status",
            "status_description",
            "status_checked_at",
            "last_success_at",
            "stale",
            "ongoing_incidents",
            "subscribed",
            "subscription",
        ]
        read_only_fields = fields

    def _mine(self, obj: Provider):
        subs = getattr(obj, "my_subscriptions", None)
        return subs[0] if subs else None

    @extend_schema_field(serializers.BooleanField())
    def get_subscribed(self, obj: Provider) -> bool:
        return self._mine(obj) is not None

    @extend_schema_field(SubscriptionSerializer(allow_null=True))
    def get_subscription(self, obj: Provider):
        mine = self._mine(obj)
        return SubscriptionSerializer(mine).data if mine else None

    @extend_schema_field(serializers.BooleanField())
    def get_stale(self, obj: Provider) -> bool:
        if obj.consecutive_failures >= 3 or obj.last_success_at is None:
            return True
        return obj.last_success_at < timezone.now() - STALE_AFTER


class ProviderDetailSerializer(ProviderSerializer):
    components = ComponentSerializer(many=True, read_only=True)
    recent_incidents = serializers.SerializerMethodField()

    class Meta(ProviderSerializer.Meta):
        fields = [
            *ProviderSerializer.Meta.fields,
            "components",
            "component_count",
            "recent_incidents",
        ]
        read_only_fields = fields

    @extend_schema_field(ProviderIncidentSerializer(many=True))
    def get_recent_incidents(self, obj: Provider):
        recent = obj.incidents.select_related("provider").order_by("-started_at")[:10]
        return ProviderIncidentSerializer(recent, many=True).data


class SubscribeSerializer(serializers.Serializer):
    min_impact = serializers.ChoiceField(
        choices=UserProviderSubscription.MinImpact.choices, required=False
    )
    alert_contact_ids = serializers.ListField(
        child=serializers.UUIDField(), required=False, max_length=50
    )

    def validate_alert_contact_ids(self, ids):
        request = self.context["request"]
        wanted = set(ids)
        owned = list(AlertContact.objects.filter(user=request.user, id__in=wanted))
        if len(owned) != len(wanted):
            raise serializers.ValidationError("One of those contacts isn't yours.")
        return owned
