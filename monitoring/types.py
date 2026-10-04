"""What each monitor type means: its target rules, config shape and allowed intervals.

One table (`SPECS`) drives validation, defaults and the OpenAPI schema, so the
frontend's generated types can't drift from what the API accepts
(docs/plan/03-monitoring-engine.md section 13).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from rest_framework import serializers

from . import target_validation as tv
from .models import Monitor

# The scheduler runs from a 5-minute cron (docs/plan/03-monitoring-engine.md), so
# nothing shorter is meaningful. Slow-moving checks (SSL, domain expiry) don't
# need polling more often than hourly.
INTERVALS = (300, 600, 900, 1800, 3600, 7200, 21600, 43200, 86400)
SLOW_INTERVALS = tuple(i for i in INTERVALS if i >= 3600)

HTTP_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")
BODY_METHODS = ("POST", "PUT", "PATCH", "DELETE")
# Headers the HTTP client sets itself; letting users override them enables request smuggling.
FORBIDDEN_HEADERS = {"host", "content-length", "transfer-encoding", "connection", "upgrade", "te"}
RECORD_TYPES = ("A", "AAAA", "CNAME", "MX", "TXT", "NS")

_HEADER_NAME = r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}$"


class StrictSerializer(serializers.Serializer):
    """Rejects keys it doesn't know, so a typo can't silently store a useless setting."""

    def to_internal_value(self, data):
        if isinstance(data, dict):
            unknown = sorted(set(data) - set(self.fields))
            if unknown:
                raise serializers.ValidationError(
                    {key: "Unknown setting for this monitor type." for key in unknown}
                )
        return super().to_internal_value(data)


def _port(**kwargs):
    return serializers.IntegerField(min_value=1, max_value=65535, **kwargs)


class HttpConfigSerializer(StrictSerializer):
    method = serializers.ChoiceField(choices=HTTP_METHODS, default="GET")
    headers = serializers.DictField(
        child=serializers.CharField(max_length=1024, allow_blank=True),
        required=False,
        default=dict,
        help_text="Extra request headers (up to 10).",
    )
    body = serializers.CharField(max_length=4096, required=False, allow_blank=True, default="")
    expected_status = serializers.ListField(
        child=serializers.RegexField(r"^([1-5][0-9]{2}|[1-5]xx)$"),
        min_length=1,
        max_length=20,
        default=lambda: ["2xx", "3xx"],
        help_text='Status codes that count as up: exact ("200") or a class ("2xx").',
    )
    timeout_seconds = serializers.IntegerField(min_value=1, max_value=30, default=10)
    follow_redirects = serializers.BooleanField(default=True)

    def validate_headers(self, headers):
        if len(headers) > 10:
            raise serializers.ValidationError("Use at most 10 headers.")

        for name, value in headers.items():
            if not re.match(_HEADER_NAME, name):
                raise serializers.ValidationError(f"'{name}' isn't a valid header name.")
            if name.lower() in FORBIDDEN_HEADERS:
                raise serializers.ValidationError(f"The {name} header can't be set.")
            if any(c in value for c in "\r\n\x00"):
                raise serializers.ValidationError(f"The {name} value can't contain line breaks.")
        return headers

    def validate(self, attrs):
        if attrs.get("body") and attrs["method"] not in BODY_METHODS:
            raise serializers.ValidationError(
                {"body": "A request body needs POST, PUT, PATCH or DELETE."}
            )
        return attrs


class KeywordConfigSerializer(HttpConfigSerializer):
    keyword = serializers.CharField(min_length=1, max_length=200, trim_whitespace=False)
    exists = serializers.BooleanField(
        default=True, help_text="Up when the keyword is present (true) or absent (false)."
    )

    def validate_keyword(self, value):
        if not value.strip():
            raise serializers.ValidationError("Enter a keyword to look for.")
        return value


class PingConfigSerializer(StrictSerializer):
    # "Ping" is a TCP connect (no ICMP on Render or Workers): docs/plan/03-monitoring-engine.md.
    port = _port(default=443)


class PortConfigSerializer(StrictSerializer):
    port = _port()


class SslConfigSerializer(StrictSerializer):
    port = _port(default=443)
    warn_days = serializers.IntegerField(min_value=1, max_value=90, default=14)


class DomainConfigSerializer(StrictSerializer):
    warn_days = serializers.IntegerField(min_value=1, max_value=365, default=30)


class CronConfigSerializer(StrictSerializer):
    grace_period_seconds = serializers.IntegerField(min_value=60, max_value=86400, default=300)


class DnsConfigSerializer(StrictSerializer):
    record_types = serializers.ListField(
        child=serializers.ChoiceField(choices=RECORD_TYPES),
        min_length=1,
        max_length=len(RECORD_TYPES),
        default=lambda: ["A"],
    )

    def validate_record_types(self, value):
        if len(set(value)) != len(value):
            raise serializers.ValidationError("List each record type once.")
        return value


@dataclass(frozen=True)
class TypeSpec:
    config: type[serializers.Serializer]
    #: Validates and normalises `target`. None = the type takes no target.
    target: Callable[[str], str] | None
    intervals: tuple[int, ...]
    default_interval: int
    target_help: str


SPECS: dict[str, TypeSpec] = {
    Monitor.Type.HTTP: TypeSpec(
        HttpConfigSerializer,
        tv.validate_http_url,
        INTERVALS,
        300,
        "A full URL, e.g. https://example.com/health",
    ),
    Monitor.Type.KEYWORD: TypeSpec(
        KeywordConfigSerializer,
        tv.validate_http_url,
        INTERVALS,
        300,
        "A full URL to fetch and search",
    ),
    Monitor.Type.PING: TypeSpec(
        PingConfigSerializer, tv.validate_public_host, INTERVALS, 300, "A host name or IP address"
    ),
    Monitor.Type.PORT: TypeSpec(
        PortConfigSerializer, tv.validate_public_host, INTERVALS, 300, "A host name or IP address"
    ),
    Monitor.Type.SSL: TypeSpec(
        SslConfigSerializer, tv.validate_public_host, SLOW_INTERVALS, 86400, "A host name"
    ),
    Monitor.Type.DOMAIN: TypeSpec(
        DomainConfigSerializer,
        tv.validate_domain_name,
        SLOW_INTERVALS,
        86400,
        "A domain like example.com",
    ),
    Monitor.Type.CRON: TypeSpec(CronConfigSerializer, None, INTERVALS, 3600, ""),
    Monitor.Type.DNS: TypeSpec(
        DnsConfigSerializer, tv.validate_domain_name, INTERVALS, 3600, "A domain like example.com"
    ),
}

CONFIG_SERIALIZERS = [spec.config for spec in SPECS.values()]
