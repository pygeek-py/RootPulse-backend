from __future__ import annotations

import uuid

from django.conf import settings
from django.db import transaction
from django.db.models import Count
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import mixins, status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework.viewsets import GenericViewSet, ModelViewSet

from accounts.services import audit
from monitoring.models import AlertContact, Monitor, MonitorAlertContact

from . import dispatcher, services, verification
from .channels import CHANNELS, ConfigError, discover_telegram_chats
from .http import DeliveryError
from .models import NotificationDelivery
from .serializers import (
    AlertContactSerializer,
    ContactVerifySerializer,
    DeliverySerializer,
    TelegramDiscoverSerializer,
    TestResultSerializer,
)

UUID_REGEX = "[0-9a-fA-F-]{36}"


class DeliveryPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100


@extend_schema_view(
    list=extend_schema(tags=["alert-contacts"]),
    create=extend_schema(tags=["alert-contacts"]),
    retrieve=extend_schema(tags=["alert-contacts"]),
    partial_update=extend_schema(tags=["alert-contacts"]),
    destroy=extend_schema(tags=["alert-contacts"]),
)
class AlertContactViewSet(ModelViewSet):
    """Where alerts go. Private to the signed-in user; secrets are never sent back
    (a webhook's signing secret appears once, in the response that creates or rotates it)."""

    serializer_class = AlertContactSerializer
    lookup_value_regex = UUID_REGEX
    # Actions below opt in to a per-endpoint rate limit; the rest of the viewset has none.
    throttle_scope: str | None = None
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return AlertContact.objects.none()
        return (
            AlertContact.objects.filter(user=self.request.user)
            .annotate(monitor_count=Count("monitors", distinct=True))
            .order_by("created_at")
        )

    # -- create / update -----------------------------------------------------------------------

    def perform_create(self, serializer):
        user = self.request.user
        if AlertContact.objects.filter(user=user).count() >= settings.MAX_ALERT_CONTACTS_PER_USER:
            raise PermissionDenied(
                f"You've reached the limit of {settings.MAX_ALERT_CONTACTS_PER_USER} contacts."
            )
        attach = serializer.validated_data.pop("attach_to_existing_monitors", False)
        with transaction.atomic():
            contact = serializer.save(user=user)
            if verification.needs_verification(contact, user.email):
                verification.start(contact)
            else:
                verification.mark_trusted(contact)
                contact.save(
                    update_fields=[
                        "verified_at",
                        "verification_hash",
                        "verification_expires_at",
                        "verification_attempts",
                    ]
                )
            if attach:
                MonitorAlertContact.objects.bulk_create(
                    [
                        MonitorAlertContact(monitor=m, alert_contact=contact)
                        for m in Monitor.objects.filter(user=user)
                    ],
                    ignore_conflicts=True,
                )
        contact.reveal_secret = True  # the one time the signing secret is shown
        audit(
            "alert_contact_created",
            self.request,
            user,
            contact_id=str(contact.id),
            channel=contact.channel,
        )

    def perform_update(self, serializer):
        before = serializer.instance.config.get("address")
        contact = serializer.save()
        # Pointing an email contact at a new address means proving that one.
        if (
            contact.channel == AlertContact.Channel.EMAIL
            and contact.config.get("address") != before
        ):
            if verification.needs_verification(contact, self.request.user.email):
                verification.start(contact)
            else:
                verification.mark_trusted(contact)
                contact.save(
                    update_fields=[
                        "verified_at",
                        "verification_hash",
                        "verification_expires_at",
                        "verification_attempts",
                    ]
                )

    def perform_destroy(self, instance):
        audit(
            "alert_contact_deleted",
            self.request,
            self.request.user,
            contact_id=str(instance.id),
            channel=instance.channel,
        )
        instance.delete()  # detaches from monitors; past deliveries keep the name

    # -- actions --------------------------------------------------------------------------------

    @extend_schema(tags=["alert-contacts"], request=None, responses={200: TestResultSerializer})
    @action(
        detail=True,
        methods=["post"],
        throttle_classes=[ScopedRateThrottle],
        throttle_scope="notify_test",
    )
    def test(self, request, pk=None):
        """Send a real test alert right now and report exactly what happened."""
        contact = self.get_object()
        if not contact.enabled:
            raise ValidationError("Switch this contact on first.")
        if not services.deliverable(contact):
            raise ValidationError("Confirm this email address first.")
        delivery = services.create_test(contact)
        status_, error = send_now(delivery)
        return Response({"status": status_, "error": error})

    @extend_schema(
        tags=["alert-contacts"],
        request=ContactVerifySerializer,
        responses={200: AlertContactSerializer},
    )
    @action(
        detail=True,
        methods=["post"],
        throttle_classes=[ScopedRateThrottle],
        throttle_scope="contact_verify",
    )
    def verify(self, request, pk=None):
        contact = self.get_object()
        serializer = ContactVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if contact.channel != AlertContact.Channel.EMAIL:
            raise ValidationError("Only email addresses need confirming.")
        if contact.verified_at is None and not verification.check(
            contact, serializer.validated_data["code"]
        ):
            raise ValidationError("That code is wrong or has expired. Ask for a new one.")
        return Response(self.get_serializer(self.get_object()).data)

    @extend_schema(tags=["alert-contacts"], request=None, responses={204: None})
    @action(
        detail=True,
        methods=["post"],
        url_path="resend-verification",
        throttle_classes=[ScopedRateThrottle],
        throttle_scope="contact_verify",
    )
    def resend_verification(self, request, pk=None):
        contact = self.get_object()
        if contact.channel != AlertContact.Channel.EMAIL or contact.verified_at is not None:
            raise ValidationError("This contact doesn't need confirming.")
        if not verification.start(contact):
            raise ValidationError("Couldn't send the email. Try again in a moment.")
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(tags=["alert-contacts"], request=None, responses={200: AlertContactSerializer})
    @action(detail=True, methods=["post"], url_path="rotate-secret")
    def rotate_secret(self, request, pk=None):
        contact = self.get_object()
        if contact.channel != AlertContact.Channel.WEBHOOK:
            raise ValidationError("Only webhooks have a signing secret.")
        # Drop the old secret so clean() generates a new one.
        config = {k: v for k, v in contact.config.items() if k != "secret"}
        contact.config = CHANNELS["webhook"].clean(config)
        contact.save(update_fields=["config"])
        contact.reveal_secret = True
        audit("webhook_secret_rotated", request, request.user, contact_id=str(contact.id))
        return Response(self.get_serializer(contact).data)

    @extend_schema(
        tags=["alert-contacts"],
        request=TelegramDiscoverSerializer,
        responses={
            200: {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "type": {"type": "string"},
                        "title": {"type": "string"},
                    },
                },
            }
        },
    )
    @action(
        detail=False,
        methods=["post"],
        url_path="telegram-chats",
        throttle_classes=[ScopedRateThrottle],
        throttle_scope="notify_test",
    )
    def telegram_chats(self, request):
        """Chats that have messaged the user's bot, so they can pick theirs instead of
        hunting for a numeric id. (They message the bot first; Telegram requires it.)"""
        serializer = TelegramDiscoverSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            return Response(discover_telegram_chats(serializer.validated_data["bot_token"].strip()))
        except ConfigError as exc:
            raise ValidationError({f"config.{k}": v for k, v in exc.errors.items()}) from exc
        except DeliveryError as exc:
            raise ValidationError(exc.message) from exc


