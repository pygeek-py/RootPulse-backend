from __future__ import annotations

import csv
import logging
import uuid
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import transaction
from django.db.models import Count, DurationField, ExpressionWrapper, F
from django.db.models.functions import Coalesce
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import mixins, status
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from accounts.services import audit

from . import postmortem, services
from .models import Incident, IncidentComment, IncidentEvent
from .serializers import (
    CommentCreateSerializer,
    CommentSerializer,
    IncidentDetailSerializer,
    IncidentSerializer,
    IncidentUpdateSerializer,
    PostmortemSerializer,
)

EXPORT_LIMIT = 10_000
SORTS = {
    "started_at": "started_at",
    "-started_at": "-started_at",
    "duration": "duration",
    "-duration": "-duration",
}

logger = logging.getLogger(__name__)


class IncidentPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100


def _day_bounds(raw: str, name: str, tz_name: str, end_of_day: bool) -> datetime:
    """A YYYY-MM-DD date as an aware datetime in the user's own time zone."""
    try:
        day = date.fromisoformat(raw)
    except ValueError as exc:
        raise ValidationError({name: "Use a date like 2026-06-01."}) from exc
    try:
        zone = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo("UTC")
    if end_of_day:  # inclusive: up to the start of the next day
        return datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
    return datetime.combine(day, time.min, tzinfo=zone)


