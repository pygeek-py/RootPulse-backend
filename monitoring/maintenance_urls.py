from rest_framework.routers import SimpleRouter

from .maintenance import MaintenanceWindowViewSet

router = SimpleRouter()
router.register("", MaintenanceWindowViewSet, basename="maintenance-window")

urlpatterns = router.urls
