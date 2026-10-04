"""Endpoints that aren't for browsers: the signed scheduler trigger and the public
heartbeat ping. Neither uses the user JWT; each has its own gate."""

from __future__ import annotations

import logging

from django.conf import settings
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from . import signing
from .engine import run_scheduler_once
from .models import Monitor

logger = logging.getLogger(__name__)


class RunDueChecksView(APIView):
    """`POST /internal/run-due-checks/`: one scheduler pass, called by GitHub Actions.

    Gated entirely by an HMAC signature over a fresh timestamp (there is no user
    here), so it is not rate limited by user. An unsigned or stale call gets a bare
    401 that doesn't say why. The call also keeps Render's free service awake.
    """

    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_classes: list = []

    def get_authenticate_header(self, request):
        return "HMAC"

    @extend_schema(exclude=True)
    def post(self, request):
        if not signing.verify(
            settings.SCHEDULER_SHARED_SECRET,
            request.headers.get(signing.HEADER),
            request.body,
        ):
            return Response({"detail": "Unauthorized."}, status=status.HTTP_401_UNAUTHORIZED)

        summary = run_scheduler_once()
        logger.info("scheduler pass: %s", summary.as_dict())
        return Response(summary.as_dict())


class HeartbeatView(APIView):
    """`GET|POST|HEAD /api/v1/heartbeat/{token}/`: a job reporting that it ran.

    The token in the URL is the credential. Unknown tokens get a plain 404, and
    a paused monitor acknowledges the ping without acting on it.
    """

    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "heartbeat"

    def get_authenticate_header(self, request):
        return "Token"

    @extend_schema(exclude=True)
    def get(self, request, token):
        return self._ping(token)

    @extend_schema(exclude=True)
    def post(self, request, token):
        return self._ping(token)

    def _ping(self, token: str) -> Response:
        monitor = Monitor.objects.filter(heartbeat_token=token).first()
        if monitor is None:
            return Response({"detail": "Unknown heartbeat."}, status=status.HTTP_404_NOT_FOUND)
        if monitor.status == Monitor.Status.PAUSED:
            return Response({"status": "paused"})

        now = timezone.now()
        changes = {"last_heartbeat_at": now}
        if monitor.status == Monitor.Status.DOWN:
            changes["next_check_at"] = now  # let the next pass notice it's alive again
        Monitor.objects.filter(pk=monitor.pk).exclude(status=Monitor.Status.PAUSED).update(
            **changes
        )
        return Response({"status": "ok"})
