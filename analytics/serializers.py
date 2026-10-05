"""Response shapes, written down so the OpenAPI schema (and the frontend's types) have them.
The views build plain dicts; these only describe them."""

from rest_framework import serializers


class TimingSerializer(serializers.Serializer):
    samples = serializers.IntegerField()
    dns_ms = serializers.IntegerField(allow_null=True)
    tcp_ms = serializers.IntegerField(allow_null=True)
    tls_ms = serializers.IntegerField(allow_null=True)
    ttfb_ms = serializers.IntegerField(allow_null=True)


class IncidentFiguresSerializer(serializers.Serializer):
    incidents = serializers.IntegerField(help_text="Incidents that began in the range.")
    downtime_seconds = serializers.IntegerField()
    mttr_seconds = serializers.IntegerField(allow_null=True)
    longest_incident_seconds = serializers.IntegerField()
    excluded_incidents = serializers.IntegerField(
        help_text="Incidents left out because they were excluded from reports."
    )


class AnalyticsSummarySerializer(IncidentFiguresSerializer):
    uptime_percent = serializers.FloatField(allow_null=True)
    checks = serializers.IntegerField()
    avg_response_ms = serializers.IntegerField(allow_null=True)
    p50_ms = serializers.IntegerField(allow_null=True)
    p95_ms = serializers.IntegerField(allow_null=True)


class MonitorPointSerializer(serializers.Serializer):
    t = serializers.CharField(help_text="Start of the hour (ISO time) or the day (ISO date).")
    checks = serializers.IntegerField()
    down = serializers.IntegerField()
    uptime_percent = serializers.FloatField(allow_null=True)
    avg_response_ms = serializers.IntegerField(allow_null=True)
    p50_ms = serializers.IntegerField(allow_null=True)
    p95_ms = serializers.IntegerField(allow_null=True)


class MonitorAnalyticsSerializer(serializers.Serializer):
    range = serializers.ChoiceField(choices=["24h", "7d", "30d", "90d"])
    granularity = serializers.ChoiceField(choices=["hour", "day"])
    start = serializers.CharField()
    end = serializers.CharField()
    summary = AnalyticsSummarySerializer()
    timing = TimingSerializer()
    series = MonitorPointSerializer(many=True)


class FleetDaySerializer(serializers.Serializer):
    date = serializers.CharField()
    uptime_percent = serializers.FloatField(allow_null=True)
    checks = serializers.IntegerField()
    incidents = serializers.IntegerField()
    downtime_seconds = serializers.IntegerField()


class FleetSummarySerializer(IncidentFiguresSerializer):
    monitors = serializers.IntegerField(help_text="Monitors that had checks in the range.")
    uptime_percent = serializers.FloatField(allow_null=True)
    checks = serializers.IntegerField()
    avg_response_ms = serializers.IntegerField(allow_null=True)
    deploy_linked_incidents = serializers.IntegerField(
        help_text="Incidents that began shortly after a deploy of the monitor's service."
    )
    deploy_linked_percent = serializers.FloatField(allow_null=True)


class OffenderMonitorSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    type = serializers.CharField()
    target = serializers.CharField(allow_blank=True)


class OffenderSerializer(serializers.Serializer):
    monitor = OffenderMonitorSerializer()
    downtime_seconds = serializers.IntegerField()
    incidents = serializers.IntegerField()
    uptime_percent = serializers.FloatField(allow_null=True)


class FleetAnalyticsSerializer(serializers.Serializer):
    range = serializers.ChoiceField(choices=["7d", "30d", "90d"])
    start = serializers.CharField()
    end = serializers.CharField()
    summary = FleetSummarySerializer()
    daily = FleetDaySerializer(many=True)
    top_offenders = OffenderSerializer(many=True)
    data_as_of = serializers.CharField(
        allow_null=True, help_text="When the fleet figures were last rebuilt."
    )
