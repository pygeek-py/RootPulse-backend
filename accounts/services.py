"""Auth building blocks shared by the views (docs/plan/04-security.md #1-#7)."""

from __future__ import annotations

from django.conf import settings
from django.http import HttpResponse
from rest_framework.exceptions import PermissionDenied
from rest_framework.throttling import BaseThrottle
from rest_framework_simplejwt.tokens import BlacklistedToken, OutstandingToken, RefreshToken

from .models import AuditLog, User


def client_ip(request) -> str | None:
    # Respects NUM_PROXIES, so a spoofed X-Forwarded-For can't fake an IP.
    return BaseThrottle().get_ident(request) or None


def audit(action: str, request, user: User | None = None, **metadata) -> None:
    """Record a security-relevant event. Never stores passwords or tokens."""
    AuditLog.objects.create(
        user=user,
        action=action,
        target_type="user" if user else "",
        target_id=str(user.pk) if user else "",
        metadata=metadata,
        ip_address=client_ip(request),
    )


def issue_tokens(user: User) -> tuple[RefreshToken, str]:
    """A fresh refresh token (tracked server-side, so it can be revoked) and
    the short-lived access token that goes in the response body."""
    refresh = RefreshToken.for_user(user)
    return refresh, str(refresh.access_token)


def revoke_all_sessions(user: User) -> None:
    """Blacklist every outstanding refresh token — used after a password reset."""
    for token in OutstandingToken.objects.filter(user=user):
        BlacklistedToken.objects.get_or_create(token=token)


def set_refresh_cookie(response: HttpResponse, refresh: RefreshToken) -> None:
    response.set_cookie(
        settings.AUTH_REFRESH_COOKIE_NAME,
        str(refresh),
        max_age=int(refresh.lifetime.total_seconds()),
        path=settings.AUTH_REFRESH_COOKIE_PATH,
        secure=settings.AUTH_REFRESH_COOKIE_SECURE,
        httponly=True,
        samesite=settings.AUTH_REFRESH_COOKIE_SAMESITE,
    )


def clear_refresh_cookie(response: HttpResponse) -> None:
    response.delete_cookie(
        settings.AUTH_REFRESH_COOKIE_NAME,
        path=settings.AUTH_REFRESH_COOKIE_PATH,
        samesite=settings.AUTH_REFRESH_COOKIE_SAMESITE,
    )


def require_trusted_origin(request) -> None:
    """CSRF defence for the endpoints that act on the refresh *cookie*.

    In production the cookie is SameSite=None (the frontend is a different
    site), so the browser will attach it to cross-site requests — including a
    malicious page's. Every such request carries an Origin header the page
    cannot forge, so anything not from our own frontend is refused.
    """
    origin = request.headers.get("Origin")
    if origin not in settings.CORS_ALLOWED_ORIGINS:
        raise PermissionDenied("Untrusted origin.")
