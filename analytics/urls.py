from django.urls import path

from .views import FleetAnalyticsView

urlpatterns = [
    path("analytics/fleet/", FleetAnalyticsView.as_view(), name="fleet-analytics"),
]
