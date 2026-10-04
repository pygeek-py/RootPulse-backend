"""URL configuration for RootPulse. See docs/plan/02-database-and-api.md."""

from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from monitoring.internal_views import HeartbeatView, RunDueChecksView


def health(request):
    """The "who watches the watcher" external check hits this (docs/plan/01-tech-stack.md)."""
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("admin/", admin.site.urls),
    path("health/", health, name="health"),
    path("api/v1/auth/", include("accounts.urls")),
    path("api/v1/monitors/", include("monitoring.urls")),
    path("api/v1/incidents/", include("incidents.urls")),
    path("api/v1/heartbeat/<str:token>/", HeartbeatView.as_view(), name="heartbeat"),
    path("internal/run-due-checks/", RunDueChecksView.as_view(), name="run-due-checks"),
    path("api/v1/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/v1/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="docs"),
]