def _formula_safe(value) -> str:
    """Stop a spreadsheet from running a cell as a formula (CSV injection)."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


@extend_schema_view(
    list=extend_schema(
        tags=["incidents"],
        parameters=[
            OpenApiParameter("status", enum=["ongoing", "resolved"]),
            OpenApiParameter("monitor_id", type=uuid.UUID),
            OpenApiParameter("start_date", description="First day to include (YYYY-MM-DD)."),
            OpenApiParameter("end_date", description="Last day to include (YYYY-MM-DD)."),
            OpenApiParameter("excluded", type=bool, description="Only (or none of) the excluded."),
            OpenApiParameter("sort", enum=list(SORTS), description="Default: -started_at."),
        ],
    ),
    retrieve=extend_schema(tags=["incidents"]),
    partial_update=extend_schema(tags=["incidents"], request=IncidentUpdateSerializer),
)
class IncidentViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    GenericViewSet,
):
    """The signed-in user's incidents. Incidents are created and closed by the monitoring
    engine, never by the API: a person can read them, annotate them and exclude them from
    reports, but can't fabricate or end one."""

    pagination_class = IncidentPagination
    # Only UUID-shaped ids reach the view; anything else is a plain 404 from the router.
    lookup_value_regex = "[0-9a-fA-F-]{36}"
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_serializer_class(self):
        return IncidentDetailSerializer if self.action == "retrieve" else IncidentSerializer

    def _scoped(self):
        if getattr(self, "swagger_fake_view", False):
            return Incident.objects.none()
        return Incident.objects.filter(monitor__user=self.request.user)

    def get_queryset(self):
        qs = (
            self._scoped()
            .select_related("monitor", "opened_by", "deploy")
            .annotate(comment_count=Count("comments", distinct=True))
        )
        params = self.request.query_params
        tz_name = self.request.user.timezone

        if (value := params.get("status")) == "ongoing":
            qs = qs.filter(ended_at__isnull=True)
        elif value == "resolved":
            qs = qs.filter(ended_at__isnull=False)
        elif value:
            raise ValidationError({"status": "Use ongoing or resolved."})

        if value := params.get("monitor_id"):
            try:
                qs = qs.filter(monitor_id=uuid.UUID(value))
            except ValueError as exc:
                raise ValidationError({"monitor_id": "Not a valid monitor id."}) from exc
        if value := params.get("start_date"):
            qs = qs.filter(started_at__gte=_day_bounds(value, "start_date", tz_name, False))
        if value := params.get("end_date"):
            qs = qs.filter(started_at__lt=_day_bounds(value, "end_date", tz_name, True))
        if (value := params.get("excluded")) in ("true", "false"):
            qs = qs.filter(excluded_from_reports=(value == "true"))

        sort = SORTS.get(params.get("sort", ""), "-started_at")
        if sort.lstrip("-") == "duration":
            qs = qs.annotate(
                duration=ExpressionWrapper(
                    Coalesce("ended_at", timezone.now()) - F("started_at"),
                    output_field=DurationField(),
                )
            )
        return qs.order_by(sort, "id")

    def get_object(self):
        # Detail routes ignore the list's filters; ownership is the only constraint.
        incident = get_object_or_404(
            self._scoped()
            .select_related("monitor", "opened_by", "deploy")
            .prefetch_related("events"),
            pk=self.kwargs["pk"],
        )
        self.check_object_permissions(self.request, incident)
        return incident

    def partial_update(self, request, *args, **kwargs):
        incident = self.get_object()
        serializer = IncidentUpdateSerializer(incident, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        new_value = serializer.validated_data.get(
            "excluded_from_reports", incident.excluded_from_reports
        )
        if new_value != incident.excluded_from_reports:
            with transaction.atomic():
                incident.excluded_from_reports = new_value
                incident.save(update_fields=["excluded_from_reports"])
                kind = IncidentEvent.Kind.EXCLUDED if new_value else IncidentEvent.Kind.INCLUDED
                IncidentEvent.objects.create(
                    incident=incident, kind=kind, created_at=timezone.now()
                )
                audit(
                    "incident_excluded" if new_value else "incident_included",
                    request,
                    request.user,
                    incident_id=str(incident.id),
                )
            # Uptime figures leave out excluded incidents, so the hours it covers are redone.
            try:
                from analytics.rollups import recompute_for_incident

                recompute_for_incident(incident)
            except Exception:  # noqa: BLE001 - the toggle itself succeeded; the next run catches up
                logger.exception("couldn't refresh rollups for incident %s", incident.id)
        incident = self.get_object()
        return Response(
            IncidentDetailSerializer(incident, context=self.get_serializer_context()).data
        )

    # -- comments -------------------------------------------------------------

    @extend_schema(
        tags=["incidents"], request=CommentCreateSerializer, responses={201: CommentSerializer}
    )
    @action(detail=True, methods=["post"], url_path="comments")
    def add_comment(self, request, pk=None):
        incident = self.get_object()
        serializer = CommentCreateSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        comment = serializer.save(incident=incident, author=request.user)
        return Response(
            CommentSerializer(comment, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(
        tags=["incidents"], request=CommentSerializer, responses={200: CommentSerializer}
    )
    @action(
        detail=True,
        methods=["patch", "delete"],
        url_path=r"comments/(?P<comment_id>[0-9a-f-]{36})",
        url_name="comment",
    )
    def comment(self, request, pk=None, comment_id=None):
        incident = self.get_object()
        # Only your own comments can be changed. (Single-user today, but the rule is cheap.)
        comment = get_object_or_404(
            IncidentComment, pk=comment_id, incident=incident, author=request.user
        )
        if request.method == "DELETE":
            comment.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        serializer = CommentSerializer(
            comment, data=request.data, partial=True, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    # -- postmortem and export --------------------------------------------------

    @extend_schema(tags=["incidents"], request=None, responses={200: PostmortemSerializer})
    @action(detail=True, methods=["post"])
    def postmortem(self, request, pk=None):
        incident = self.get_object()
        if incident.ongoing:
            raise ValidationError("A postmortem can be written once the incident is resolved.")
        return Response(postmortem.build(incident, request.user.timezone))

    @extend_schema(
        tags=["incidents"],
        responses={(200, "text/csv"): bytes},
        parameters=[
            OpenApiParameter("status", enum=["ongoing", "resolved"]),
            OpenApiParameter("monitor_id", type=uuid.UUID),
            OpenApiParameter("start_date"),
            OpenApiParameter("end_date"),
            OpenApiParameter("excluded", type=bool),
        ],
    )
    @action(detail=False, methods=["get"])
    def export(self, request):
        rows = self.get_queryset().order_by("-started_at", "id")[:EXPORT_LIMIT]
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        stamp = timezone.now().strftime("%Y%m%d")
        response["Content-Disposition"] = f'attachment; filename="incidents-{stamp}.csv"'
        writer = csv.writer(response)
        writer.writerow(
            [
                "id",
                "monitor",
                "type",
                "target",
                "started_at",
                "ended_at",
                "duration_seconds",
                "status",
                "reason",
                "http_status",
                "resolution",
                "excluded_from_reports",
            ]
        )
        for incident in rows:
            writer.writerow(
                [
                    incident.id,
                    _formula_safe(incident.monitor.name),
                    incident.monitor.type,
                    _formula_safe(incident.monitor.target),
                    incident.started_at.isoformat(),
                    incident.ended_at.isoformat() if incident.ended_at else "",
                    services.duration_seconds(incident),
                    "ongoing" if incident.ongoing else "resolved",
                    incident.reason,
                    incident.status_code or "",
                    incident.resolution,
                    "yes" if incident.excluded_from_reports else "no",
                ]
            )
        return response
