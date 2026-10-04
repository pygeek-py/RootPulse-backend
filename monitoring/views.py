from django.conf import settings
from django.db.models import Q
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import mixins, status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from accounts.services import audit

from .models import Monitor
from .serializers import MonitorSerializer
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
        qs = Monitor.objects.filter(user=self.request.user)
        params = self.request.query_params

        if value := params.get("status"):
            qs = qs.filter(status=value)
        if value := params.get("type"):
            qs = qs.filter(type=value)
        if value := params.get("q", "").strip():
            qs = qs.filter(Q(name__icontains=value) | Q(target__icontains=value))
        return qs.order_by(SORTS.get(params.get("sort", ""), "-created_at"), "id")

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
            monitor.status = Monitor.Status.PAUSED
            monitor.next_check_at = None  # never due while paused
            monitor.save(update_fields=["status", "next_check_at", "updated_at"])
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
