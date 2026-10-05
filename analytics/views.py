from __future__ import annotations

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import queries
from .serializers import FleetAnalyticsSerializer


def parse_range(request, allowed, default: str) -> str:
    value = request.query_params.get("range", default)
    if value not in allowed:
        raise ValidationError({"range": f"Use one of: {', '.join(allowed)}."})
    return value


class FleetAnalyticsView(APIView):
    """Everything about the signed-in user's monitors together, read from daily rollups."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["analytics"],
        parameters=[OpenApiParameter("range", enum=list(queries.FLEET_RANGES), default="30d")],
        responses={200: FleetAnalyticsSerializer},
    )
    def get(self, request):
        range_key = parse_range(request, queries.FLEET_RANGES, "30d")
        return Response(queries.fleet_analytics(request.user, range_key))
