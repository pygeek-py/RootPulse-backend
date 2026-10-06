"""Managing keys. Only a signed-in dashboard session may do it: a key can't make more keys, list
them or revoke them, so a leaked key can't be used to dig in."""

from __future__ import annotations

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.viewsets import ViewSet

from accounts.services import audit

from .models import ApiKey
from .serializers import ApiKeyCreatedSerializer, ApiKeySerializer

MAX_ACTIVE_KEYS = 10


class SessionOnly(BasePermission):
    message = "API keys can only be managed from a signed-in session."

    def has_permission(self, request, view):
        return not isinstance(request.auth, ApiKey)


@extend_schema_view(
    list=extend_schema(tags=["api-keys"], responses={200: ApiKeySerializer(many=True)}),
    create=extend_schema(
        tags=["api-keys"],
        request=ApiKeySerializer,
        responses={201: ApiKeyCreatedSerializer},
        description="The response carries the key itself, once. Store it: only a hash is kept.",
    ),
    destroy=extend_schema(
        tags=["api-keys"],
        responses={204: None},
        description=(
            "Revokes the key (it stops working at once). Deleting a key that is already revoked "
            "removes it from the list."
        ),
    ),
)
class ApiKeyViewSet(ViewSet):
    permission_classes = [IsAuthenticated, SessionOnly]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "api_key_manage"
    lookup_value_regex = "[0-9a-fA-F-]{36}"

    def list(self, request):
        keys = ApiKey.objects.filter(user=request.user)
        return Response(ApiKeySerializer(keys, many=True).data)

    def create(self, request):
        serializer = ApiKeySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            active = ApiKey.objects.select_for_update().filter(
                user=request.user, revoked_at__isnull=True
            )
            if len(list(active)) >= MAX_ACTIVE_KEYS:
                raise ValidationError(
                    f"You can have {MAX_ACTIVE_KEYS} active keys. Revoke one you no longer use."
                )
            key, raw = ApiKey.issue(
                request.user,
                serializer.validated_data["name"],
                serializer.validated_data.get("scope", ApiKey.Scope.READ),
            )
        audit("api_key_created", request, request.user, key_id=str(key.id), scope=key.scope)
        key.key = raw  # carried to the serializer for this response only; never stored
        return Response(ApiKeyCreatedSerializer(key).data, status=status.HTTP_201_CREATED)

    def destroy(self, request, pk=None):
        key = get_object_or_404(ApiKey.objects.filter(user=request.user), pk=pk)
        if key.revoked_at is None:
            key.revoked_at = timezone.now()
            key.save(update_fields=["revoked_at"])
            audit("api_key_revoked", request, request.user, key_id=str(key.id))
        else:
            key.delete()
            audit("api_key_deleted", request, request.user, key_id=str(pk))
        return Response(status=status.HTTP_204_NO_CONTENT)
