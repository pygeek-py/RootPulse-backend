from django.urls import path
from drf_spectacular.utils import extend_schema

from .views import IncidentsCsvAlias, StatusPagePdfView, UptimeCsvView, UptimePdfView

urlpatterns = [
    path(
        "reports/incidents.csv",
        extend_schema(exclude=True)(IncidentsCsvAlias).as_view({"get": "export"}),
        name="report-incidents-csv",
    ),
    path("reports/uptime.csv", UptimeCsvView.as_view(), name="report-uptime-csv"),
    path("reports/uptime.pdf", UptimePdfView.as_view(), name="report-uptime-pdf"),
    path(
        "reports/status-page/<uuid:page_id>.pdf",
        StatusPagePdfView.as_view(),
        name="report-status-page-pdf",
    ),
]
