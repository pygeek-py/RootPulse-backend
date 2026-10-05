from __future__ import annotations

import logging

from django.conf import settings
from django.db.models import Count
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import mixins, status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework.viewsets import GenericViewSet, ModelViewSet

from accounts.services import audit

from . import parsers, services
from .models import Deploy, DeploySource
from .serializers import (
    GENERATED,
    DeploySerializer,
    DeploySourceSerializer,
    WebhookResultSerializer,
)

logger = logging.getLogger(__name__)
UUID_REGEX = "[0-9a-fA-F-]{36}"


@extend_schema_view(
    list=extend_schema(tags=["deploys"]),
    create=extend_schema(tags=["deploys"]),
    retrieve=extend_schema(tags=["deploys"]),
    partial_update=extend_schema(tags=["deploys"]),
    destroy=extend_schema(tags=["deploys"]),
)
class DeploySourceViewSet(ModelViewSet):
    """Where deploys come from. Private to the signed-in user; the signing secret is shown once."""

    serializer_class = DeploySourceSerializer
    lookup_value_regex = UUID_REGEX
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return DeploySource.objects.none()
        return (
            DeploySource.objects.filter(user=self.request.user)
            .annotate(deploy_count=Count("deploys"))
            .order_by("created_at")
        )

    def perform_create(self, serializer):
        user = self.request.user
        if DeploySource.objects.filter(user=user).count() >= settings.MAX_DEPLOY_SOURCES_PER_USER:
            raise PermissionDenied(
                f"You've reached the limit of {settings.MAX_DEPLOY_SOURCES_PER_USER} connections."
            )
        source = serializer.save(user=user)
        source.reveal_secret = source.type in GENERATED  # the one time a generated secret shows
        audit(
            "deploy_source_created", self.request, user, source_id=str(source.id), type=source.type
        )

    def perform_destroy(self, instance):
        audit("deploy_source_deleted", self.request, self.request.user, source_id=str(instance.id))
        instance.delete()  # deploys it reported stay in the history

    @extend_schema(tags=["deploys"], request=None, responses={200: DeploySourceSerializer})
    @action(detail=True, methods=["post"], url_path="rotate-secret")
    def rotate_secret(self, request, pk=None):
        import secrets

        source = self.get_object()
        if source.type not in GENERATED:
            raise ValidationError(
                f"{source.type.title()} issues its own secret. Paste the new one in instead."
            )
        source.secret = secrets.token_urlsafe(32)
        source.save(update_fields=["secret"])
        source.reveal_secret = True
        audit("deploy_secret_rotated", request, request.user, source_id=str(source.id))
        return Response(self.get_serializer(source).data)


class DeployPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100


@extend_schema_view(
    list=extend_schema(
        tags=["deploys"],
        parameters=[OpenApiParameter("service", description="Only deploys of this service.")],
    )
)
class DeployViewSet(mixins.ListModelMixin, GenericViewSet):
    """Deploys that have been reported, newest first."""

    serializer_class = DeploySerializer
    pagination_class = DeployPagination
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Deploy.objects.none()
        qs = Deploy.objects.filter(user=self.request.user).order_by("-occurred_at", "-received_at")
        if service := self.request.query_params.get("service", "").strip():
            qs = qs.filter(service_name__iexact=service)
        return qs


class DeployWebhookView(APIView):
    """`POST /api/v1/deploys/webhook/{source}/{token}/`: a provider telling us something deployed.

    There is no signed-in user: the token in the URL says whose connection this is, and the
    provider's own signature over the body says the request is genuine. Both have to hold. An
    unknown or switched-off connection is a plain 404 (and never says which part was wrong); a
    bad signature is a 401.
    """

    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "deploy_webhook"

    @extend_schema(
        tags=["deploys"],
        request=None,
        responses={200: WebhookResultSerializer, 202: WebhookResultSerializer},
    )
    def post(self, request, source, token):
        connection = (
            DeploySource.objects.filter(token=token, type=source, enabled=True)
            .select_related("user")
            .first()
        )
        if connection is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        declared = request.META.get("CONTENT_LENGTH")
        if declared and declared.isdigit() and int(declared) > settings.DEPLOY_MAX_BODY_BYTES:
            return Response(
                {"detail": "Too large."}, status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
            )
        body = request.body
        if len(body) > settings.DEPLOY_MAX_BODY_BYTES:
            return Response(
                {"detail": "Too large."}, status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
            )

        try:
            parsed = parsers.verify_and_parse(connection, request.headers, body)
        except parsers.Rejected as exc:
            return Response({"detail": exc.message}, status=exc.status)
        except parsers.Ignored as exc:
            return Response(
                {"status": "ignored", "reason": exc.reason}, status=status.HTTP_202_ACCEPTED
            )

        outcome, deploy, reason = services.ingest(connection, parsed, body)
        if outcome == services.Outcome.IGNORED:
            return Response(
                {"status": "ignored", "reason": reason}, status=status.HTTP_202_ACCEPTED
            )
        return Response({"status": outcome, "deploy_id": deploy.id if deploy else None})
