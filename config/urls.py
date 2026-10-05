"""URL configuration for RootPulse. See docs/plan/02-database-and-api.md."""

from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from monitoring.internal_views import HeartbeatView, RunDueChecksView, RunRollupsView


def health(request):
    """The "who watches the watcher" external check hits this (docs/plan/01-tech-stack.md)."""
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("admin/", admin.site.urls),
    path("health/", health, name="health"),
    path("api/v1/auth/", include("accounts.urls")),
    path("api/v1/monitors/", include("monitoring.urls")),
    path("api/v1/maintenance-windows/", include("monitoring.maintenance_urls")),
    path("api/v1/incidents/", include("incidents.urls")),
    path("api/v1/", include("notifications.urls")),
    path("api/v1/", include("analytics.urls")),
    path("api/v1/", include("deploys.urls")),
    path("api/v1/", include("providers.urls")),
    path("api/v1/", include("statuspages.urls")),
    path("api/v1/", include("reports.urls")),
    path("api/v1/heartbeat/<str:token>/", HeartbeatView.as_view(), name="heartbeat"),
    path("internal/run-due-checks/", RunDueChecksView.as_view(), name="run-due-checks"),
    path("internal/run-rollups/", RunRollupsView.as_view(), name="run-rollups"),
    path("api/v1/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/v1/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="docs"),
]
