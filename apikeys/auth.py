"""API-key sign-in, as an alternative to the dashboard's JWT on every endpoint.

A key travels as `Authorization: Bearer rp_...`, so a script is one header away from the
dashboard's own calls. Everything after authentication is identical: the same views, the same
per-user filtering, the same rate limits (a key is the user, for throttling).

The read/full rule lives *here*, in authentication, rather than as a permission class: views set
their own `permission_classes`, and a rule that depended on every view remembering to include
one would fail open the first time somebody forgot.
"""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from drf_spectacular.extensions import OpenApiAuthenticationExtension
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.exceptions import AuthenticationFailed, PermissionDenied, Throttled
from rest_framework.permissions import SAFE_METHODS
from rest_framework.throttling import BaseThrottle

from .models import KEY_PREFIX, ApiKey, hash_key

# `last_used_at` is for a person looking at their keys, not for accounting: writing it on every
# request would put a database write on the hot path of every call.
LAST_USED_RESOLUTION = timedelta(minutes=1)


FAILURE_WINDOW = 60  # seconds


def _failures_key(request) -> str:
    return f"apikey-failures:{BaseThrottle().get_ident(request)}"


def _refuse_if_guessing(request) -> None:
    """Wrong keys are refused before the view's own throttles ever run, so they need a limit of
    their own: otherwise anyone could make the server look up keys as fast as they like. The count
    is per address and only wrong keys add to it, so a script with a good key never meets it."""
    if cache.get(_failures_key(request), 0) >= settings.API_KEY_FAILURES_PER_MINUTE:
        raise Throttled(wait=FAILURE_WINDOW)


def _note_failure(request) -> None:
    key = _failures_key(request)
    if cache.add(key, 1, FAILURE_WINDOW) is False:
        try:
            cache.incr(key)
        except ValueError:  # expired between the two calls
            cache.add(key, 1, FAILURE_WINDOW)


class ApiKeyAuthentication(BaseAuthentication):
    keyword = b"bearer"

    def authenticate_header(self, request):
        return "Bearer"

    def authenticate(self, request):
        parts = get_authorization_header(request).split()
        if len(parts) != 2 or parts[0].lower() != self.keyword:
            return None
        try:
            raw = parts[1].decode()
        except UnicodeError:
            return None
        if not raw.startswith(KEY_PREFIX):
            return None  # not ours: a JWT, for the next authenticator

        _refuse_if_guessing(request)
        key = ApiKey.objects.select_related("user").filter(key_hash=hash_key(raw)).first()
        # One message for every way a key can be wrong, so it says nothing about which keys exist.
        if key is None or not key.active or not key.user.is_active:
            _note_failure(request)
            raise AuthenticationFailed("That API key isn't valid.")

        if key.scope == ApiKey.Scope.READ and request.method not in SAFE_METHODS:
            raise PermissionDenied("This API key is read-only.")

        now = timezone.now()
        if key.last_used_at is None or now - key.last_used_at > LAST_USED_RESOLUTION:
            ApiKey.objects.filter(pk=key.pk).update(last_used_at=now)
        return key.user, key


class ApiKeyAuthenticationScheme(OpenApiAuthenticationExtension):
    target_class = "apikeys.auth.ApiKeyAuthentication"
    name = "apiKey"

    def get_security_definition(self, auto_schema):
        return {
            "type": "http",
            "scheme": "bearer",
            "description": (
                "An API key made under Settings, API keys, sent as `Authorization: Bearer rp_...`. "
                "A `read` key can only make GET, HEAD and OPTIONS requests."
            ),
        }
