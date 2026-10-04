"""HTTP side of the one-click connections (logic lives in integrations.py)."""

from __future__ import annotations

import hmac
import logging

from django.conf import settings
from django.http import HttpResponseRedirect
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from accounts.services import audit

from . import integrations
from .channels import DeliveryError
from .models import IntegrationLink
from .serializers import AlertContactSerializer

logger = logging.getLogger(__name__)


class _Availability(serializers.Serializer):
    available = serializers.BooleanField()


class _TelegramAvailability(_Availability):
    bot_username = serializers.CharField(allow_null=True)


class IntegrationsSerializer(serializers.Serializer):
    telegram = _TelegramAvailability()
    discord = _Availability()
    slack = _Availability()


class TelegramLinkStartSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    url = serializers.CharField(help_text="Opens the bot in a private chat.")
    group_url = serializers.CharField(help_text="Adds the bot to a group instead.")
    expires_in = serializers.IntegerField()


class TelegramLinkStatusSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=["pending", "connected", "expired"])
    contact = AlertContactSerializer(allow_null=True)


class OAuthStartSerializer(serializers.Serializer):
    authorize_url = serializers.CharField()


class IntegrationsView(APIView):
    """Which one-click connections this server can offer."""

    permission_classes = [IsAuthenticated]

    @extend_schema(tags=["integrations"], responses={200: IntegrationsSerializer})
    def get(self, request):
        return Response(integrations.availability())


class TelegramStartView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "integration_connect"

    @extend_schema(
        tags=["integrations"], request=None, responses={200: TelegramLinkStartSerializer}
    )
    def post(self, request):
        if not integrations.telegram_available():
            raise ValidationError("Telegram connect isn't set up on this server.")
        try:
            username = integrations.bot_username()
        except DeliveryError as exc:
            raise ValidationError("Couldn't reach Telegram. Try again in a moment.") from exc
        if not username:
            raise ValidationError("Telegram connect isn't set up on this server.")
        link, token = integrations.start_telegram_link(request.user)
        return Response(
            {
                "id": link.id,
                **integrations.telegram_urls(token),
                "expires_in": settings.INTEGRATION_LINK_TTL_SECONDS,
            }
        )


class TelegramLinkStatusView(APIView):
    """Polled by the page while the person is in Telegram. In development this also asks
    Telegram for waiting messages, so linking is near-instant rather than waiting for a loop."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "integration_poll"

    @extend_schema(tags=["integrations"], responses={200: TelegramLinkStatusSerializer})
    def get(self, request, pk):
        link = IntegrationLink.objects.filter(pk=pk, user=request.user).first()
        if link is None:
            raise NotFound()
        if link.consumed_at is None:
            integrations.poll_once()
            link.refresh_from_db()
        state = integrations.link_state(link)
        contact = None
        if state == "connected" and link.contact is not None:
            contact = link.contact
            contact.monitor_count = contact.monitors.count()
        return Response(
            {
                "status": state,
                "contact": AlertContactSerializer(contact).data if contact else None,
            }
        )


class OAuthStartView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "integration_connect"

    @extend_schema(tags=["integrations"], request=None, responses={200: OAuthStartSerializer})
    def post(self, request, provider):
        if not integrations.oauth_available(provider):
            raise ValidationError(f"{provider.title()} connect isn't set up on this server.")
        return Response({"authorize_url": integrations.authorize_url(provider, request.user)})


class OAuthCallbackView(APIView):
    """Where Discord/Slack send the browser back. There is no signed-in request here (it is a
    redirect from another site), so the signed `state` we issued says who started it."""

    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "integration_callback"

    @extend_schema(exclude=True)
    def get(self, request, provider):
        def back(error: str | None = None):
            return HttpResponseRedirect(integrations.frontend_result(provider, error))

        if not integrations.oauth_available(provider):
            return back("not_configured")
        user = integrations.read_state(request.query_params.get("state", ""), provider)
        if user is None:
            return back("state")
        code = request.query_params.get("code", "")
        if request.query_params.get("error") or not code:
            return back("denied")
        try:
            contact = integrations.finish_oauth(provider, user, code)
        except integrations.IntegrationError as exc:
            return back(exc.code)
        audit("alert_contact_created", request, user, contact_id=str(contact.id), channel=provider)
        return back()


class TelegramWebhookView(APIView):
    """Telegram pushes updates here in production. The shared secret (set when the webhook is
    registered) is the only gate, since Telegram can't sign in."""

    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_classes: list = []

    @extend_schema(exclude=True)
    def post(self, request):
        secret = settings.TELEGRAM_WEBHOOK_SECRET
        sent = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not secret or not hmac.compare_digest(secret, sent):
            return Response({"detail": "Unauthorized."}, status=status.HTTP_401_UNAUTHORIZED)
        try:
            integrations.handle_update(request.data if isinstance(request.data, dict) else {})
        except Exception:  # noqa: BLE001 - always answer 200 so Telegram doesn't retry forever
            logger.exception("telegram webhook update failed")
        return Response({"ok": True})
