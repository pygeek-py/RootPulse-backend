from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils.dateparse import parse_datetime
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import mixins, status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.pagination import CursorPagination, PageNumberPagination
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from accounts.services import audit
from incidents import services as incidents

from .models import Check, Monitor
from .serializers import (
    CheckPageSerializer,
    CheckSerializer,
    MonitorDetailSerializer,
    MonitorSerializer,
)
from .services import first_check_due

SORTS = {
    "name": "name",
    "-name": "-name",
    "created_at": "created_at",
    "-created_at": "-created_at",
    "status": "status",
    "-status": "-status",
    "type": "type",
}


class MonitorPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100


class CheckPagination(CursorPagination):
    """Append-heavy table: cursor paging stays fast however many checks pile up."""

    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 288  # one day of 5-minute checks
    ordering = ("-started_at", "-id")


@extend_schema_view(
    list=extend_schema(
        tags=["monitors"],
        parameters=[
            OpenApiParameter("status", enum=[c for c, _ in Monitor.Status.choices]),
            OpenApiParameter("type", enum=[c for c, _ in Monitor.Type.choices]),
            OpenApiParameter("q", description="Search name and target."),
            OpenApiParameter("sort", enum=list(SORTS), description="Default: -created_at."),
        ],
    ),
    create=extend_schema(tags=["monitors"]),
    retrieve=extend_schema(tags=["monitors"]),
    partial_update=extend_schema(tags=["monitors"]),
    destroy=extend_schema(tags=["monitors"]),
)
class MonitorViewSet(
    mixins.ListModelMixin,
    mixins.CreateModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    GenericViewSet,
):
    """A user's monitors. Everything is scoped to the signed-in user, so another
    user's monitor is simply a 404."""

    serializer_class = MonitorSerializer
    pagination_class = MonitorPagination
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]  # no PUT

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):  # schema generation has no user
            return Monitor.objects.none()
        qs = Monitor.objects.filter(user=self.request.user)
        params = self.request.query_params

        if value := params.get("status"):
            qs = qs.filter(status=value)
        if value := params.get("type"):
            qs = qs.filter(type=value)
        if value := params.get("q", "").strip():
            qs = qs.filter(Q(name__icontains=value) | Q(target__icontains=value))
        return qs.order_by(SORTS.get(params.get("sort", ""), "-created_at"), "id")

    def get_serializer_class(self):
        # Everything about a single monitor returns the same shape, including the 24h summary
        # (the frontend caches whatever pause, resume or an edit returns as the detail view's
        # data). Only the list leaves it out, since it would cost a query per row.
        return MonitorSerializer if self.action == "list" else MonitorDetailSerializer

    def perform_create(self, serializer):
        owned = Monitor.objects.filter(user=self.request.user).count()
        if owned >= settings.MAX_MONITORS_PER_USER:
            raise PermissionDenied(
                f"You've reached the limit of {settings.MAX_MONITORS_PER_USER} monitors. "
                "Delete one to add another."
            )
        monitor = serializer.save()
        audit("monitor_created", self.request, self.request.user, monitor_id=str(monitor.id))

    def perform_destroy(self, instance):
        audit(
            "monitor_deleted",
            self.request,
            self.request.user,
            monitor_id=str(instance.id),
            name=instance.name,
        )
        instance.delete()

    @extend_schema(tags=["monitors"], request=None, responses={200: MonitorSerializer})
    @action(detail=True, methods=["post"])
    def pause(self, request, pk=None):
        monitor = self.get_object()
        if monitor.status != Monitor.Status.PAUSED:
            with transaction.atomic():
                monitor.status = Monitor.Status.PAUSED
                monitor.next_check_at = None  # never due while paused
                monitor.save(update_fields=["status", "next_check_at", "updated_at"])
                incidents.close_for_pause(monitor)  # nothing can observe the outage any more
        return Response(self.get_serializer(monitor).data)

    @extend_schema(tags=["monitors"], request=None, responses={200: MonitorSerializer})
    @action(detail=True, methods=["post"])
    def resume(self, request, pk=None):
        monitor = self.get_object()
        if monitor.status == Monitor.Status.PAUSED:
            # Back to "unknown": the first check after resuming sets the real status.
            monitor.status = Monitor.Status.PENDING
            monitor.next_check_at = first_check_due(monitor)
            monitor.save(update_fields=["status", "next_check_at", "updated_at"])
        return Response(self.get_serializer(monitor).data, status=status.HTTP_200_OK)

    @extend_schema(
        tags=["monitors"],
        parameters=[
            OpenApiParameter("since", description="ISO 8601 timestamp: checks at or after it."),
            OpenApiParameter("until", description="ISO 8601 timestamp: checks before it."),
            OpenApiParameter("region", description="Only checks run from this region."),
            OpenApiParameter(
                "confirmation",
                type=bool,
                description="true: only re-checks; false: only scheduled checks.",
            ),
        ],
        responses={200: CheckPageSerializer},
    )
    @action(detail=True, methods=["get"])
    def checks(self, request, pk=None):
        monitor = self.get_object()
        params = request.query_params
        qs = Check.objects.filter(monitor=monitor)

        for name, lookup in (("since", "started_at__gte"), ("until", "started_at__lt")):
            if value := params.get(name):
                moment = parse_datetime(value)
                if moment is None:
                    raise ValidationError({name: "Use an ISO 8601 timestamp."})
                qs = qs.filter(**{lookup: moment})
        if value := params.get("region"):
            qs = qs.filter(region=value)
        if (value := params.get("confirmation")) in ("true", "false"):
            qs = qs.filter(confirmation=(value == "true"))

        paginator = CheckPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(CheckSerializer(page, many=True).data)
