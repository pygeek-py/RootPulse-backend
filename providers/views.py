from __future__ import annotations

from django.db.models import Count, Prefetch, Q
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import mixins, status
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from accounts.services import audit
from monitoring.models import AlertContact

from . import ingest
from .models import Provider, ProviderIncident, UserProviderSubscription
from .serializers import (
    ProviderDetailSerializer,
    ProviderIncidentSerializer,
    ProviderSerializer,
    SubscribeSerializer,
)


def with_my_subscription(qs, user):
    """Annotate providers with how many incidents are ongoing and attach the user's own
    subscription, in two queries however many providers there are."""
    return qs.annotate(
        ongoing_incidents=Count(
            "incidents", filter=Q(incidents__ended_at__isnull=True), distinct=True
        )
    ).prefetch_related(
        Prefetch(
            "subscriptions",
            queryset=UserProviderSubscription.objects.filter(user=user).prefetch_related(
                "alert_contacts"
            ),
            to_attr="my_subscriptions",
        )
    )


@extend_schema_view(
    list=extend_schema(
        tags=["providers"],
        parameters=[
            OpenApiParameter("q", description="Search by name, category or what it does."),
            OpenApiParameter("category"),
            OpenApiParameter("subscribed", type=bool, description="Only the ones you track."),
        ],
    ),
    retrieve=extend_schema(tags=["providers"], responses={200: ProviderDetailSerializer}),
)
class ProviderViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, GenericViewSet):
    """The curated list of third-party providers, and which ones you track."""

    serializer_class = ProviderSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = None  # about twenty, always shown together
    lookup_field = "slug"
    lookup_value_regex = "[a-z0-9-]+"

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Provider.objects.none()
        qs = with_my_subscription(Provider.objects.filter(enabled=True), self.request.user)
        params = self.request.query_params
        if text := params.get("q", "").strip():
            qs = qs.filter(
                Q(name__icontains=text)
                | Q(category__icontains=text)
                | Q(description__icontains=text)
            )
        if category := params.get("category", "").strip():
            qs = qs.filter(category__iexact=category)
        if params.get("subscribed") == "true":
            qs = qs.filter(subscriptions__user=self.request.user)
        return qs.order_by("name")

    def get_serializer_class(self):
        return ProviderDetailSerializer if self.action == "retrieve" else ProviderSerializer

    def _fresh(self, provider: Provider) -> Provider:
        return self.get_queryset().get(pk=provider.pk)

    @extend_schema(
        tags=["providers"],
        request=SubscribeSerializer,
        responses={200: ProviderSerializer},
        methods=["POST"],
    )
    @extend_schema(tags=["providers"], request=None, responses={204: None}, methods=["DELETE"])
    @action(detail=True, methods=["post", "delete"])
    def subscribe(self, request, slug=None):
        """Track a provider (POST: also to change which contacts it alerts), or stop (DELETE)."""
        provider = self.get_object()
        user = request.user
        existing = UserProviderSubscription.objects.filter(user=user, provider=provider).first()

        if request.method == "DELETE":
            if existing is not None:
                ingest.cancel_pending(existing)
                existing.delete()
                audit("provider_unsubscribed", request, user, provider=provider.slug)
            return Response(status=status.HTTP_204_NO_CONTENT)

        serializer = SubscribeSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        created = existing is None
        sub = existing or UserProviderSubscription(user=user, provider=provider)
        if "min_impact" in data:
            sub.min_impact = data["min_impact"]
        sub.save()
        if "alert_contact_ids" in data:
            sub.alert_contacts.set(data["alert_contact_ids"])
        elif created:
            # Alerts on by default: tracking something nobody hears about isn't tracking.
            sub.alert_contacts.set(AlertContact.objects.filter(user=user))
        if created:
            audit("provider_subscribed", request, user, provider=provider.slug)
        # Anything already going wrong is worth knowing right away.
        sub = UserProviderSubscription.objects.prefetch_related("alert_contacts").get(pk=sub.pk)
        ingest.reconcile(provider, only=sub)
        return Response(
            ProviderSerializer(self._fresh(provider), context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class IncidentPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100


@extend_schema_view(
    list=extend_schema(
        tags=["providers"],
        parameters=[
            OpenApiParameter("status", enum=["ongoing", "resolved"]),
            OpenApiParameter("provider", description="Only this provider (its slug)."),
            OpenApiParameter(
                "scope",
                enum=["subscribed", "all"],
                description="Providers you track (default), or every provider.",
            ),
        ],
    )
)
class ProviderIncidentViewSet(mixins.ListModelMixin, GenericViewSet):
    """Incidents the providers have reported, newest first."""

    serializer_class = ProviderIncidentSerializer
    pagination_class = IncidentPagination
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return ProviderIncident.objects.none()
        params = self.request.query_params
        qs = ProviderIncident.objects.select_related("provider").filter(provider__enabled=True)
        scope = params.get("scope", "subscribed")
        if scope not in ("subscribed", "all"):
            raise ValidationError({"scope": "Use subscribed or all."})
        if scope == "subscribed":
            qs = qs.filter(provider__subscriptions__user=self.request.user)
        match params.get("status"):
            case "ongoing":
                qs = qs.filter(ended_at__isnull=True)
            case "resolved":
                qs = qs.filter(ended_at__isnull=False)
            case None | "":
                pass
            case _:
                raise ValidationError({"status": "Use ongoing or resolved."})
        if slug := params.get("provider", "").strip():
            qs = qs.filter(provider__slug=slug)
        return qs.order_by("-started_at", "id")
