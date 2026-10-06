"""Tell callers where they stand: `X-RateLimit-Limit`, `-Remaining` and `-Reset` (seconds) on
every response from a throttled endpoint, and `Retry-After` on a 429 (DRF adds that one)."""

from __future__ import annotations

from rest_framework.throttling import AnonRateThrottle, UserRateThrottle


class _Reporting:
    def allow_request(self, request, view):
        allowed = super().allow_request(request, view)
        # With no rate configured for this scope there is nothing to report.
        if getattr(self, "rate", None) and getattr(self, "history", None) is not None:
            used = len(self.history)
            oldest = self.history[-1] if self.history else self.now
            request._request._ratelimit = (
                self.num_requests,
                max(0, self.num_requests - used),
                max(0, int(self.duration - (self.now - oldest))),
            )
        return allowed


class ReportingUserRateThrottle(_Reporting, UserRateThrottle):
    pass


class ReportingAnonRateThrottle(_Reporting, AnonRateThrottle):
    pass


class RateLimitHeadersMiddleware:
    """Copies what the throttle noted onto the response."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        info = getattr(request, "_ratelimit", None)
        if info is not None:
            limit, remaining, reset = info
            response["X-RateLimit-Limit"] = str(limit)
            response["X-RateLimit-Remaining"] = str(remaining)
            response["X-RateLimit-Reset"] = str(reset)
        return response
