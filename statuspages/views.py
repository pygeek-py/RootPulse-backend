"""The owner's side: build the page. Everything is scoped to the signed-in user."""

from __future__ import annotations

from django.db import IntegrityError, transaction
from django.db.models import Count, Max, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.viewsets import ModelViewSet

from accounts.services import audit

from . import services
from .models import (
    StatusPage,
    StatusPageAnnouncement,
    StatusPageComponent,
    StatusPageSubscriber,
)
from .serializers import (
    AnnouncementSerializer,
    PageComponentSerializer,
    PublicStatusPageSerializer,
    ReorderSerializer,
    StatusPageSerializer,
    SubscriberSerializer,
)

MAX_PAGES_PER_USER = 1  # a product limit (PRD 6.4), enforced here rather than in the schema
UUID = "[0-9a-fA-F-]{36}"


class SubscriberPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 200


@extend_schema_view(
    list=extend_schema(tags=["status-pages"]),
    create=extend_schema(tags=["status-pages"]),
    retrieve=extend_schema(tags=["status-pages"]),
    partial_update=extend_schema(tags=["status-pages"]),
    destroy=extend_schema(tags=["status-pages"]),
)
class StatusPageViewSet(ModelViewSet):
    serializer_class = StatusPageSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = None  # at most one
    lookup_value_regex = UUID
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return StatusPage.objects.none()
        return (
            StatusPage.objects.filter(user=self.request.user)
            .annotate(
                component_total=Count("components", distinct=True),
                subscriber_total=Count(
                    "subscribers", filter=Q(subscribers__confirmed_at__isnull=False), distinct=True
                ),
            )
            .order_by("created_at")
        )

    def perform_create(self, serializer):
        if StatusPage.objects.filter(user=self.request.user).count() >= MAX_PAGES_PER_USER:
            raise ValidationError("You can have one status page for now.")
        try:
            with transaction.atomic():
                page = serializer.save()
        except IntegrityError:  # the same slug claimed at the same moment
            raise ValidationError({"slug": "That address is taken."}) from None
        if page.has_password:
            audit("status_page_password_set", self.request, self.request.user, page=page.slug)

    def perform_update(self, serializer):
        had_password = serializer.instance.has_password
        old_hash = serializer.instance.password_hash
        try:
            with transaction.atomic():
                page = serializer.save()
        except IntegrityError:
            raise ValidationError({"slug": "That address is taken."}) from None
        if page.password_hash != old_hash:
            audit(
                "status_page_password_set" if page.has_password else "status_page_password_cleared",
                self.request,
                self.request.user,
                page=page.slug,
                had_password=had_password,
            )

    def perform_destroy(self, instance):
        audit("status_page_deleted", self.request, self.request.user, page=instance.slug)
        instance.delete()

    def _page(self) -> StatusPage:
        return get_object_or_404(
            StatusPage.objects.filter(user=self.request.user), pk=self.kwargs["pk"]
        )

    @extend_schema(tags=["status-pages"], responses={200: PublicStatusPageSerializer})
    @action(detail=True, methods=["get"])
    def preview(self, request, pk=None):
        """The page as visitors will see it, whether or not it is published or has a password."""
        return Response(services.build_public(self._page()))

    # --- components -------------------------------------------------------------------

    @extend_schema(
        tags=["status-pages"],
        methods=["GET"],
        responses={200: PageComponentSerializer(many=True)},
    )
    @extend_schema(
        tags=["status-pages"],
        methods=["POST"],
        request=PageComponentSerializer,
        responses={201: PageComponentSerializer},
    )
    @action(detail=True, methods=["get", "post"], url_path="components", pagination_class=None)
    def components(self, request, pk=None):
        page = self._page()
        if request.method == "GET":
            rows = page.components.select_related("monitor")
            return Response(
                PageComponentSerializer(rows, many=True, context={"request": request}).data
            )

        serializer = PageComponentSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        monitor = serializer.validated_data["monitor"]
        if page.components.count() >= services.MAX_COMPONENTS:
            raise ValidationError(f"A page can show up to {services.MAX_COMPONENTS} components.")
        if page.components.filter(monitor=monitor).exists():
            raise ValidationError({"monitor_id": "That monitor is already on this page."})
        last = page.components.aggregate(last=Max("order"))["last"]
        component = StatusPageComponent.objects.create(
            page=page,
            monitor=monitor,
            display_name=serializer.validated_data.get("display_name") or monitor.name[:80],
            group=serializer.validated_data.get("group", ""),
            order=0 if last is None else last + 1,
        )
        return Response(
            PageComponentSerializer(component, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(
        tags=["status-pages"],
        request=ReorderSerializer,
        responses={200: PageComponentSerializer(many=True)},
    )
    @action(detail=True, methods=["post"], url_path="components/reorder")
    def reorder_components(self, request, pk=None):
        """Put the components in the order given. Every component has to be listed once."""
        page = self._page()
        serializer = ReorderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        ids = serializer.validated_data["ids"]
        current = {c.id: c for c in page.components.all()}
        if len(ids) != len(set(ids)) or set(ids) != set(current):
            raise ValidationError({"ids": "List every component on the page exactly once."})
        for position, component_id in enumerate(ids):
            current[component_id].order = position
        StatusPageComponent.objects.bulk_update(list(current.values()), ["order"])
        rows = page.components.select_related("monitor")
        return Response(PageComponentSerializer(rows, many=True, context={"request": request}).data)

    @extend_schema(
        tags=["status-pages"],
        methods=["PATCH"],
        request=PageComponentSerializer,
        responses={200: PageComponentSerializer},
    )
    @extend_schema(tags=["status-pages"], methods=["DELETE"], responses={204: None})
    @action(
        detail=True,
        methods=["patch", "delete"],
        url_path=rf"components/(?P<component_id>{UUID})",
    )
    def component(self, request, pk=None, component_id=None):
        component = get_object_or_404(
            StatusPageComponent.objects.select_related("monitor"),
            pk=component_id,
            page=self._page(),
        )
        if request.method == "DELETE":
            component.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        serializer = PageComponentSerializer(
            component, data=request.data, partial=True, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    # --- announcements ----------------------------------------------------------------

    @extend_schema(
        tags=["status-pages"], methods=["GET"], responses={200: AnnouncementSerializer(many=True)}
    )
    @extend_schema(
        tags=["status-pages"],
        methods=["POST"],
        request=AnnouncementSerializer,
        responses={201: AnnouncementSerializer},
    )
    @action(detail=True, methods=["get", "post"], url_path="announcements", pagination_class=None)
    def announcements(self, request, pk=None):
        page = self._page()
        if request.method == "GET":
            return Response(AnnouncementSerializer(page.announcements.all()[:200], many=True).data)

        if page.announcements.count() >= 200:
            raise ValidationError("This page has too many announcements. Delete old ones first.")
        serializer = AnnouncementSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        notify = serializer.validated_data.pop("notify", True)
        announcement = serializer.save(page=page)
        if notify:
            services.announce(announcement)
        return Response(AnnouncementSerializer(announcement).data, status=status.HTTP_201_CREATED)

    @extend_schema(
        tags=["status-pages"],
        methods=["PATCH"],
        request=AnnouncementSerializer,
        responses={200: AnnouncementSerializer},
    )
    @extend_schema(tags=["status-pages"], methods=["DELETE"], responses={204: None})
    @action(
        detail=True,
        methods=["patch", "delete"],
        url_path=rf"announcements/(?P<announcement_id>{UUID})",
    )
    def announcement(self, request, pk=None, announcement_id=None):
        announcement = get_object_or_404(
            StatusPageAnnouncement, pk=announcement_id, page=self._page()
        )
        if request.method == "DELETE":
            announcement.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        serializer = AnnouncementSerializer(announcement, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    @extend_schema(tags=["status-pages"], request=None, responses={200: AnnouncementSerializer})
    @action(
        detail=True,
        methods=["post"],
        url_path=rf"announcements/(?P<announcement_id>{UUID})/resolve",
    )
    def resolve_announcement(self, request, pk=None, announcement_id=None):
        """Mark an incident resolved, or end a maintenance early. Subscribers who were told it
        began are told it's over."""
        announcement = get_object_or_404(
            StatusPageAnnouncement.objects.select_related("page"),
            pk=announcement_id,
            page=self._page(),
        )
        if announcement.resolved_at is None:
            announcement.resolved_at = timezone.now()
            announcement.save(update_fields=["resolved_at", "updated_at"])
            services.announcement_resolved(announcement)
        return Response(AnnouncementSerializer(announcement).data)

    # --- subscribers ------------------------------------------------------------------

    @extend_schema(tags=["status-pages"], responses={200: SubscriberSerializer(many=True)})
    @action(
        detail=True,
        methods=["get"],
        url_path="subscribers",
        pagination_class=SubscriberPagination,
    )
    def subscribers(self, request, pk=None):
        rows = self._page().subscribers.all()
        paginator = SubscriberPagination()
        paged = paginator.paginate_queryset(rows, request, view=self)
        return paginator.get_paginated_response(SubscriberSerializer(paged, many=True).data)

    @extend_schema(tags=["status-pages"], responses={204: None})
    @action(
        detail=True,
        methods=["delete"],
        url_path=rf"subscribers/(?P<subscriber_id>{UUID})",
    )
    def subscriber(self, request, pk=None, subscriber_id=None):
        get_object_or_404(StatusPageSubscriber, pk=subscriber_id, page=self._page()).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
