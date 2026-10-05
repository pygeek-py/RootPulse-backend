"""`/api/v1/reports/`: files to download. Every one is scoped to the signed-in user and generated
on the spot from the same data the dashboards read."""

from __future__ import annotations

import csv
import uuid

from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from incidents.views import IncidentViewSet, _formula_safe
from statuspages import services as page_services
from statuspages.models import StatusPage

from . import pdf, services

RANGE_PARAMS = [
    OpenApiParameter("start_date", description="First day, YYYY-MM-DD (a UTC day)."),
    OpenApiParameter("end_date", description="Last day, YYYY-MM-DD (a UTC day), inclusive."),
    OpenApiParameter("monitor_id", type=uuid.UUID, description="Only this monitor."),
]


class ReportView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "reports"

    def report(self, request) -> dict:
        params = request.query_params
        start, end = services.parse_range(params.get("start_date"), params.get("end_date"))
        monitor_id = None
        if raw := params.get("monitor_id"):
            try:
                monitor_id = uuid.UUID(raw)
            except ValueError as exc:
                raise ValidationError({"monitor_id": "Not a valid monitor id."}) from exc
        return services.uptime_report(request.user, start, end, monitor_id=monitor_id)

    @staticmethod
    def download(content: bytes | str, content_type: str, filename: str) -> HttpResponse:
        response = HttpResponse(content, content_type=content_type)
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        response["Cache-Control"] = "no-store"
        return response


def _span(report: dict) -> str:
    return f"{report['period']['start']}_to_{report['period']['end']}"


class UptimeCsvView(ReportView):
    @extend_schema(tags=["reports"], parameters=RANGE_PARAMS, responses={(200, "text/csv"): bytes})
    def get(self, request):
        report = self.report(request)
        response = self.download(
            "", "text/csv; charset=utf-8", f"rootpulse-uptime-{_span(report)}.csv"
        )
        writer = csv.writer(response)
        writer.writerow(
            [
                "monitor",
                "type",
                "target",
                "checks",
                "successful_checks",
                "failed_checks",
                "uptime_percent",
                "avg_response_ms",
                "incidents",
                "downtime_seconds",
                "longest_incident_seconds",
                "mean_time_to_recover_seconds",
            ]
        )

        def cell(value):
            return "" if value is None else value

        for m in report["monitors"]:
            writer.writerow(
                [
                    _formula_safe(m["name"]),
                    m["type"],
                    _formula_safe(m["target"]),
                    m["checks"],
                    m["up"],
                    m["down"],
                    cell(m["uptime_percent"]),
                    cell(m["avg_response_ms"]),
                    m["incidents"],
                    m["downtime_seconds"],
                    m["longest_incident_seconds"],
                    cell(m["mttr_seconds"]),
                ]
            )
        s = report["summary"]
        writer.writerow(
            [
                "All monitors",
                "",
                "",
                s["checks"],
                "",
                "",
                cell(s["uptime_percent"]),
                cell(s["avg_response_ms"]),
                s["incidents"],
                s["downtime_seconds"],
                s["longest_incident_seconds"],
                cell(s["mttr_seconds"]),
            ]
        )
        return response


class UptimePdfView(ReportView):
    @extend_schema(
        tags=["reports"], parameters=RANGE_PARAMS, responses={(200, "application/pdf"): bytes}
    )
    def get(self, request):
        report = self.report(request)
        return self.download(
            pdf.uptime_report_pdf(report, request.user.timezone),
            "application/pdf",
            f"rootpulse-uptime-{_span(report)}.pdf",
        )


class StatusPagePdfView(ReportView):
    @extend_schema(tags=["reports"], responses={(200, "application/pdf"): bytes})
    def get(self, request, page_id):
        page = get_object_or_404(StatusPage.objects.filter(user=request.user), pk=page_id)
        payload = page_services.build_public(page)
        return self.download(
            pdf.status_page_pdf(payload, request.user.timezone),
            "application/pdf",
            f"{page.slug}-status.pdf",
        )


class IncidentsCsvAlias(IncidentViewSet):
    """`GET /reports/incidents.csv`: the incidents export (the same filters and columns as
    `GET /incidents/export/`), so every download lives under /reports/."""

    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "reports"
    http_method_names = ["get", "head", "options"]