def send_now(delivery: NotificationDelivery) -> tuple[str, str]:
    """Deliver one test alert synchronously, counting it as a single attempt."""
    delivery.attempt_count = 1
    delivery.save(update_fields=["attempt_count"])
    status_ = dispatcher.deliver(delivery.id)
    delivery.refresh_from_db()
    if status_ == NotificationDelivery.Status.PENDING:
        # A retryable failure: for a test, report it instead of leaving it queued.
        delivery.status = NotificationDelivery.Status.FAILED
        delivery.next_attempt_at = None
        delivery.save(update_fields=["status", "next_attempt_at"])
        status_ = delivery.status
    return status_, delivery.error


@extend_schema_view(
    list=extend_schema(
        tags=["notifications"],
        parameters=[
            OpenApiParameter("status", enum=[c for c, _ in NotificationDelivery.Status.choices]),
            OpenApiParameter("monitor_id", type=uuid.UUID),
            OpenApiParameter("contact_id", type=uuid.UUID),
            OpenApiParameter("incident_id", type=uuid.UUID),
        ],
    )
)
class DeliveryViewSet(mixins.ListModelMixin, GenericViewSet):
    """History of the alerts RootPulse tried to send, newest first."""

    serializer_class = DeliverySerializer
    pagination_class = DeliveryPagination
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return NotificationDelivery.objects.none()
        user = self.request.user
        qs = (
            NotificationDelivery.objects.filter(contact__user=user)
            .select_related("monitor", "provider_incident__provider")
            .order_by("-created_at", "-id")
        )
        params = self.request.query_params
        if value := params.get("status"):
            if value not in NotificationDelivery.Status.values:
                raise ValidationError({"status": "Not a valid status."})
            qs = qs.filter(status=value)
        for param, field in (
            ("monitor_id", "monitor_id"),
            ("contact_id", "contact_id"),
            ("incident_id", "incident_id"),
        ):
            if value := params.get(param):
                try:
                    qs = qs.filter(**{field: uuid.UUID(value)})
                except ValueError as exc:
                    raise ValidationError({param: "Not a valid id."}) from exc
        return qs


class VapidKeyView(APIView):
    """The public half of the server's push key, which a browser needs to subscribe."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["notifications"],
        responses={
            200: {
                "type": "object",
                "properties": {"public_key": {"type": "string", "nullable": True}},
            }
        },
    )
    def get(self, request):
        configured = bool(settings.VAPID_PUBLIC_KEY and settings.VAPID_PRIVATE_KEY)
        return Response({"public_key": settings.VAPID_PUBLIC_KEY if configured else None})
