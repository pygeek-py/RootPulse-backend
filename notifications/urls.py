from django.urls import path
from rest_framework.routers import SimpleRouter

from .views import AlertContactViewSet, DeliveryViewSet, VapidKeyView

router = SimpleRouter()
router.register("alert-contacts", AlertContactViewSet, basename="alert-contact")
router.register("notifications", DeliveryViewSet, basename="notification")

urlpatterns = [
    path("notifications/vapid-key/", VapidKeyView.as_view(), name="vapid-key"),
    *router.urls,
]
