"""What a check run produces, independent of where it ran (the API or a prober)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# --- outcomes ("status_detail") ------------------------------------------
OK = "ok"
TIMEOUT = "timeout"
DNS_ERROR = "dns_error"
CONNECTION_REFUSED = "connection_refused"
CONNECTION_ERROR = "connection_error"
TLS_ERROR = "tls_error"
STATUS_MISMATCH = "status_mismatch"
KEYWORD_MISSING = "keyword_missing"
KEYWORD_FOUND = "keyword_found"  # present, but the monitor expects it absent
TOO_MANY_REDIRECTS = "too_many_redirects"
CERT_EXPIRING = "certificate_expiring"
CERT_EXPIRED = "certificate_expired"
DOMAIN_EXPIRING = "domain_expiring"
DNS_CHANGED = "dns_changed"
DNS_BASELINE = "dns_baseline"
HEARTBEAT_MISSED = "heartbeat_missed"
HEARTBEAT_WAITING = "heartbeat_waiting"
# Inconclusive: our side or an unreadable result. Never changes a monitor's status.
BLOCKED_TARGET = "blocked_target"
LOOKUP_UNAVAILABLE = "lookup_unavailable"
INTERNAL_ERROR = "internal_error"
PROBER_UNAVAILABLE = "prober_unavailable"


@dataclass
class CheckResult:
    """success: True up, False down, None inconclusive (see Check.success)."""

    success: bool | None
    status_detail: str
    status_code: int | None = None
    duration_ms: int | None = None
    dns_ms: int | None = None
    tcp_ms: int | None = None
    tls_ms: int | None = None
    ttfb_ms: int | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def inconclusive(cls, status_detail: str, **detail: Any) -> CheckResult:
        return cls(success=None, status_detail=status_detail, detail=dict(detail))

    def as_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "status_detail": self.status_detail,
            "status_code": self.status_code,
            "duration_ms": self.duration_ms,
            "dns_ms": self.dns_ms,
            "tcp_ms": self.tcp_ms,
            "tls_ms": self.tls_ms,
            "ttfb_ms": self.ttfb_ms,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CheckResult:
        """Parse a prober's JSON reply, trusting only the fields we know."""

        def number(key: str) -> int | None:
            value = data.get(key)
            return (
                int(value)
                if isinstance(value, (int, float)) and not isinstance(value, bool)
                else None
            )

        success = data.get("success")
        return cls(
            success=success if isinstance(success, bool) else None,
            status_detail=str(data.get("status_detail", ""))[:64],
            status_code=number("status_code"),
            duration_ms=number("duration_ms"),
            dns_ms=number("dns_ms"),
            tcp_ms=number("tcp_ms"),
            tls_ms=number("tls_ms"),
            ttfb_ms=number("ttfb_ms"),
            detail=data.get("detail") if isinstance(data.get("detail"), dict) else {},
        )


def elapsed_ms(start: float, end: float) -> int:
    return max(0, round((end - start) * 1000))
